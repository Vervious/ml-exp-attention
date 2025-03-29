#
# For licensing see accompanying LICENSE file.
# Copyright (C) 2024 Apple Inc. All Rights Reserved.
#
"""FlashSigmoid attention layers."""

import functools
import typing as t

import torch
import torch.nn.functional as F
from torch import nn

# Local imports
from attention_simulator.layers.position_embedding import apply_rotary_emb

# Attempt to load sigmoid flash sigmoid attention if it exists.
try:
    from flash_exp import flash_attn_func as flash_attn_sigmoid_func
    from flash_exp import flash_attn_qkvpacked_func as flash_attn_sigmoid_qkvpacked_func
except ImportError:
    pass



def flash2_sigmoid_attn(
    q: torch.Tensor,
    k: torch.Tensor,
    v: torch.Tensor,
    sigmoid_bias: float,
    attn_drop: float = 0.0,
    window_size: t.Tuple = (-1, -1),
    alibi_slopes: t.Optional[torch.Tensor] = None,
    causal: bool = False,
    prescale: bool = True,
):
    """Unfused attn helper.

    :param q: (batch_size, seqlen, nheads_q, headdim)
    :param k: (batch_size, seqlen, nheads_kv, headdim)
    :param v: (batch_size, seqlen, nheads_kv, headdim)
    :param sigmoid_bias: Bias to add to sigmoid attention logits.
    :param attn_drop: prob of attn dropout
    :param window_size: If > -1 then apply local window attention.
    :param alibi_slopes: (nheads,) or (batch_size, nheads), fp32. A bias of
        (-alibi_slope * |i + seqlen_k - seqlen_q - j|)
        is added to the attention score of query i and key j.
    :param causal: apply causal masking if true.
    :param prescale: If `True`, apply scaling to logits before dot-product attention.

    """
    scale_factor = q.size(-1) ** (-0.25)

    out = flash_attn_sigmoid_func(
        q=q * scale_factor if prescale else q,
        k=k * scale_factor if prescale else k,
        v=v,
        softmax_scale=1.0 if prescale else None,
        dropout_p=attn_drop,
        window_size=window_size,
        alibi_slopes=alibi_slopes,
        causal=causal,
        sigmoid_bias=sigmoid_bias,
    ).flatten(
        2
    )  # (batch_size, seqlen, nheads * headdim)
    return out


def fused_sigmoid_flash2_attn(
    qkv: torch.Tensor,
    sigmoid_bias: float,
    attn_drop: float = 0.0,
    window_size: t.Tuple = (-1, -1),
    alibi_slopes: t.Optional[torch.Tensor] = None,
    causal: bool = True,
    prescale: bool = True,
):
    """Fused attn helper.

    :param qkv: (batch_size, seqlen, 3, nheads, headdim)
    :param sigmoid_bias: Bias to add to sigmoid attention logits.
    :param attn_drop: Prob of attn dropout
    :param window_size: If > -1 then apply local window attention.
    :param alibi_slopes: (nheads,) or (batch_size, nheads), fp32. A bias of
        (-alibi_slope * |i + seqlen_k - seqlen_q - j|)
        is added to the attention score of query i and key j.
    :param causal: apply causal masking if true.
    :param prescale: If `True`, apply scaling to logits before dot-product attention.

    """
    scale_factor = torch.ones([1, 1, 3, 1, 1], dtype=qkv.dtype, device=qkv.device)
    scale_factor[:, :, :-1] *= qkv.size(-1) ** (-0.25)

    out = flash_attn_sigmoid_qkvpacked_func(
        qkv=qkv * scale_factor if prescale else qkv,
        softmax_scale=1.0 if prescale else None,
        dropout_p=attn_drop,
        causal=causal,
        window_size=window_size,
        alibi_slopes=alibi_slopes,
        sigmoid_bias=sigmoid_bias,
    ).flatten(2)
    return out  # (batch_size, seqlen, nheads * headdim)


