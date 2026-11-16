"""MLX LoRA adapter -> PEFT -> GGUF LoRA -> merged into live Picky's Q4_K_M GGUF -> Ollama model.

No full-precision fuse: only the 62 LoRA'd tensors are rewritten (as Q8_0); peak extra disk
~13 GB while Ollama imports, ~7.5 GB after.
Usage: to_ollama.py <adapters.safetensors> <ollama-model-name>
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import mlx.core as mx
import numpy as np
from safetensors.numpy import save_file

ROOT = Path(os.environ.get("PICKY_LORA_HOME", "/Users/Agent/picky-lora"))
BASE_OLLAMA = "qwen3.5:9b-q4_K_M"
src, name = Path(sys.argv[1]), sys.argv[2]
cfg = json.loads((src.parent / "adapter_config.json").read_text())
lp = cfg["lora_parameters"]
work = ROOT / "export" / name.replace(":", "_")
peft, base_cfg = work / "peft", work / "base_cfg"
for d in (peft, base_cfg):
    d.mkdir(parents=True, exist_ok=True)

# 1) PEFT safetensors: mlx lora_a (in, r) / lora_b (r, out) -> lora_A (r, in) / lora_B (out, r)
out, targets = {}, set()
for k, v in mx.load(str(src)).items():
    hf = k.replace("language_model.model.", "base_model.model.model.language_model.", 1)
    arr = np.array(v.astype(mx.float32)).T.copy()
    if k.endswith(".lora_a"):
        out[hf[: -len(".lora_a")] + ".lora_A.weight"] = arr
    elif k.endswith(".lora_b"):
        out[hf[: -len(".lora_b")] + ".lora_B.weight"] = arr
    targets.add(k.rsplit(".", 2)[-2])
save_file(out, str(peft / "adapter_model.safetensors"))
(peft / "adapter_config.json").write_text(json.dumps({
    "peft_type": "LORA", "task_type": "CAUSAL_LM", "base_model_name_or_path": "Qwen/Qwen3.5-9B",
    "r": lp["rank"], "lora_alpha": lp["scale"] * lp["rank"], "lora_dropout": 0.0, "bias": "none",
    "target_modules": sorted(targets), "fan_in_fan_out": False}, indent=1))

# 2) base config for the converter: the MLX config without its quantization block, + tokenizer
c = json.loads((ROOT / "base/config.json").read_text())
for q in ("quantization", "quantization_config"):
    c.pop(q, None)
(base_cfg / "config.json").write_text(json.dumps(c, indent=1))
for f in ("tokenizer.json", "tokenizer_config.json", "vocab.json", "chat_template.jinja"):
    if (ROOT / "base" / f).exists():
        shutil.copy(ROOT / "base" / f, base_cfg / f)

# 3) GGUF LoRA
lora_gguf = work / "picky-lora.gguf"
subprocess.run([sys.executable, str(ROOT / "llama.cpp/convert_lora_to_gguf.py"), str(peft), "--base", str(base_cfg),
                "--outtype", "f16", "--outfile", str(lora_gguf)], check=True)

if os.environ.get("NO_OLLAMA"):
    print("wrote", lora_gguf)
    sys.exit(0)

# 4) Merge into live Picky's GGUF. Ollama 0.40 dropped ADAPTER ("LoRA adapters are no
#    longer supported"), so: the 62 LoRA'd tensors are dequantized, get scale*B@A, and
#    are written as Q8_0 (gguf-py can't write Q4_K/Q6_K; Q8_0 adds ~0.9 GB but loses less);
#    every other tensor and all metadata are copied byte for byte.
import gguf  # noqa: E402
from gguf import quants  # noqa: E402

mf = subprocess.run(["ollama", "show", BASE_OLLAMA, "--modelfile"], capture_output=True, text=True, check=True).stdout
froms = [ln.split(None, 1)[1] for ln in mf.splitlines() if ln.startswith("FROM ")]
base = gguf.GGUFReader(froms[0])
lora = gguf.GGUFReader(str(lora_gguf))
alpha = float(lora.fields["adapter.lora.alpha"].parts[lora.fields["adapter.lora.alpha"].data[0]][0])
scale = alpha / lp["rank"]
L = {t.name: np.array(t.data, dtype=np.float32) for t in lora.tensors}
merged = work / "merged.gguf"
arch = bytes(base.fields["general.architecture"].parts[base.fields["general.architecture"].data[0]]).decode()
w = gguf.GGUFWriter(str(merged), arch)
for f in base.fields.values():
    if f.name == gguf.Keys.General.ARCHITECTURE or f.name.startswith("GGUF."):
        continue
    vt = f.types[0]
    w.add_key_value(f.name, f.contents(), vt, sub_type=f.types[-1] if vt == gguf.GGUFValueType.ARRAY else None)
out_t, n_merged = [], 0
for t in base.tensors:
    a_key, b_key = t.name + ".lora_a", t.name + ".lora_b"
    if a_key in L:
        W = quants.dequantize(t.data, t.tensor_type).reshape(int(t.shape[1]), int(t.shape[0]))
        W = W + scale * (L[b_key] @ L[a_key])  # (out, r) @ (r, in)
        q = quants.quantize(W.astype(np.float32), gguf.GGMLQuantizationType.Q8_0)
        out_t.append((t.name, q, gguf.GGMLQuantizationType.Q8_0))
        n_merged += 1
    else:
        out_t.append((t.name, t.data, t.tensor_type))
assert n_merged == len(L) // 2, f"merged {n_merged} of {len(L) // 2} LoRA tensors"
for tname, data, tt in out_t:
    w.add_tensor_info(tname, data.shape, data.dtype, data.nbytes, tt)
w.write_header_to_file()
w.write_kv_data_to_file()
w.write_ti_data_to_file()
for tname, data, tt in out_t:
    w.write_tensor_data(data)
w.close()
print(f"merged {n_merged} tensors (scale {scale:g}) -> {merged}")

# 5) Ollama model: merged weights + live Picky's vision projector, template and params
lines = [ln for ln in mf.splitlines() if not ln.startswith("#") and not ln.startswith("FROM ")]
(work / "Modelfile").write_text("\n".join([f"FROM {merged}"] + [f"FROM {x}" for x in froms[1:]] + lines) + "\n")
subprocess.run(["ollama", "create", name, "-f", str(work / "Modelfile")], check=True)
merged.unlink()  # Ollama keeps its own blob copy
print("created", name)
