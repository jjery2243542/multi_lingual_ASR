import torch 
import torch.nn.functional as F
from torch.nn.utils.rnn import pack_padded_sequence
from torch.nn.utils.rnn import pad_packed_sequence
from torch.nn.utils.rnn import pad_sequence
import numpy as np
from utils import cc
from utils import pad_list
import os

def _get_vgg2l_odim(idim, in_channel=1, out_channel=128):
    idim = idim / in_channel
    idim = np.ceil(np.array(idim, dtype=np.float32) / 2)
    idim = np.ceil(np.array(idim, dtype=np.float32) / 2)
    return int(idim) * out_channel

def _pad_one_frame(inp):
    inp_t = inp.transpose(1, 2)
    out_t = F.pad(inp_t, (0, 1), mode='replicate')
    out = out_t.transpose(1, 2)
    return out

class VGG2L(torch.nn.Module):
    def __init__(self, in_channel=1):
        super(VGG2L, self).__init__()
        self.in_channel = in_channel
        self.conv1_1 = torch.nn.Conv2d(in_channel, 64, 3, stride=1, padding=1)
        self.conv1_2 = torch.nn.Conv2d(64, 64, 3, stride=1, padding=1)
        self.conv2_1 = torch.nn.Conv2d(64, 128, 3, stride=1, padding=1)
        self.conv2_2 = torch.nn.Conv2d(128, 128, 3, stride=1, padding=1)

    def conv_block(self, inp, layers):
        out = inp
        for layer in layers:
            out = F.relu(layer(out))
        out = F.max_pool2d(out, 2, stride=2, ceil_mode=True)
        return out

    def forward(self, xs, ilens):
        # xs = [batch_size, frames, feeature_dim]
        # ilens is a list of frame length of each utterance 
        xs = torch.transpose(
                xs.view(xs.size(0), xs.size(1), self.in_channel, xs.size(2)//self.in_channel), 1, 2)
        xs = self.conv_block(xs, [self.conv1_1, self.conv1_2])
        xs = self.conv_block(xs, [self.conv2_1, self.conv2_2])
        ilens = np.array(np.ceil(np.array(ilens, dtype=np.float32) / 2), dtype=np.int64) 
        ilens = np.array(np.ceil(np.array(ilens, dtype=np.float32) / 2), dtype=np.int64).tolist()
        xs = torch.transpose(xs, 1, 2)
        xs = xs.contiguous().view(xs.size(0), xs.size(1), xs.size(2) * xs.size(3))
        return xs, ilens

class pBLSTM(torch.nn.Module):
    def __init__(self, input_dim, hidden_dim, n_layers, subsample, dropout_rate):
        super(pBLSTM, self).__init__()
        layers, dropout_layers = [], []
        for i in range(n_layers):
            idim = input_dim if i == 0 else hidden_dim * 2
            layers.append(torch.nn.LSTM(idim, hidden_dim, num_layers=1, bidirectional=True, batch_first=True))
            dropout_layers.append(torch.nn.Dropout(p=dropout_rate, inplace=True))
        self.layers = torch.nn.ModuleList(layers)
        self.dropout_layers = torch.nn.ModuleList(dropout_layers)
        self.subsample = subsample

    def forward(self, xpad, ilens):
        for i, (layer, dropout_layer) in enumerate(zip(self.layers, self.dropout_layers)):
            # pack sequence 
            xpack = pack_padded_sequence(xpad, ilens, batch_first=True)
            xs, (_, _) = layer(xpack)
            xpad, ilens = pad_packed_sequence(xs, batch_first=True)
            #xpad = dropout_layer(xpad)
            dropout_layer(xpad)
            ilens = ilens.numpy()
            # subsampling
            sub = self.subsample[i]
            if sub > 1:
                xpad = xpad[:, ::sub]
                ilens = [(length + 1) // sub for length in ilens]
        # type to list of int
        ilens = np.array(ilens, dtype=np.int64).tolist()
        return xpad, ilens

class Encoder(torch.nn.Module):
    def __init__(self, input_dim, hidden_dim, n_layers, subsample, dropout_rate, in_channel=1):
        super(Encoder, self).__init__()
        self.enc1 = VGG2L(in_channel)
        out_channel = _get_vgg2l_odim(input_dim) 
        self.enc2 = pBLSTM(input_dim=out_channel, hidden_dim=hidden_dim, 
                n_layers=n_layers, subsample=subsample, dropout_rate=dropout_rate)

    def forward(self, x, ilens):
        out, ilens = self.enc1(x, ilens)
        out, ilens = self.enc2(out, ilens)
        return out, ilens

class MultiHeadAttLoc(torch.nn.Module):
    def __init__(self, encoder_dim, decoder_dim, att_dim, conv_channels, conv_kernel_size, heads, att_odim):
        super(MultiHeadAttLoc, self).__init__()
        self.heads = heads
        self.mlp_enc = torch.nn.ModuleList([torch.nn.Linear(encoder_dim, att_dim) for _ in range(self.heads)])
        self.mlp_dec = torch.nn.ModuleList([torch.nn.Linear(decoder_dim, att_dim, bias=False) \
		for _ in range(self.heads)])
        self.mlp_att = torch.nn.ModuleList([torch.nn.Linear(conv_channels, att_dim, bias=False) \
		for _ in range(self.heads)])
        self.loc_conv = torch.nn.ModuleList([torch.nn.Conv2d(
                1, conv_channels, (1, conv_kernel_size), bias=False) for _ in range(self.heads)])
        if conv_kernel_size % 2 == 0:
            self.padding = (conv_kernel_size // 2, conv_kernel_size // 2 - 1)
        else:
            self.padding = (conv_kernel_size // 2, conv_kernel_size // 2)
        self.gvec = torch.nn.ModuleList([torch.nn.Linear(att_dim, 1, bias=False) for _ in range(self.heads)])
        self.mlp_o = torch.nn.Linear(self.heads * encoder_dim, att_odim)

        self.encoder_dim = encoder_dim
        self.decoder_dim = decoder_dim
        self.att_dim = att_dim
        self.conv_channels = conv_channels
        self.enc_length = None
        self.enc_h = None
        self.pre_compute_enc_h = None

    def reset(self):
        self.enc_length = None
        self.enc_h = None
        self.pre_compute_enc_h = None

    def forward(self, enc_pad, enc_len, dec_z, att_prev, scaling=1.0):
        batch_size =enc_pad.size(0)
        if self.pre_compute_enc_h is None:
            self.enc_h = enc_pad
            self.enc_length = self.enc_h.size(1)
            self.pre_compute_enc_h = [self.mlp_enc[h](self.enc_h) for h in range(self.heads)]

        if dec_z is None:
            dec_z = enc_pad.data.new(batch_size, self.decoder_dim).zero_()
        else:
            dec_z = dec_z.view(batch_size, self.decoder_dim)

        # initialize attention weights to uniform
        if att_prev is None:
            one_head = [enc_pad.data.new(l).zero_() + (1 / l) for l in enc_len]
            one_head = pad_sequence(one_head, batch_first=True, padding_value=0)
            att_prev = [one_head] + [one_head.clone() for _ in range(self.heads - 1)]
        cs, ws = [], []
        for h in range(self.heads):
            #att_prev: batch_size x frame
            att_prev_pad = F.pad(att_prev[h].view(batch_size, 1, 1, self.enc_length), self.padding)
            att_conv = self.loc_conv[h](att_prev_pad)
            # att_conv: batch_size x channel x 1 x frame -> batch_size x frame x channel
            att_conv = att_conv.squeeze(2).transpose(1, 2)
            # att_conv: batch_size x frame x channel -> batch_size x frame x att_dim
            att_conv = self.mlp_att[h](att_conv)

            # dec_z_tiled: batch_size x 1 x att_dim
            dec_z_tiled = self.mlp_dec[h](dec_z).view(batch_size, 1, self.att_dim)
            att_state = torch.tanh(self.pre_compute_enc_h[h] + dec_z_tiled + att_conv)
            e = self.gvec[h](att_state).squeeze(2)
            # w: batch_size x frame
            w = F.softmax(scaling * e, dim=1)
            ws.append(w)
            # w_expanded: batch_size x 1 x frame
            w_expanded = w.unsqueeze(1)
            c = torch.bmm(w_expanded, self.enc_h).squeeze(1)
            cs.append(c)
        c = self.mlp_o(torch.cat(cs, dim=1))
        return c, ws 

class StateTransform(torch.nn.Module):
    def __init__(self, idim, odim):
        super(StateTransform, self).__init__()
        self.fcz = torch.nn.Linear(idim, odim)
        self.fcc = torch.nn.Linear(idim, odim)

    def forward(self, z):
        dec_init_z = F.relu(self.fcz(z))
        dec_init_c = F.relu(self.fcc(z))
        return dec_init_z, dec_init_c

class Decoder(torch.nn.Module):
    def __init__(self, output_dim, hidden_dim, encoder_dim, attention, att_odim, dropout_rate, bos, eos, pad):
        super(Decoder, self).__init__()
        self.bos, self.eos, self.pad = bos, eos, pad
        # 3 is bos, eos, pad
        self.embedding = torch.nn.Embedding(output_dim + 3, hidden_dim, padding_idx=pad)
        self.LSTMCell = torch.nn.LSTMCell(att_odim + hidden_dim, hidden_dim)
        # 3 is bos, eos, pad
        self.dropout = torch.nn.Dropout(p=dropout_rate, inplace=True)
        self.output_layer = torch.nn.Linear(hidden_dim, output_dim + 3)
        self.attention = attention

    def forward(self, enc_pad, enc_len, dec_init_state, ys=None, tf_rate=0.8, max_dec_timesteps=500):
        batch_size = enc_pad.size(0)
        if ys is not None:
            # prepare input and output sequences
            bos = ys[0].data.new([self.bos])
            eos = ys[0].data.new([self.eos])
            ys_in = [torch.cat([bos, y], dim=0) for y in ys]
            ys_out = [torch.cat([y, eos], dim=0) for y in ys]
            pad_ys_in = pad_list(ys_in, pad_value=self.pad)
            pad_ys_out = pad_list(ys_out, pad_value=self.pad)
            # get length info
            batch_size, olength = pad_ys_out.size(0), pad_ys_out.size(1)
            # map idx to embedding
            eys = self.embedding(pad_ys_in)
        # loop for each timestep
        dec_z, dec_c = dec_init_state
        ws = None
        logits, prediction, ws_list = [], [], []
        # reset the attention module
        self.attention.reset()
        olength = max_dec_timesteps if not ys else olength
        for t in range(olength):
            # run attention module
            c, ws = self.attention(enc_pad, enc_len, dec_z, ws)
            ws_list.append(ws)
            # supervised learning: using teacher forcing
            if ys is not None:
                # teacher forcing
                tf = True if np.random.random_sample() < tf_rate else False
                emb = eys[:, t, :] if tf or t == 0 else self.embedding(prediction[-1])
            # else, label the data with greedy
            else:
                if t == 0:
                    bos = cc(torch.Tensor([self.bos for _ in range(batch_size)]).type(torch.LongTensor))
                    emb = self.embedding(bos)
                else:
                    emb = self.embedding(prediction[-1])
            cell_inp = torch.cat([emb, c], dim=-1)
            dec_z, dec_c = self.LSTMCell(cell_inp, (dec_z, dec_c))
            logit = self.output_layer(dec_z)
            logits.append(logit)
            prediction.append(torch.argmax(logit, dim=-1))
        logits = torch.stack(logits, dim=1)
        log_probs = F.log_softmax(logits, dim=1)
        prediction = torch.stack(prediction, dim=1)
        if ys:
            ys_log_probs = torch.gather(log_probs, dim=2, index=pad_ys_out.unsqueeze(2)).squeeze_(2)
        else:
            ys_log_probs = torch.gather(log_probs, dim=2, index=prediction.unsqueeze(2)).squeeze_(2)
        return ys_log_probs, prediction, ws_list

class E2E(torch.nn.Module):
    def __init__(self, input_dim, enc_hidden_dim, enc_n_layers, subsample, dropout_rate, 
            dec_hidden_dim, att_dim, conv_channels, conv_kernel_size, att_odim,
            output_dim, pad=0, bos=1, eos=2, heads=4):
        super(E2E, self).__init__()
        # encoder to encode acoustic features
        self.encoder = Encoder(input_dim=input_dim, hidden_dim=enc_hidden_dim, 
                n_layers=enc_n_layers, subsample=subsample, dropout_rate=dropout_rate)
        # transform encoder's last output to decoder hidden_dim 
        self.state_transform = StateTransform(enc_hidden_dim * 2, dec_hidden_dim)
        # attention module
        self.attention = MultiHeadAttLoc(encoder_dim=enc_hidden_dim * 2, 
                decoder_dim=dec_hidden_dim, att_dim=att_dim, 
                conv_channels=conv_channels, conv_kernel_size=conv_kernel_size, 
                heads=heads, att_odim=att_odim)
        # decoder to generate words (or other units) 
        self.decoder = Decoder(output_dim=output_dim, hidden_dim=dec_hidden_dim, 
                encoder_dim=enc_hidden_dim, attention=self.attention, 
                dropout_rate=dropout_rate, att_odim=att_odim, 
                bos=bos, eos=eos, pad=pad)

    def forward(self, data, ilens, ys=None, tf_rate=1.0, max_dec_timesteps=200):
        enc_h, enc_lens = self.encoder(data, ilens)
        dec_h, dec_c = self.state_transform(enc_h[:, -1])
        log_probs, prediction, ws_list = self.decoder(enc_h, enc_lens, (dec_h, dec_c), ys, 
                tf_rate=tf_rate, max_dec_timesteps=max_dec_timesteps)
        return log_probs, prediction, ws_list


if __name__ == '__main__':
    # just for debugging
    def get_data(root_dir='/storage/feature/LibriSpeech/npy_files/train-clean-100/7402/90848', text_index_path='/storage/feature/LibriSpeech/text_bpe/train-clean-100/7402/7402-90848.label.txt'):
        prefix = '7402-90848'
        datas = []
        for i in range(8):
            seg_id = str(i).zfill(4)
            filename = f'{prefix}-{seg_id}.npy'
            path = os.path.join(root_dir, filename)
            data = torch.from_numpy(np.load(path)).type(torch.FloatTensor)
            datas.append(data)
        datas.sort(key=lambda x: x.size(0), reverse=True)
        ilens = np.array([data.size(0) for data in datas], dtype=np.int64)
        datas = pad_sequence(datas, batch_first=True, padding_value=0)

        ys = []
        with open(text_index_path, 'r') as f:
            for line in f:
                utt_id, indexes = line.strip().split(',', maxsplit=1)
                indexes = cc(torch.Tensor([int(index) + 3 for index in indexes.split()]).type(torch.LongTensor))
                ys.append(indexes)
        return datas, ilens, ys[:8]
    data, ilens, ys = get_data()
    data = cc(data)
    model = cc(E2E(input_dim=40, enc_hidden_dim=800, enc_n_layers=3, 
        subsample=[1, 2, 1], dropout_rate=0.3, 
        dec_hidden_dim=1024, att_dim=512, conv_channels=10, 
        conv_kernel_size=201, att_odim=800, output_dim=500))
    log_probs, prediction, ws_list = model(data, ilens, ys)
    p_lens = [p.size() for p in prediction]
    t_lens = [t.size() for t in ys]
    print(p_lens, t_lens)