class FlashSigmoidAttention(nn.Module):
    """SigmoidAttention layer."""

    def __init__(
        self,
        dim: int,
        sigmoid_bias: float,
        causal: bool = True,
        num_heads: int = 8,
        bias: bool = False,
        qk_norm: bool = False,
        attn_drop: float = 0.0,
        proj_drop: float = 0.0,
        window_size: t.Tuple = (-1, -1),
        alibi_slopes: t.Optional[torch.Tensor] = None,
        norm_layer: t.Callable = nn.LayerNorm,
        which_linear: t.Callable = nn.Linear,
        qk_transform: t.Optional[t.Callable] = None,
        which_flash_attn: t.Callable = flash2_sigmoid_attn,
    ):
        """Flash attention layer.

        :param dim: Dimensionality of the input and output embeddings.
        :param sigmoid_bias: Bias to add to sigmoid attention logits.
        :param causal: Apply causal attention.
        :param num_heads: Number of attention heads.
        :param bias: If True, add a learnable bias to query, key, value and proj.
        :param qk_norm: QK norm from https://arxiv.org/abs/2302.05442
        :param attn_drop: Dropout probability for the attention matrix.
        :param proj_drop: Dropout probability for the output projection.
        :param window_size: Tuple describing window attention (if > -1).
        :param alibi_slopes: (nheads,) or (batch_size, nheads), fp32. A bias of
            (-alibi_slope * |i + seqlen_k - seqlen_q - j|)
            is added to the attention score of query i and key j.
        :param norm_layer: Normalization layer callable.
        :param which_linear: Linear layer type to use.
        :param qk_transform: Transform for xf(q), xf(k) -- eg: RoPE.
        :param which_flash_attn: Which flash attention function to call.

        """
        super().__init__()
        assert dim % num_heads == 0, "dim should be divisible by num_heads"
        self.causal = causal
        self.num_heads = num_heads
        self.qk_norm = qk_norm
        self.window_size = window_size
        self.alibi_slopes = alibi_slopes
        self.head_dim = dim // num_heads
        self.sigmoid_bias = sigmoid_bias

        # Swap between flash and torch F.SDPA
        self.which_flash_attn = which_flash_attn

        # These functions can be used to modulate {q, k}.
        self.qk_transform = qk_transform

        # Layers to handle projections and normalization.
        self.qkv = which_linear(dim, dim * 3, bias=bias)
        self.q_norm = norm_layer(self.head_dim) if qk_norm else nn.Identity()
        self.k_norm = norm_layer(self.head_dim) if qk_norm else nn.Identity()
        self.proj = which_linear(dim, dim, bias=bias)

        # Optional dropout layers.
        self.attn_drop = attn_drop  # Handled inside Flash.
        self.proj_drop = nn.Dropout(proj_drop)

    def extra_repr(self) -> str:
        """Extra repr for the class."""
        return (
            f"causal={self.causal}, \n"
            f"num_heads={self.num_heads}, \n"
            f"head_dim={self.head_dim}, \n"
            f"window_size={self.window_size} \n"
            f"alibi_slopes={self.alibi_slopes}, \n"
            f"which_flash_attn={self.which_flash_attn} \n"
            f"qk_transform={self.qk_transform}, \n"
            f"sigmoid_bias={self.sigmoid_bias}"
        )

    def forward(
        self,
        x: torch.Tensor,
        **unused_kwargs,
    ) -> torch.Tensor:
        output = {}  # house all results

        B, N, C = x.shape
        qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads)
        q, k, v = qkv.unbind(2)

        # QK norm.
        q = self.q_norm(q)
        k = self.k_norm(k)

        # Apply an optional function on Q and K (eg: RoPE).
        if self.qk_transform is not None:
            q, k = self.qk_transform(q, k, q_pos_offset=0, k_pos_offset=0)

        # Apply attention to V and project back using a linear layer.
        attn_times_v = self.which_flash_attn(
            q=q.to(v.dtype),
            k=k.to(v.dtype),
            v=v,
            attn_drop=self.attn_drop,
            causal=self.causal,
            window_size=self.window_size,
            alibi_slopes=self.alibi_slopes,
            sigmoid_bias=self.sigmoid_bias,
        )

        final_proj_output = self.proj_drop(self.proj(attn_times_v))

        # Return what is possible for analysis.
        output.update(
            {
                "attn_times_v": attn_times_v,
                "attn_proj": final_proj_output,
            }
        )
        return output


