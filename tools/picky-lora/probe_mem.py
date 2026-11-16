"""Measure peak MLX memory and seconds per training step for a given layers/seq."""
import json, sys, time
import lowmem
import mlx.core as mx, mlx.nn as nn, mlx.optimizers as optim
from mlx_lm import load
from mlx_lm.tuner.utils import linear_to_lora_layers
from mlx_lm.tuner.trainer import default_loss, grad_checkpoint

layers, seq = int(sys.argv[1]), int(sys.argv[2])
mx.set_cache_limit(256 << 20)
model, tok = load("/Users/Agent/picky-lora/base")
model.freeze(); model.train()
linear_to_lora_layers(model, layers, {"rank": 8, "scale": 20.0, "dropout": 0.0})
grad_checkpoint(model.layers[0])
ex = json.loads(open("/Users/Agent/picky-lora/data/mlx/train.jsonl").readline())["messages"]
ids = tok.apply_chat_template(ex, tokenize=True)
ids = (ids * 3)[:seq]
x = mx.array([ids]); lengths = mx.array([[0, len(ids)]])
opt = optim.Adam(learning_rate=1e-5)
vg = nn.value_and_grad(model, default_loss)
mx.reset_peak_memory()
for i in range(3):
    t = time.time()
    (loss, n), g = vg(model, x, lengths)
    opt.update(model, g); mx.eval(model.parameters(), opt.state, loss)
    print(f"layers={layers} seq={len(ids)} step {i}: {time.time()-t:.1f}s peak={mx.get_peak_memory()/1e9:.2f}GB active={mx.get_active_memory()/1e9:.2f}GB", flush=True)
