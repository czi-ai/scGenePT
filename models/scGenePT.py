import gc
import pickle as pkl
import matplotlib.pyplot as plt
import plotly.express as px

import json
import os
import sys
import time
import copy
from pathlib import Path
from typing import Iterable, List, Tuple, Dict, Union, Optional
import warnings

import torch
import numpy as np
import matplotlib
from torch import nn, Tensor
from torch.nn import functional as F
from torchtext.vocab import Vocab
from torchtext._torchtext import (
    Vocab as VocabPybind,
)
from torch_geometric.loader import DataLoader
from gears import PertData, GEARS
from gears.inference import compute_metrics, deeper_analysis, non_dropout_analysis
from gears.utils import create_cell_graph_dataset_for_prediction

sys.path.insert(0, "../")

import scgpt as scg
from scgpt.model import TransformerGenerator
from scgpt.loss import (
    masked_mse_loss,
    criterion_neg_log_bernoulli,
    masked_relative_error,
)
from scgpt.tokenizer import tokenize_batch, pad_batch, tokenize_and_pad_batch
from scgpt.tokenizer.gene_tokenizer import GeneVocab
from scgpt.utils import set_seed, map_raw_id_to_vocab_id
import os
import math
from typing import Mapping, Optional, Tuple, Any, Union

import torch.distributed as dist
import torch.nn.functional as F
from torch.nn import TransformerEncoder, TransformerEncoderLayer
from torch.distributions import Bernoulli
from torch.utils.data import dataset
from tqdm import trange

from scgpt.model import (
    ExprDecoder,
    MVCDecoder,
    ContinuousValueEncoder,
    FastTransformerEncoderWrapper,
    FlashTransformerEncoderLayer,
)

matplotlib.rcParams["savefig.transparent"] = False
warnings.filterwarnings("ignore")

set_seed(42)

