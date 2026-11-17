# Picky style LoRA (MLX, 16GB M4)

> **RETIRED 2026-11-16.** The cycle trained and shipped onto `qwen3.5:9b`, which was deleted; gemma4:12b-it-qat is the only model on the box. LaunchAgent `com.pickem.picky-lora` was booted out and its plist moved to `~/Library/LaunchAgents/disabled/` (repo copy: `com.pickem.picky-lora.plist.retired` here). `PICKY_LORA_PAUSE` stays in place. The code is kept for reference only: don't reload it, and don't pull or create a model for it; reviving it means retargeting the base model, the Ollama `FROM` in `to_ollama.py` and the exam setup.

A style-only LoRA for text Picky on `mlx-community/Qwen3.5-9B-MLX-4bit`. It trains his voice: answer first, league-buddy banter, jabs, the Ackbar nemesis and Dear Leader bits as behavior. It never trains league facts. Every training reply takes its numbers and names from a randomized fact block in its own prompt, so the model learns to copy facts from context. Fine-tuning on new facts raises hallucination (Gekhman et al., 2024).

Code lives here. Data, weights, the venv and logs live in `/Users/Agent/picky-lora/` (private, never committed). `scripts/` and `lora_config.yaml` there are symlinks to this directory.

## Pieces
| file | what |
|---|---|
| `make_specs.py` | randomized scenario specs (15 message types, group/DM, random facts; nicknames from the private member map) |
| `gen.py` + `gen_prompt.md` | offline `claude -p` (Opus via server/picky_claude.py) writes recent chat + message + Picky's reply per spec |
| `validate.py` | rejects any reply with a number, school or member not in its facts/message; also milk/Jeep, em dashes, >3 sentences in groups, Dear Leader talk when Chuck is the sender, repeated openers |
| `render.py` | chat JSONL for mlx_lm (short persona + LEAGUE DATA + GROUP/DM rider + RECENT GROUP CHAT, riders in the user turn), ≤384 tokens, stable 10% validation split |
| `lowmem.py`, `train.py` | mlx_lm.lora with the 16GB fixes: frozen Qwen3.5 linear-attention layers keep the Metal kernel, small MLX cache, no forced wired limit |
| `memwatch.sh` | stops training if free memory < 6%, swap grows > 2 GB over its settled level, or free disk < 5 GiB; keeps the 2 newest checkpoints |
| `picky_down.sh` / `picky_up.sh` | maintenance replies on + Ollama/image server off / the reverse plus health checks. Group posts stay on (they never touch Ollama) |
| `to_ollama.py` | MLX adapter -> PEFT -> GGUF LoRA (llama.cpp `convert_lora_to_gguf.py`) -> Ollama model `FROM qwen3.5:9b-q4_K_M` + `ADAPTER`. No fused weights, so disk stays flat |
| `eval_model.sh` | text exam (`eval/run.py --text`) through an isolated side sidecar on :8781 (throwaway history/milk state, no maintenance flag) |
| `harvest.py` | grades the week's real exchanges (`cfb-pickem-private/picky_exchanges.jsonl`) and turns weak ones into specs: real message, fresh random facts |
| `cycle.py` | the nightly cycle, below |

## Nightly cycle (`com.pickem.picky-lora`)
Charlie, 2026-11-15: "you can train him every night and all day Monday". Windows run 22:00-08:30 CT, and Sunday 22:00 runs through Tuesday 08:30. Launches are at 22:00 and Monday 10:00. The 08:35 launch is a safety net that brings Picky back up if a run left him down.

1. Data: harvest weak replies, add 120 fresh specs, `claude -p`, validate, render (Picky stays live). The training set is capped at 650: harvested first, then the newest synthetic, then the base set.
2. Train: down, then train under the watchdog. Training stops at window end minus 150 min, and the next window resumes from the checkpoint (`runs/<cycle>/partN`).
3. Eval: Ollama comes back with maintenance replies still on. The candidate becomes Ollama `picky-lora:cand`. The text exam runs on the live model and the candidate (plus base when live isn't base), then the blind `judge_text.py` grades them.
4. Ship ONLY if the candidate beats live on helpful, human AND wit (judge means), with invented facts no higher and 0 privacy leaks. Shipping copies it to `picky-lora:live`, points `PICKEM_CHAT_MODEL` in com.pickem.chat's plist at it (the group sidecar inherits it), restarts chat + sidecar, and texts Charlie. Otherwise live stays and the reason is logged.
5. Restore every time by window end (maintenance off, Ollama + image up, health checks).

It waits up to 45 min for `/Users/Agent/picky-marathon/.tuning.lock` and holds it for the whole run.

**Kill switch:** `touch /Users/Agent/cfb-pickem-private/PICKY_LORA_PAUSE` (remove to resume). Unload: `launchctl bootout gui/$(id -u)/com.pickem.picky-lora`.
**Roll back to base:** set `PICKEM_CHAT_MODEL` to `qwen3.5:9b-q4_K_M` in `~/Library/LaunchAgents/com.pickem.chat.plist`, then `launchctl kickstart -k` com.pickem.chat and com.pickem.picky-group.
**Logs:** `/Users/Agent/picky-lora/logs/cycle.log`, `cycle_history.jsonl`, `memwatch.log`, `updown.log`, `train-<cycle>.log`.

## Measured on this box (2026-11-15)
- One training step at 512 tokens peaks at about 12 GB, mostly one LoRA linear-attention layer's backward pass. It swapped, so sequences are capped at 384 (about 10.3 GB, about 8 s/step). 256 tokens is about 8.6 GB.
- Without `lowmem.py`, every linear-attention layer runs mlx_lm's pure-ops path in training mode.
- Docker Desktop's VM held 8 GB. It's quit now; training needs that headroom.
