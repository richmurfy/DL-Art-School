import torch
import torch.nn as nn
import torch.nn.functional as F

from models.gpt_voice.lucidrains_gpt import Transformer
from models.gpt_voice.min_gpt import GPT, GPTConfig
from models.tacotron2.taco_utils import get_mask_from_lengths
from models.tacotron2.text import symbols
from trainer.networks import register_model
from utils.util import opt_get


class GptTts(nn.Module):
    MAX_SYMBOLS_PER_PHRASE = 200
    NUMBER_SYMBOLS = len(symbols)
    NUMBER_TEXT_TOKENS = NUMBER_SYMBOLS + MAX_SYMBOLS_PER_PHRASE + 2
    MEL_DICTIONARY_SIZE = 512+3
    MEL_START_TOKEN = MEL_DICTIONARY_SIZE-3
    MEL_STOP_TOKEN = MEL_DICTIONARY_SIZE-2

    def __init__(self, layers=8, model_dim=512, heads=8):
        super().__init__()
        max_mel_frames = 900 * 1 // 4  #  900 is the max number of MEL frames. The VQVAE outputs 1/8 of the input mel as tokens.

        self.model_dim = model_dim
        self.max_mel_frames = max_mel_frames
        self.text_embedding = nn.Embedding(self.NUMBER_TEXT_TOKENS, model_dim)
        self.mel_embedding = nn.Embedding(self.MEL_DICTIONARY_SIZE, model_dim)
        self.text_pos_embedding = nn.Embedding(self.MAX_SYMBOLS_PER_PHRASE, model_dim)
        self.mel_pos_embedding = nn.Embedding(max_mel_frames, model_dim)
        #self.gpt = GPT(GPTConfig(1+self.MAX_SYMBOLS_PER_PHRASE+max_mel_frames, n_layer=8, n_embd=model_dim, n_head=8), do_pos_emb=False)
        self.gpt = Transformer(dim=model_dim, depth=layers, seq_len=1+self.MAX_SYMBOLS_PER_PHRASE+max_mel_frames, heads=heads)

        self.final_norm = nn.LayerNorm(model_dim)
        self.text_head = nn.Linear(model_dim, self.NUMBER_TEXT_TOKENS)
        self.mel_head = nn.Linear(model_dim, self.MEL_DICTIONARY_SIZE)

    def forward(self, text_inputs, text_lengths, mel_targets, output_lengths):
        text_emb = self.text_embedding(text_inputs)
        text_emb = text_emb + self.text_pos_embedding(torch.arange(text_inputs.shape[1], device=text_inputs.device))
        mel_emb = self.mel_embedding(mel_targets)
        mel_emb = mel_emb + self.mel_pos_embedding(torch.arange(mel_targets.shape[1], device=mel_targets.device))
        emb = torch.cat([text_emb, mel_emb], dim=1)
        enc = self.gpt(emb)

        # Compute logits for text and mel heads
        text_logits = self.final_norm(enc[:, :text_emb.shape[1]])
        mel_logits = self.final_norm(enc[:, text_emb.shape[1]:])
        text_logits = self.text_head(text_logits)
        mel_logits = self.mel_head(mel_logits)

        # Compute loss
        text_targets = text_inputs[:,1:]
        text_logits = text_logits.permute(0,2,1)[:,:,:-1]  # The last element of the logits is unneeded because the input to the transformer contains a <EOS> token for both text and mel.
        loss_text = F.cross_entropy(text_logits, text_targets, reduction='none')
        mel_targets = mel_targets[:,1:]
        mel_logits = mel_logits.permute(0,2,1)[:,:,:-1]
        loss_mel = F.cross_entropy(mel_logits, mel_targets, reduction='none')

        # Fix up mel_logits so it can go into a VAE decoder as well.
        mel_codes = torch.argmax(F.softmax(mel_logits, dim=1), dim=1)
        mel_pad_mask = ~get_mask_from_lengths(output_lengths-1, mel_targets.shape[1])
        mel_codes = mel_codes * torch.ones_like(mel_codes).masked_fill_(mel_pad_mask, 0)
        mel_codes = mel_codes[:,:-1]  # Strip off <EOS> token too (or padding). The important part is that the output sequence length is identical to the VAE input.
        extra_mask = mel_codes < self.MEL_DICTIONARY_SIZE-3  # The VAE doesn't know about START/STOP/PAD
        mel_codes = mel_codes * extra_mask

        # This class also returns the mel_targets for validation purposes. Format those.
        mel_targets = mel_targets[:,:-1]
        mel_targets = mel_targets * (mel_targets < self.MEL_DICTIONARY_SIZE-3)
        return loss_text.mean(), loss_mel.mean(), mel_codes, mel_targets

    def inference(self, text_inputs):
        b, _ = text_inputs.shape
        text_emb = self.text_embedding(text_inputs)
        text_emb = text_emb + self.text_pos_embedding(torch.arange(text_inputs.shape[1], device=text_inputs.device))

        mel_seq = torch.full((b,1), fill_value=self.MEL_START_TOKEN, device=text_emb.device)
        stop_encountered = torch.zeros((b,), device=text_emb.device)
        while not torch.all(stop_encountered) and len(mel_seq) < self.max_mel_frames:
            mel_emb = self.mel_embedding(mel_seq)
            mel_emb = mel_emb + self.mel_pos_embedding(torch.arange(mel_emb.shape[1], device=mel_emb.device))
            emb = torch.cat([text_emb, mel_emb], dim=1)
            enc = self.gpt(emb)
            mel_logits = self.final_norm(enc[:, text_emb.shape[1]:])
            mel_logits = self.mel_head(mel_logits)
            mel_codes = torch.argmax(F.softmax(mel_logits, dim=-1), dim=-1)
            mel_seq = torch.cat([mel_seq, mel_codes[:, -1].unsqueeze(1)], dim=1)
            stop_encountered = torch.logical_or(stop_encountered, mel_seq[:,-1] == self.MEL_STOP_TOKEN)

        if len(mel_seq) >= self.max_mel_frames:
            print("Warning! Encountered frame limit before a stop token. Output is likely wrong.")

        # Format mel_seq so that the DVAE can actually use it (it is a two-tiered DVAE)
        mel_seq = mel_seq[:, 1:-1]  # Remove first and last tokens, which were artificially added for GPT
        mel_seq = mel_seq * (mel_seq < 512)  # The DVAE doesn't understand BOS/EOS/PAD tokens.

        return mel_seq

    def inference_beam(self, text_inputs):
        pass


@register_model
def register_gpt_tts(opt_net, opt):
    return GptTts(**opt_get(opt_net, ['kwargs'], {}))


if __name__ == '__main__':
    gpt = GptTts()
    l1, l2, i = gpt(torch.randint(high=24, size=(2,60)),
               torch.tensor([55,58]),
               torch.randint(high=512, size=(2,310)),
               torch.tensor([300,305]))
    print(i.shape)

    #o = gpt.infer(torch.randint(high=24, size=(2,60)))
    #print(o.shape)