# code retrieved July 08, 2024 from https://czi-ie-meta-prod-east-databricks-workspace.cloud.databricks.com/?o=1826199555946458#notebook/3977793979534441/command/3977793979534454
class TransformerGenerator(nn.Module):
    def __init__(
        self,
        ntoken: int,
        d_model: int,
        nhead: int,
        d_hid: int,
        nlayers: int,
        nlayers_cls: int,
        n_cls: int,
        vocab: Any,
        dropout: float = 0.5,
        pad_token: str = "<pad>",
        pad_value: int = 0,
        pert_pad_id: int = 2,
        do_mvc: bool = False,
        domain_spec_batchnorm: Union[bool, str] = False,
        n_input_bins: Optional[int] = 0,
        cell_emb_style: str = "cls",
        mvc_decoder_style: str = "inner product",
        decoder_activation: Optional[str] = None,
        decoder_adaptive_bias: bool = False,
        ecs_threshold: float = 0.3,
        explicit_zero_prob: bool = False,
        use_fast_transformer: bool = False,
        fast_transformer_backend: str = "flash",
        pre_norm: bool = False,
        embs_to_include = ['scGPT_counts_embs', 'scGPT_token_embs', 'genePT_token_embs'],
        lookup_embed = None,
        use_drug_embeds = False,
        drug_embeds = None
    ):
        super().__init__()
        self.model_type = "Transformer"
        self.d_model = d_model
        self.pad_token_id = vocab[pad_token]
        self.pad_value = pad_value
        self.pert_pad_id = pert_pad_id
        self.ecs_threshold = ecs_threshold
        self.domain_spec_batchnorm = domain_spec_batchnorm
        self.n_input_bins = n_input_bins
        self.cell_emb_style = cell_emb_style
        self.explicit_zero_prob = explicit_zero_prob
        self.norm_scheme = "pre" if pre_norm else "post"
        if cell_emb_style not in ["cls", "avg-pool", "w-pool"]:
            raise ValueError(f"Unknown cell_emb_style: {cell_emb_style}")
        if use_fast_transformer:
            try:
                from flash_attn.flash_attention import FlashMHA
            except ImportError:
                import warnings

                warnings.warn(
                    "flash-attn is not installed, using pytorch transformer instead. "
                    "Set use_fast_transformer=False to avoid this warning. "
                    "Installing flash-attn is highly recommended."
                )
                use_fast_transformer = False
        self.use_fast_transformer = use_fast_transformer

        if 'scGPT_token_embs' in self.embs_to_include:
            self.encoder = GeneEncoder(ntoken, d_model, padding_idx=vocab[pad_token])
        if 'genePT_token_embs' in self.embs_to_include:
            self.genept_encoder = GenePTEncoder(ntoken, d_model, padding_idx=vocab[pad_token], genept_lookup_embed=lookup_embed)
        if 'scGPT_counts_embs' in self.embs_to_include:
            self.value_encoder = ContinuousValueEncoder(d_model, dropout)
        if use_drug_embeds:
            self.pert_encoder = PertEncoder(torch.from_numpy(drug_embeds), 1536, d_model, pert_pad_id) 
        else:
            self.pert_encoder = nn.Embedding(n_perturbagens + 1, d_model, padding_idx=pert_pad_id)

        # print("Using simple batchnorm instead of domain specific batchnorm")
        # self.bn = nn.BatchNorm1d(d_model, eps=6.1e-5)

        if use_fast_transformer:
            if fast_transformer_backend == "linear":
                self.transformer_encoder = FastTransformerEncoderWrapper(
                    d_model, nhead, d_hid, nlayers, dropout
                )
            elif fast_transformer_backend == "flash":
                encoder_layers = FlashTransformerEncoderLayer(
                    d_model,
                    nhead,
                    d_hid,
                    dropout,
                    batch_first=True,
                    norm_scheme=self.norm_scheme,
                )
                self.transformer_encoder = TransformerEncoder(encoder_layers, nlayers)
        else:
            encoder_layers = TransformerEncoderLayer(
                d_model, nhead, d_hid, dropout, batch_first=True
            )
            self.transformer_encoder = TransformerEncoder(encoder_layers, nlayers)

        # self.decoder = nn.Linear(d_model, 1)
        self.decoder = AffineExprDecoder(
            d_model,
            explicit_zero_prob=explicit_zero_prob,
            activation=decoder_activation,
            adaptive_bias=decoder_adaptive_bias,
        )
        self.cls_decoder = ClsDecoder(d_model, n_cls, nlayers=nlayers_cls)
        if do_mvc:
            self.mvc_decoder = MVCDecoder(
                d_model,
                arch_style=mvc_decoder_style,
                explicit_zero_prob=explicit_zero_prob,
            )

        if 'scGPT_token_embs' in self.embs_to_include:
            self.init_weights()

    def init_weights(self) -> None:
        initrange = 0.1
        self.encoder.embedding.weight.data.uniform_(-initrange, initrange)

    def _encode(
        self,
        src: Tensor,
        values: Tensor,
        input_pert_flags,
        src_key_padding_mask: Tensor,
    ) -> Tensor:
        embs2values = {}
        if 'scGPT_token_embs' in self.embs_to_include:
            src_scgpt = self.encoder(src)  # (batch, seq_len, embsize)
            self.cur_gene_token_embs = src_scgpt
            embs2values['scGPT_token_embs'] = src_scgpt
        if 'genePT_token_embs' in self.embs_to_include:
            src_genept = self.genept_encoder(src)  # (batch, seq_len, embsize)
            embs2values['genePT_token_embs'] = src_genept
            if 'scGPT_token_embs' in self.embs_to_include:
                self.cur_gene_token_embs += src_genept
            else:
                self.cur_gene_token_embs = src_genept
        if 'scGPT_counts_embs' in self.embs_to_include:
            embs2values['scGPT_counts_embs']  = self.value_encoder(values)  # (batch, seq_len, embsize)
            
        embs2values['pert_embs'] = self.pert_encoder(input_pert_flags)
        seen_embs = False
        for emb, emb_value in embs2values.items():
            if not seen_embs:
                total_embs = emb_value
                seen_embs = True
            else:
                total_embs += emb_value
        total_embs = total_embs.type(torch.float32)

        # total_embs = self.bn(total_embs.permute(0, 2, 1)).permute(0, 2, 1)
        output = self.transformer_encoder(
            total_embs, src_key_padding_mask=src_key_padding_mask
        )
        return output  # (batch, seq_len, embsize)

    def _get_cell_emb_from_layer(
        self, layer_output: Tensor, weights: Tensor = None
    ) -> Tensor:
        """
        Args:
            layer_output(:obj:`Tensor`): shape (batch, seq_len, embsize)
            weights(:obj:`Tensor`): shape (batch, seq_len), optional and only used
                when :attr:`self.cell_emb_style` is "w-pool".

        Returns:
            :obj:`Tensor`: shape (batch, embsize)
        """
        if self.cell_emb_style == "cls":
            cell_emb = layer_output[:, 0, :]  # (batch, embsize)
        elif self.cell_emb_style == "avg-pool":
            cell_emb = torch.mean(layer_output, dim=1)
        elif self.cell_emb_style == "w-pool":
            if weights is None:
                raise ValueError("weights is required when cell_emb_style is w-pool")
            if weights.dim() != 2:
                raise ValueError("weights should be 2D")
            cell_emb = torch.sum(layer_output * weights.unsqueeze(2), dim=1)
            cell_emb = F.normalize(cell_emb, p=2, dim=1)  # (batch, embsize)

        return cell_emb

    def forward(
        self,
        src: Tensor,
        values: Tensor,
        input_pert_flags: Tensor,
        src_key_padding_mask: Tensor,
        CLS: bool = False,
        CCE: bool = False,
        MVC: bool = False,
        ECS: bool = False,
        do_sample: bool = False,
    ) -> Mapping[str, Tensor]:
        """
        Args:
            src (:obj:`Tensor`): token ids, shape [batch_size, seq_len]
            values (:obj:`Tensor`): token values, shape [batch_size, seq_len]
            src_key_padding_mask (:obj:`Tensor`): mask for src, shape [batch_size,
                seq_len]
            CLS (:obj:`bool`): if True, return the celltype classification objective
                (CLS) output
            CCE (:obj:`bool`): if True, return the contrastive cell embedding objective
                (CCE) output
            MVC (:obj:`bool`): if True, return the masked value prediction for cell
                embedding MVC output
            ECS (:obj:`bool`): if True, return the elastic cell similarity objective
                (ECS) output.

        Returns:
            dict of output Tensors.
        """
        if self.explicit_zero_prob and not do_sample and not self.training:
            do_sample = True
            logger.warning("Auto set do_sample to True when model is in eval mode.")

        # binning input gene values
        if self.n_input_bins > 0:
            from ..preprocess import binning

            processed_values = torch.stack(
                [binning(row, n_bins=self.n_input_bins) for row in values], dim=0
            ).to(values.device)
        else:
            processed_values = values

        transformer_output = self._encode(
            src, processed_values, input_pert_flags, src_key_padding_mask
        )
        output = {}
        mlm_output = self.decoder(transformer_output, values)
        if self.explicit_zero_prob and do_sample:
            bernoulli = Bernoulli(probs=mlm_output["zero_probs"])
            output["mlm_output"] = bernoulli.sample() * mlm_output["pred"]
        else:
            output["mlm_output"] = mlm_output["pred"]  # (batch, seq_len)
        if self.explicit_zero_prob:
            output["mlm_zero_probs"] = mlm_output["zero_probs"]

        cell_emb = self._get_cell_emb_from_layer(transformer_output, values)
        if CLS:
            output["cls_output"] = self.cls_decoder(cell_emb)  # (batch, n_cls)
        if MVC:
            mvc_output = self.mvc_decoder(
                cell_emb,
                self.cur_gene_token_embs,
            )  # (batch, seq_len)
            if self.explicit_zero_prob and do_sample:
                bernoulli = Bernoulli(probs=mvc_output["zero_probs"])
                output["mvc_output"] = bernoulli.sample() * mvc_output["pred"]
            else:
                output["mvc_output"] = mvc_output["pred"]  # (batch, seq_len)
            if self.explicit_zero_prob:
                output["mvc_zero_probs"] = mvc_output["zero_probs"]
        if ECS:
            # Here using customized cosine similarity instead of F.cosine_similarity
            # to avoid the pytorch issue of similarity larger than 1.0, pytorch # 78064
            # normalize the embedding
            cell_emb_normed = F.normalize(cell_emb, p=2, dim=1)
            cos_sim = torch.mm(cell_emb_normed, cell_emb_normed.t())  # (batch, batch)

            # mask out diagnal elements
            mask = torch.eye(cos_sim.size(0)).bool().to(cos_sim.device)
            cos_sim = cos_sim.masked_fill(mask, 0.0)
            # only optimize positive similarities
            cos_sim = F.relu(cos_sim)

            output["loss_ecs"] = torch.mean(1 - (cos_sim - self.ecs_threshold) ** 2)

        return output

    def encode_batch(
        self,
        src: Tensor,
        values: Tensor,
        src_key_padding_mask: Tensor,
        batch_size: int,
        output_to_cpu: bool = True,
    ) -> Tensor:
        """
        Args:
            src: Tensor, shape [N, seq_len]
            values: Tensor, shape [N, seq_len]
            src_key_padding_mask: Tensor, shape [N, seq_len]

        Returns:
            output Tensor of shape [N, seq_len, embsize]
        """
        outputs = []
        N = src.size(0)
        device = next(self.parameters()).device
        for i in trange(0, N, batch_size):
            output = self._encode(
                src[i : i + batch_size].to(device),
                values[i : i + batch_size].to(device),
                src_key_padding_mask[i : i + batch_size].to(device),
            )
            if output_to_cpu:
                output = output.cpu()
            outputs.append(output)
        return torch.cat(outputs, dim=0)

    def pred_perturb(
        self,
        batch_data,
        include_zero_gene="batch-wise",
        gene_ids=None,
        amp=True,
    ) -> Tensor:
        """
        Args:
            batch_data: a dictionary of input data with keys.

        Returns:
            output Tensor of shape [N, seq_len]
        """
        self.eval()
        device = next(self.parameters()).device
        batch_data.to(device)
        batch_size = len(batch_data.pert)
        x: torch.Tensor = batch_data.x
        ori_gene_values = x[:, 0].view(batch_size, -1)  # (batch_size, n_genes)
        pert_flags = x[:, 1].long().view(batch_size, -1)

        if include_zero_gene in ["all", "batch-wise"]:
            assert gene_ids is not None
            if include_zero_gene == "all":
                input_gene_ids = torch.arange(ori_gene_values.size(1), device=device)
            else:  # batch-wise
                input_gene_ids = (
                    ori_gene_values.nonzero()[:, 1].flatten().unique().sort()[0]
                )
            input_values = ori_gene_values[:, input_gene_ids]
            input_pert_flags = pert_flags[:, input_gene_ids]

            mapped_input_gene_ids = map_raw_id_to_vocab_id(input_gene_ids, gene_ids)
            mapped_input_gene_ids = mapped_input_gene_ids.repeat(batch_size, 1)

            src_key_padding_mask = torch.zeros_like(
                input_values, dtype=torch.bool, device=device
            )
            with torch.cuda.amp.autocast(enabled=amp):
                output_dict = self(
                    mapped_input_gene_ids,
                    input_values,
                    input_pert_flags,
                    src_key_padding_mask=src_key_padding_mask,
                    CLS=False,
                    CCE=False,
                    MVC=False,
                    ECS=False,
                    do_sample=True,
                )
            output_values = output_dict["mlm_output"].float()
            pred_gene_values = torch.zeros_like(ori_gene_values)
            pred_gene_values[:, input_gene_ids] = output_values
        return pred_gene_values
    
    def pred_perturb(
        self,
        batch_data,
        include_zero_gene="batch-wise",
        gene_ids=None,
        amp=True,
        pert_type = 'intrinsic'
    ) -> Tensor:
        """
        Args:
            batch_data: a dictionary of input data with keys.

        Returns:
            output Tensor of shape [N, seq_len]
        """
        self.eval()
        device = next(self.parameters()).device
        if pert_type == 'intrinsic':
            batch_data.to(device)
            batch_size = len(batch_data.pert)
            x: torch.Tensor = batch_data.x
            ori_gene_values = x[:, 0].view(batch_size, -1)  # (batch_size, n_genes)
            pert_flags = x[:, 1].long().view(batch_size, -1)
        else:
            batch_size = len(batch_data['ctrl_gene_expression'])        
            ori_gene_values = batch_data['ctrl_gene_expression'].type(torch.float32) # control cell as input
            pert_flags = batch_data['pert_vector'].long() # flag whether a gene is perturbed or not

        ori_gene_values = ori_gene_values.to(device)
        pert_flags = pert_flags.to(device)

        if include_zero_gene in ["all", "batch-wise"]:
            assert gene_ids is not None
            if include_zero_gene == "all":
                input_gene_ids = torch.arange(ori_gene_values.size(1), device=device)
            else:  # batch-wise
                input_gene_ids = (
                    ori_gene_values.nonzero()[:, 1].flatten().unique().sort()[0]
                )
            input_values = ori_gene_values[:, input_gene_ids]
            input_pert_flags = pert_flags[:, input_gene_ids]

            mapped_input_gene_ids = map_raw_id_to_vocab_id(input_gene_ids, gene_ids)
            mapped_input_gene_ids = mapped_input_gene_ids.repeat(batch_size, 1)

            src_key_padding_mask = torch.zeros_like(
                input_values, dtype=torch.bool, device=device
            )
            with torch.cuda.amp.autocast(enabled=amp):
                output_dict = self(
                    mapped_input_gene_ids,
                    input_values,
                    input_pert_flags,
                    src_key_padding_mask=src_key_padding_mask,
                    CLS=False,
                    CCE=False,
                    MVC=False,
                    ECS=False,
                    do_sample=True,
                )
            output_values = output_dict["mlm_output"].float()
            pred_gene_values = torch.zeros_like(ori_gene_values)
            pred_gene_values[:, input_gene_ids] = output_values
        return pred_gene_values


