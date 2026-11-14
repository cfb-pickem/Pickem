"""Picky image-generation microservice.

Loads Z-Image Turbo (MLX / mflux) once at startup, exposes POST /generate on
127.0.0.1:8767. Called by the chat backend on 8766; nothing else should hit it.

Design notes:
- One asyncio.Lock serializes GPU access — Metal/MLX can't usefully overlap two
  denoise runs on an M4 base, and competing with Ollama's Qwen would thrash.
- Generation itself is sync + heavy, so it runs on ONE dedicated thread behind
  the lock. MLX streams are per-thread, so load and generate must share it;
  a single worker also means a timed-out job still blocks the next one
  instead of overlapping it on the GPU.
- Rate limit: 20 generations per user_id per rolling hour, in-memory dict.
- Memory: the mini has 16 GB and Ollama keeps Qwen 7B (~5 GB) pinned. The
  model (pre-quantized 4-bit copy, ~6 GB) loads on first request and unloads
  after IDLE_UNLOAD_SECONDS idle, so it only competes with Qwen while
  someone is actually making pictures.
"""

from __future__ import annotations

import asyncio
import base64
import gc
import io
import logging
import os
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Deque, Dict

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import uvicorn
import mlx.core as mx

from mflux.models.common.config.model_config import ModelConfig
from mflux.models.z_image.variants.z_image import ZImage

MODEL_ALIAS = "z-image-turbo"
# Saved once with: mflux-save --model z-image-turbo --quantize 4 --path <this>
MODEL_PATH = os.environ.get(
    "PICKY_IMAGE_MODEL_PATH", os.path.expanduser("~/models/z-image-turbo-4bit")
)
IDLE_UNLOAD_SECONDS = float(os.environ.get("PICKY_IMAGE_IDLE_UNLOAD", "300"))
MLX_CACHE_LIMIT_BYTES = 512 * 1024 * 1024
DEFAULT_STEPS = 9           # mflux's own default for z-image-turbo
DEFAULT_WIDTH = 1024
DEFAULT_HEIGHT = 1024
QUANTIZE_BITS = 4           # 4-bit quant to leave headroom for Qwen 7B
MAX_GEN_SECONDS = 240.0     # includes lock wait + a cold model load
RATE_LIMIT_PER_HOUR = 20

log = logging.getLogger("picky.image")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# Guarded by _gpu_lock. Exactly one request touches the model at a time.
_model: ZImage | None = None
_gpu_lock = asyncio.Lock()
_gpu = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mlx")
_last_used = 0.0
_rate_log: Dict[str, Deque[float]] = {}


def _load_model() -> ZImage:
    log.info("loading %s (quantize=%d)...", MODEL_ALIAS, QUANTIZE_BITS)
    t0 = time.monotonic()
    model = ZImage(
        model_config=ModelConfig.z_image_turbo(),
        model_path=MODEL_PATH,
        quantize=QUANTIZE_BITS,
    )
    log.info("model loaded in %.1fs", time.monotonic() - t0)
    return model


def _generate_sync(prompt: str, seed: int, steps: int, width: int, height: int) -> bytes:
    """Run one denoise + VAE decode on the mlx thread. Returns raw PNG bytes."""
    global _model
    if _model is None:
        _model = _load_model()
    img = _model.generate_image(
        seed=seed,
        prompt=prompt,
        num_inference_steps=steps,
        width=width,
        height=height,
    )
    buf = io.BytesIO()
    img.image.save(buf, format="PNG")
    return buf.getvalue()


def _unload_sync() -> None:
    global _model
    _model = None
    gc.collect()
    mx.clear_cache()
    log.info("model unloaded after %.0fs idle", IDLE_UNLOAD_SECONDS)


async def _idle_unloader() -> None:
    while True:
        await asyncio.sleep(30)
        if _model is None or _gpu_lock.locked():
            continue
        if time.monotonic() - _last_used < IDLE_UNLOAD_SECONDS:
            continue
        async with _gpu_lock:
            await asyncio.get_running_loop().run_in_executor(_gpu, _unload_sync)


def _check_rate_limit(user_id: str) -> None:
    now = time.monotonic()
    window_start = now - 3600.0
    hits = _rate_log.setdefault(user_id, deque())
    while hits and hits[0] < window_start:
        hits.popleft()
    if len(hits) >= RATE_LIMIT_PER_HOUR:
        retry_in = int(hits[0] + 3600.0 - now)
        raise HTTPException(
            status_code=429,
            detail=f"rate limit: {RATE_LIMIT_PER_HOUR}/hour, retry in {retry_in}s",
        )
    hits.append(now)


@asynccontextmanager
async def lifespan(app: FastAPI):
    mx.set_cache_limit(MLX_CACHE_LIMIT_BYTES)
    unloader = asyncio.create_task(_idle_unloader())
    log.info("ready on 127.0.0.1:8767 (model loads on first request)")
    yield
    unloader.cancel()
    log.info("shutting down")


app = FastAPI(lifespan=lifespan)


class GenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=2000)
    user_id: str = Field(..., min_length=1, max_length=128)
    seed: int | None = None
    steps: int = Field(DEFAULT_STEPS, ge=1, le=50)
    width: int = Field(DEFAULT_WIDTH, ge=256, le=1536)
    height: int = Field(DEFAULT_HEIGHT, ge=256, le=1536)


class GenerateResponse(BaseModel):
    image_base64: str
    elapsed_seconds: float
    seed: int
    model: str = MODEL_ALIAS


@app.get("/healthz")
async def healthz():
    return {"ok": True, "loaded": _model is not None, "model": MODEL_ALIAS}


@app.post("/generate", response_model=GenerateResponse)
async def generate(req: GenerateRequest):
    _check_rate_limit(req.user_id)
    seed = req.seed if req.seed is not None else int(time.time() * 1000) % 2_000_000_000

    async def _run() -> bytes:
        global _last_used
        async with _gpu_lock:
            t0 = time.monotonic()
            loop = asyncio.get_running_loop()
            png = await loop.run_in_executor(
                _gpu,
                _generate_sync,
                req.prompt,
                seed,
                req.steps,
                req.width,
                req.height,
            )
            elapsed = time.monotonic() - t0
            _last_used = time.monotonic()
            log.info(
                "gen user=%s steps=%d %dx%d seed=%d bytes=%d in %.1fs",
                req.user_id, req.steps, req.width, req.height, seed, len(png), elapsed,
            )
            return png, elapsed

    try:
        png, elapsed = await asyncio.wait_for(_run(), timeout=MAX_GEN_SECONDS)
    except asyncio.TimeoutError:
        log.error("gen timeout user=%s steps=%d", req.user_id, req.steps)
        raise HTTPException(status_code=504, detail=f"generation exceeded {MAX_GEN_SECONDS}s")

    return GenerateResponse(
        image_base64=base64.b64encode(png).decode("ascii"),
        elapsed_seconds=round(elapsed, 2),
        seed=seed,
    )


if __name__ == "__main__":
    uvicorn.run(
        "image:app",
        host="127.0.0.1",
        port=int(os.environ.get("PICKY_IMAGE_PORT", "8767")),
        log_level="info",
        workers=1,
    )