class FlashSigmoidCrossAttention(nn.Module):
    """A cross-attention Flash implementation."""

    def __init__(
        self,
        q_dim: int,
        kv_dim: int,
        sigmoid_bias: float,
        causal: bool = True,
        num_heads: int = 8,
        bias: bool = False,
        qk_norm: bool = False,
        attn_drop: float = 0.0,
        proj_drop: float = 0.0,
        window_size: t.Tuple = (-1, -1),
        alibi_slopes: t.Optional[torch.Tensor] = None,
        norm_layer: t.Callable = nn.LayerNorm,
        which_linear: t.Callable = nn.Linear,
        qk_transform: t.Optional[t.Callable] = None,
        which_flash_attn: t.Callable = flash2_sigmoid_attn,
    ):
        """Cross attention layer.

        :param q_dim: Dimensionality of the Q tensor.
        :param kv_dim: Dimensionality of the {K, V} tensors.
        :param sigmoid_bias: Bias to add to sigmoid attention logits.
        :param causal: Apply causal attention.
        :param num_heads: Number of attention heads.
        :param bias: If True, add a learnable bias to query, key, value and proj.
        :param qk_norm: QK norm from https://arxiv.org/abs/2302.05442
        :param attn_drop: Dropout probability for the attention matrix.
        :param proj_drop: Dropout probability for the output projection.
        :param window_size: Tuple describing window attention (if > -1).
        :param alibi_slopes: (nheads,) or (batch_size, nheads), fp32. A bias of
            (-alibi_slope * |i + seqlen_k - seqlen_q - j|)
            is added to the attention score of query i and key j.
        :param norm_layer: Normalization layer callable.
        :param which_linear: Linear layer type to use.
        :param qk_transform: Transform for xf(q), xf(k) -- eg: RoPE.
        :param which_flash_attn: Which flash attention function to call.

        """
        super().__init__()
        assert q_dim % num_heads == 0 and kv_dim % num_heads == 0, "Dimensions should be divisible by num_heads"
        self.causal = causal
        self.num_heads = num_heads
        self.qk_norm = qk_norm
        self.window_size = window_size
        self.alibi_slopes = alibi_slopes
        self.head_dim_q = q_dim // num_heads
        self.head_dim_kv = kv_dim // num_heads
        self.sigmoid_bias = sigmoid_bias

        # Swap between flash and torch F.SDPA
        self.which_flash_attn = which_flash_attn

        # These functions can be used to modulate {q, k}.
        self.qk_transform = qk_transform

        # Layers to handle projections and normalization.
        self.q_proj = which_linear(q_dim, q_dim, bias=bias)
        self.kv_proj = which_linear(kv_dim, kv_dim * 2, bias=bias)
        self.q_norm = norm_layer(self.head_dim_q) if qk_norm else nn.Identity()
        self.k_norm = norm_layer(self.head_dim_kv) if qk_norm else nn.Identity()
        self.v_norm = norm_layer(self.head_dim_kv)  # Always norm on V.
        self.proj = which_linear(kv_dim, q_dim, bias=bias)

        # Optional dropout layers.
        self.attn_drop = attn_drop  # Handled inside Flash.
        self.proj_drop = nn.Dropout(proj_drop)

    def extra_repr(self) -> str:
        """Extra repr for the class."""
        return (
            f"causal={self.causal}, \n"
            f"num_heads={self.num_heads}, \n"
            f"head_dim_q={self.head_dim_q}, \n"
            f"head_dim_kv={self.head_dim_kv}, \n"
            f"window_size={self.window_size} \n"
            f"alibi_slopes={self.alibi_slopes}, \n"
            f"which_flash_attn={self.which_flash_attn} \n"
            f"qk_transform={self.qk_transform}, \n"
            f"sigmoid_bias={self.sigmoid_bias}"
        )

    def forward(
        self,
        q: torch.Tensor,
        kv: torch.Tensor,
        **unused_kwargs,
    ) -> torch.Tensor:
        output = {}  # house all results

        q_shape = q.shape
        kv_shape = kv.shape

        q = self.q_proj(q).reshape(q_shape[0], q_shape[1], self.num_heads, self.head_dim_q)
        kv = self.kv_proj(kv).reshape(kv_shape[0], kv_shape[1], 2, self.num_heads, self.head_dim_kv)
        k, v = kv.unbind(2)

        q = self.q_norm(q)
        k = self.k_norm(k)
        v = self.v_norm(v)

        # Apply an optional function on Q and K (eg: RoPE).
        if self.qk_transform is not None:
            q, k = self.qk_transform(q, k, q_pos_offset=0, k_pos_offset=0)

        # Force the dtype back.
        q = q.to(v.dtype)
        k = k.to(v.dtype)

        # Apply attention to V and project back using a linear layer.
        attn_times_v = self.which_flash_attn(
            q=q,
            k=k,
            v=v,
            attn_drop=self.attn_drop,
            causal=self.causal,
            window_size=self.window_size,
            alibi_slopes=self.alibi_slopes,
            sigmoid_bias=self.sigmoid_bias,
        )
        final_proj_output = self.proj_drop(self.proj(attn_times_v))

        output.update(
            {
                "attn_times_v": attn_times_v,
                "attn_proj": final_proj_output,
            }
        )
        return output


RoPEFlashSigmoidAttention = functools.partial(FlashSigmoidAttention, qk_transform=apply_rotary_emb)
RoPEFlashSigmoidCrossAttention = functools.partial(FlashSigmoidCrossAttention, qk_transform=apply_rotary_emb)