def generate_square_subsequent_mask(sz: int) -> Tensor:
    """Generates an upper-triangular matrix of -inf, with zeros on diag."""
    return torch.triu(torch.ones(sz, sz) * float("-inf"), diagonal=1)


class GeneEncoder(nn.Module):
    def __init__(
        self,
        num_embeddings: int,
        embedding_dim: int,
        padding_idx: Optional[int] = None,
    ):
        super().__init__()
        self.embedding = nn.Embedding(
            num_embeddings, embedding_dim, padding_idx=padding_idx
        )
        self.enc_norm = nn.LayerNorm(embedding_dim)

    def forward(self, x: Tensor) -> Tensor:
        x = self.embedding(x)  # (batch, seq_len, embsize)
        x = self.enc_norm(x)
        return x

class GenePTEncoder(nn.Module):
    def __init__(
        self,
        num_embeddings: int,
        embedding_dim: int,
        padding_idx: Optional[int] = None,
        genept_lookup_embed: Optional = [],
        genept_embs_size = 1536
    ):
        super().__init__()
        self.embedding = nn.Embedding.from_pretrained(torch.from_numpy(genept_lookup_embed), freeze = False, padding_idx=padding_idx)
        self.fc = nn.Linear(genept_embs_size, embedding_dim, dtype = torch.double)
        self.enc_norm = nn.LayerNorm(embedding_dim)

    def forward(self, x: Tensor) -> Tensor:
        x = self.embedding(x)  # (batch, seq_len, embsize)
        x = self.fc(x).float()
        # x = self.enc_norm(x)
        return x

