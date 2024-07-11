from typing import *

import torch
import torch.nn as nn

from diffusers.models.autoencoders.vae import VectorQuantizer
from diffusers.configuration_utils import register_to_config, ConfigMixin
from diffusers.models.modeling_utils import ModelMixin
from diffusers.utils.accelerate_utils import apply_forward_hook

from .vae import Encoder, Decoder
from .conditional_vae import ConditionalEncoder, ConditionalDecoder


class CompressiveVQModel(ModelMixin, ConfigMixin):

    @register_to_config
    def __init__(
        self,
        in_channels: int = 3,
        out_channels: int = 3,
        down_block_types: Tuple[str, ...] = ("DownEncoderBlock2D",),
        up_block_types: Tuple[str, ...] = ("UpDecoderBlock2D",),
        block_out_channels: Tuple[int, ...] = (64,),
        layers_per_block: int = 1,
        act_fn: str = "silu",
        latent_channels: int = 3,
        sample_size: int = 32,
        num_vq_embeddings: int = 256,
        norm_num_groups: int = 32,
        vq_embed_dim: Optional[int] = None,
        scaling_factor: float = 0.18215,
        norm_type: str = "group",  # group, spatial
        mid_block_add_attention=True,
        lookup_from_codebook=False,
        force_upcast=False,
        num_dyn_embeddings: int = 256,
        context_length: int = 1,
        max_att_resolution=32,
        resolution=256,
        patch_size=4,
    ):
        super().__init__()
        self.latent_channels = latent_channels
        self.dyna_latent_channels = latent_channels
        self.context_length = context_length
        self.num_vq_embeddings = num_vq_embeddings
        self.num_dyn_embeddings = num_dyn_embeddings
        self.patch_size = patch_size

        # encoders
        self.cond_encoder = ConditionalEncoder(
            in_channels=in_channels,
            out_channels=self.dyna_latent_channels,
            down_block_types=down_block_types,
            block_out_channels=block_out_channels,
            layers_per_block=layers_per_block,
            act_fn=act_fn,
            norm_num_groups=norm_num_groups,
            double_z=False,
            mid_block_add_attention=True,
            max_att_resolution=max_att_resolution,
            init_resolution=resolution,
            context_length=context_length,
        )

        self.encoder = Encoder(
            in_channels=in_channels,
            out_channels=latent_channels,
            down_block_types=down_block_types,
            block_out_channels=block_out_channels,
            layers_per_block=layers_per_block,
            act_fn=act_fn,
            norm_num_groups=norm_num_groups,
            double_z=False,
            mid_block_add_attention=mid_block_add_attention,
        )

        # vector quantization
        vq_embed_dim = vq_embed_dim if vq_embed_dim is not None else latent_channels
        self.vq_embed_dim = vq_embed_dim

        self.quant_conv = nn.Conv2d(latent_channels, vq_embed_dim, 1)
        self.quantize = VectorQuantizer(
            num_vq_embeddings,
            vq_embed_dim,
            beta=1.0,
            # beta=0.25,
            remap=None,
            sane_index_shape=False,
            legacy=False,
        )
        self.post_quant_conv = nn.Conv2d(vq_embed_dim, latent_channels, 1)

        self.quant_linear = nn.Linear(self.dyna_latent_channels * self.patch_size * self.patch_size, vq_embed_dim)

        self.dynamics_quantize = VectorQuantizer(
            num_dyn_embeddings,
            vq_embed_dim,
            beta=1.0,
            # beta=0.25,
            remap=None,
            sane_index_shape=False,
            legacy=False,
        )
        self.post_quant_linear = nn.Linear(vq_embed_dim, self.dyna_latent_channels * self.patch_size * self.patch_size)

        # decoders
        self.cond_decoder = ConditionalDecoder(
            in_channels=self.dyna_latent_channels,
            out_channels=out_channels,
            up_block_types=up_block_types,
            block_out_channels=block_out_channels,
            layers_per_block=layers_per_block,
            act_fn=act_fn,
            norm_num_groups=norm_num_groups,
            norm_type=norm_type,
            mid_block_add_attention=True,
            max_att_resolution=max_att_resolution,
            init_resolution=16,  # TODO: magic number
            context_length=context_length,
        )

        self.decoder = Decoder(
            in_channels=latent_channels,
            out_channels=out_channels,
            up_block_types=up_block_types,
            block_out_channels=block_out_channels,
            layers_per_block=layers_per_block,
            act_fn=act_fn,
            norm_num_groups=norm_num_groups,
            norm_type=norm_type,
            mid_block_add_attention=mid_block_add_attention,
        )

    @apply_forward_hook
    def tokenize(self, pixel_values: torch.FloatTensor, context_length: int = 0):
        assert context_length == self.context_length  # TODO: fix

        B, T, C, H, W = pixel_values.shape

        context_frames = pixel_values[:, :context_length].reshape(-1, C, H, W)
        future_frames = pixel_values[:, context_length:].reshape(-1, C, H, W)
        future_length = T - context_length

        # encode context frames
        h, cond_features = self.encoder(context_frames, return_features=True)
        if self.context_length > 1:
            B = future_frames.shape[0] // future_length
            cond_features = [
                f.reshape(B, self.context_length, *f.shape[-3:]).unsqueeze(1)
                .repeat(1, future_length, 1, 1, 1, 1).reshape(-1, self.context_length, *f.shape[-3:])
                for f in cond_features
            ]  # B*(T-t), t, C, H, W
        else:
            cond_features = [
                f.unsqueeze(1).repeat(1, future_length, 1, 1, 1).reshape(-1, *f.shape[-3:])
                for f in cond_features
            ]
        h = self.quant_conv(h)
        

        # encode future frames conditioned on context
        d = self.cond_encoder(future_frames, cond_features)
        p = self.patch_size
        d = d.permute(0, 2, 3, 1).unfold(1, p, p).unfold(2, p, p).permute(
            0, 1, 2, 4, 5, 3)  # patchify: [B, H/P, W/P, P, P, C]
        d = d.reshape(d.shape[0], d.shape[1] * d.shape[2], -1)
        d = self.quant_linear(d)

        # quantize
        quant, commit_loss, info = self.quantize(h)

        d = d.transpose(-1, -2).unsqueeze(-1)  # [B, L, D] => [B, D, L, 1]
        quant_d, dyn_commit_loss, info_d = self.dynamics_quantize(d)

        # flatten into tokens
        indices_c = info[2].reshape(B, context_length, -1)
        print(indices_c.shape)
        scf_token = self.num_vq_embeddings + self.num_dyn_embeddings
        scf_tokens = torch.ones(B, context_length, 1).to(indices_c.device, indices_c.dtype) * scf_token
        indices_c = torch.cat([scf_tokens, indices_c], dim=2).reshape(B, -1)[:, 1:]
        print(scf_tokens.shape)
        print(indices_c.shape)

        indices_d = info_d[2].reshape(B, future_length, -1) + self.num_vq_embeddings
        sdf_token = self.num_vq_embeddings + self.num_dyn_embeddings + 1
        sdf_tokens = torch.ones(B, future_length, 1).to(indices_d.device, indices_d.dtype) * sdf_token
        indices_d = torch.cat([sdf_tokens, indices_d], dim=2).reshape(B, -1)

        indices = torch.cat([indices_c, indices_d], dim=1)
        labels = torch.cat([
            torch.ones(B, indices_c.shape[1] + 1).to(indices.device, indices.dtype) * -100,  # -100 for no loss
            indices_d[:, 1:]], dim=1)

        return indices

    @apply_forward_hook
    def detokenize(self, indices, context_length: int = 0):
        assert context_length == self.context_length  # TODO: fix
        ctx_res = 16  # TODO: magic number
        dyn_res = 4
        B = indices.shape[0]

        # extract embeddings
        assert (indices.shape[1] + 1 - (1 + ctx_res * ctx_res) * context_length) % (1 + dyn_res * dyn_res) == 0
        future_length = (indices.shape[1] + 1 - (1 + ctx_res * ctx_res) * context_length) // (1 + dyn_res * dyn_res)
        indices = torch.cat([torch.ones(B, 1).to(indices.device, indices.dtype), indices], dim=1)  # concat dummy tokens
        num_context_tokens = context_length * (1 + ctx_res * ctx_res)
        indices_c = indices[:, :num_context_tokens].reshape(B, context_length, -1)[:, :, 1:].reshape(B, -1)
        indices_d = indices[:, num_context_tokens:].reshape(B, future_length, -1)[:, :, 1:].reshape(B, -1)
        indices_d = (indices_d - self.num_vq_embeddings).clamp(min=0, max=self.num_dyn_embeddings - 1)

        quant = self.quantize.embedding(indices_c)
        quant = quant.reshape(B * context_length, ctx_res, ctx_res, self.vq_embed_dim).permute(0, 3, 1,
                                                                                               2)  # [B, D, H, W]
        quant2 = self.post_quant_conv(quant)

        quant_d = self.dynamics_quantize.embedding(indices_d)
        quant_d = quant_d.reshape(-1, dyn_res * dyn_res, self.vq_embed_dim)  # [B, L, D]
        quant2_d = self.post_quant_linear(quant_d)

        h, w, p, c = quant2.shape[-1], quant2.shape[-1], self.patch_size, self.dyna_latent_channels
        quant2_d = torch.reshape(quant2_d, [quant2_d.shape[0], h // p, w // p, p, p, c])
        quant2_d = torch.einsum("nhwpqc->nchpwq", quant2_d)  # de-patchify
        quant2_d = torch.reshape(quant2_d, [quant2_d.shape[0], c, h, w])

        # decode context frames
        context_dec, cond_features = self.decoder(quant2, return_features=True)
        if context_length > 1:
            B = quant2_d.shape[0] // future_length
            cond_features = [
                f.reshape(B, context_length, *f.shape[-3:]).unsqueeze(1)
                .repeat(1, future_length, 1, 1, 1, 1).reshape(-1, context_length, *f.shape[-3:])
                for f in cond_features
            ]  # B*(T-t), t, C, H, W
        else:
            cond_features = [f.unsqueeze(1).repeat(1, future_length, 1, 1, 1).reshape(-1, *f.shape[-3:]) for f in
                             cond_features]

        # decode future frames conditioned on context
        dec = self.cond_decoder(quant2_d, cond_features)

        context_dec = context_dec.reshape(B, context_length, *context_dec.shape[-3:])
        dec = dec.reshape(B, future_length, *dec.shape[-3:])

        return torch.cat([context_dec, dec], dim=1)
