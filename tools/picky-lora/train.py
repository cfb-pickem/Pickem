"""mlx_lm.lora with lowmem's Qwen3.5 fixes (see lowmem.py)."""
import sys
import lowmem  # noqa: F401  (patches before the model loads)
from mlx_lm import lora

sys.argv = ["mlx_lm.lora", *sys.argv[1:]]
lora.main()