class AffineExprDecoder(nn.Module):
    def __init__(
        self,
        d_model: int,
        explicit_zero_prob: bool = False,
        activation: Optional[str] = None,
        tanh_coeff: bool = False,
        adaptive_bias: bool = False,
    ):
        """
        Predict the expression value of each gene in an affine like form of Ax + b.
        This decoder takes two ExprDecoder intrinsically to genrate the coefficient A and bias b.

        Args:
            d_model: The embedding dimension.
            explicit_zero_prob: If True, predict the probability of each gene being
                zero.
            activation: The activation function for the coefficient A and bias b.
            tanh_coeff: If True, use tanh activation for the coefficient A.
            adaptive_bias: If True, use a learnable bias for the bias b.
        """
        super().__init__()
        self.explicit_zero_prob = explicit_zero_prob
        self.tanh_coeff = tanh_coeff
        self.adaptive_bias = adaptive_bias
        self.coeff_decoder = ExprDecoder(d_model, explicit_zero_prob=explicit_zero_prob)
        self.bias_decoder = ExprDecoder(d_model, explicit_zero_prob=explicit_zero_prob)

        self.activation = activation
        if activation is not None:
            assert hasattr(nn, activation), f"Unknown activation: {activation}"
            self.activation = getattr(nn, activation)()

    def forward(self, x: Tensor, values: Tensor) -> Tensor:
        """
        Args:
            x: Tensor, shape [batch_size, seq_len, embsize]
            values: Tensor, shape [batch_size, seq_len]

        Returns:
            output Tensor of shape [batch_size, seq_len]
        """
        coeff = self.coeff_decoder(x)
        bias = self.bias_decoder(x)

        if self.activation is not None:
            coeff["pred"] = self.activation(coeff["pred"])
            bias["pred"] = self.activation(bias["pred"])

        # if self.tanh_coeff:
        #     coeff["pred"] = 1 + torch.tanh(coeff["pred"])

        if self.adaptive_bias:
            # bias["pred"] = bias["pred"] * values.mean(dim=1, keepdim=True)
            non_zero_value_mean = values.sum(dim=1, keepdim=True) / (values != 0).sum(
                dim=1, keepdim=True
            )
            bias["pred"] = bias["pred"] * non_zero_value_mean

        if self.explicit_zero_prob:
            return {
                "pred": coeff["pred"] * values + bias["pred"],
                "zero_probs": coeff["zero_probs"],
            }

        return dict(pred=coeff["pred"] * values + bias["pred"])


