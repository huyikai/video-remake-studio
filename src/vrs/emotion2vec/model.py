"""emotion2vec encoder + classification head. Vendored from FunASR (MIT)."""

from __future__ import annotations

from functools import partial

import numpy as np
import torch
import torch.nn.functional as F

from vrs.emotion2vec.audio import AudioEncoder
from vrs.emotion2vec.modules import AltBlock


class Emotion2vec(torch.nn.Module):
    def __init__(self, model_conf, *, vocab_size: int = -1) -> None:
        super().__init__()
        cfg = model_conf
        self.cfg = cfg

        make_layer_norm = partial(
            torch.nn.LayerNorm,
            eps=cfg.get("norm_eps"),
            elementwise_affine=cfg.get("norm_affine"),
        )

        def make_block(drop_path, dim=None, heads=None):
            return AltBlock(
                cfg.get("embed_dim") if dim is None else dim,
                cfg.get("num_heads") if heads is None else heads,
                cfg.get("mlp_ratio"),
                qkv_bias=True,
                drop=cfg.get("encoder_dropout"),
                attn_drop=cfg.get("attention_dropout"),
                mlp_drop=cfg.get("activation_dropout"),
                post_mlp_drop=cfg.get("post_mlp_drop"),
                drop_path=drop_path,
                norm_layer=make_layer_norm,
                layer_norm_first=cfg.get("layer_norm_first"),
                ffn_targets=not cfg.get("end_of_block_targets"),
            )

        self.alibi_biases = {}
        self.modality_encoders = torch.nn.ModuleDict()
        self.modality_encoders["AUDIO"] = AudioEncoder(
            cfg.modalities.audio,
            cfg.get("embed_dim"),
            make_block,
            make_layer_norm,
            cfg.get("layer_norm_first"),
            self.alibi_biases,
        )
        self.ema = None
        self.average_top_k_layers = cfg.get("average_top_k_layers")
        self.loss_beta = cfg.get("loss_beta")
        self.loss_scale = cfg.get("loss_scale")
        self.dropout_input = torch.nn.Dropout(cfg.get("dropout_input"))
        dpr = np.linspace(
            cfg.get("start_drop_path_rate"),
            cfg.get("end_drop_path_rate"),
            cfg.get("depth"),
        )
        self.blocks = torch.nn.ModuleList([make_block(dpr[i]) for i in range(cfg.get("depth"))])
        self.norm = None
        if cfg.get("layer_norm_first"):
            self.norm = make_layer_norm(cfg.get("embed_dim"))
        self.proj = None
        if vocab_size > 0:
            self.proj = torch.nn.Linear(cfg.get("embed_dim"), vocab_size)

    def forward(
        self,
        source,
        padding_mask=None,
        mask=True,
        features_only=False,
        force_remove_masked=False,
        remove_extra_tokens=True,
        precomputed_mask=None,
        **_kwargs,
    ):
        feature_extractor = self.modality_encoders["AUDIO"]
        extractor_out = feature_extractor(
            source,
            padding_mask,
            mask,
            remove_masked=not features_only or force_remove_masked,
            clone_batch=self.cfg.get("clone_batch") if not features_only else 1,
            mask_seeds=None,
            precomputed_mask=precomputed_mask,
        )
        x = extractor_out["x"]
        encoder_mask = extractor_out["encoder_mask"]
        masked_padding_mask = extractor_out["padding_mask"]
        masked_alibi_bias = extractor_out.get("alibi_bias", None)
        alibi_scale = extractor_out.get("alibi_scale", None)
        if self.dropout_input is not None:
            x = self.dropout_input(x)
        layer_results = []
        for i, blk in enumerate(self.blocks):
            ab = masked_alibi_bias
            if ab is not None and alibi_scale is not None:
                scale = alibi_scale[i] if alibi_scale.size(0) > 1 else alibi_scale.squeeze(0)
                ab = ab * scale.type_as(ab)
            x, lr = blk(x, padding_mask=masked_padding_mask, alibi_bias=ab)
            if features_only:
                layer_results.append(lr)
        if self.norm is not None:
            x = self.norm(x)
        if features_only:
            if remove_extra_tokens:
                x = x[:, feature_extractor.modality_cfg.num_extra_tokens :]
                if masked_padding_mask is not None:
                    masked_padding_mask = masked_padding_mask[
                        :, feature_extractor.modality_cfg.num_extra_tokens :
                    ]
            return {
                "x": x,
                "padding_mask": masked_padding_mask,
                "layer_results": layer_results,
                "mask": encoder_mask,
            }
        return x

    def extract_features(self, source, padding_mask=None, mask=False, remove_extra_tokens=True):
        return self.forward(
            source,
            padding_mask=padding_mask,
            mask=mask,
            features_only=True,
            remove_extra_tokens=remove_extra_tokens,
        )

    def classify_utterance(self, waveform: torch.Tensor) -> torch.Tensor:
        """16 kHz mono waveform → class probabilities, shape [C]."""
        source = waveform
        if source.ndim > 1:
            source = source.reshape(-1)
        if self.cfg.normalize:
            source = F.layer_norm(source, source.shape)
        source = source.view(1, -1)
        feats = self.extract_features(source, padding_mask=None)
        if self.proj is None:
            raise RuntimeError("emotion2vec 权重没有分类头 proj")
        logits = self.proj(feats["x"].mean(dim=1))
        return torch.softmax(logits, dim=-1)[0]
