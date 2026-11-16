"""Qwen3.5 training memory fix for the 16GB mini.

In training mode mlx_lm's GatedDeltaNet drops its Metal kernel for a pure-ops
path (the kernel has no vjp). That path ran in ALL 24 linear-attention layers,
peaking at ~12 GB at 512 tokens, so the box paged and each step took ~30 s.
Only the LoRA layers (the last N) need gradients; the frozen layers before them
can keep the fast kernel. Also: small MLX buffer cache, no forced wired limit.
"""
import mlx.core as mx
import mlx_lm.models.qwen3_5 as q
import mlx_lm.tuner.trainer as trainer

TRAIN_LAYERS = 8
mx.set_cache_limit(256 * 1024 * 1024)
trainer.maybe_set_recommended_wired_limit = lambda: None
_init = q.DecoderLayer.__init__


def _patched_init(self, args, layer_idx):
    _init(self, args, layer_idx)
    if self.is_linear and layer_idx < args.num_hidden_layers - TRAIN_LAYERS:
        self.linear_attn.frozen_kernel = True


q.DecoderLayer.__init__ = _patched_init
q.GatedDeltaNet.training = property(lambda self: self._training and not getattr(self, "frozen_kernel", False))