class TokenEmbedding(nn.Module):
    def __init__(
        self,
        num_embeddings: int,
        embedding_dim: int,
        padding_idx: Optional[int] = None,
        zero_out_idx: Optional[int] = None,
    ):
        """
        Generic token embedding module.

        Args:
            num_embeddings: The number of tokens.
            embedding_dim: The embedding dimension.
            padding_idx: The index of the padding token.
            zero_out_idx: Indicate if any idx embedding should be zero vector.
        """
        super().__init__()
        self.embedding = nn.Embedding(
            num_embeddings, embedding_dim, padding_idx=padding_idx
        )
        self.enc_norm = nn.LayerNorm(embedding_dim)

        self.zero_out_idx = zero_out_idx
        if zero_out_idx is not None:
            self._fill_idx_with_zero(zero_out_idx)
            zero_vector = self(zero_out_idx)
            assert torch.all(zero_vector == 0.0)
            assert not zero_vector.requires_grad

    def _fill_idx_with_zero(self, idx) -> None:
        with torch.no_grad():
            self.embedding.weight[idx].fill_(0)

    def forward(self, x: Tensor) -> Tensor:
        x = self.embedding(x)  # (batch, seq_len, embsize)
        x = self.enc_norm(x)
        return x


class PositionalEncoding(nn.Module):
    def __init__(self, d_model: int, dropout: float = 0.1, max_len: int = 5000):
        super().__init__()
        self.dropout = nn.Dropout(p=dropout)

        position = torch.arange(max_len).unsqueeze(1)
        div_term = torch.exp(
            torch.arange(0, d_model, 2) * (-math.log(10000.0) / d_model)
        )
        pe = torch.zeros(max_len, 1, d_model)
        pe[:, 0, 0::2] = torch.sin(position * div_term)
        pe[:, 0, 1::2] = torch.cos(position * div_term)
        self.register_buffer("pe", pe)

    def forward(self, x: Tensor) -> Tensor:
        """
        Args:
            x: Tensor, shape [seq_len, batch_size, embedding_dim]
        """
        x = x + self.pe[: x.size(0)]
        return self.dropout(x)


class Similarity(nn.Module):
    """
    Dot product or cosine similarity
    """

    def __init__(self, temp):
        super().__init__()
        self.temp = temp
        self.cos = nn.CosineSimilarity(dim=-1)

    def forward(self, x, y):
        return self.cos(x, y) / self.temp


class ClsDecoder(nn.Module):
    """
    Decoder for classification task.
    """

    def __init__(
        self,
        d_model: int,
        n_cls: int,
        nlayers: int = 3,
        activation: callable = nn.ReLU,
    ):
        super().__init__()
        # module list
        self._decoder = nn.ModuleList()
        for i in range(nlayers - 1):
            self._decoder.append(nn.Linear(d_model, d_model))
            self._decoder.append(activation())
            self._decoder.append(nn.LayerNorm(d_model))
        self.out_layer = nn.Linear(d_model, n_cls)

    def forward(self, x: Tensor) -> Tensor:
        """
        Args:
            x: Tensor, shape [batch_size, embsize]
        """
        for layer in self._decoder:
            x = layer(x)
        return self.out_layer(x)