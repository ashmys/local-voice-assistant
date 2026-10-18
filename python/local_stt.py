"""Local Whisper STT adapter (faster-whisper, batch) for LiveKit."""
from __future__ import annotations

import asyncio
import logging
import os
import site
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

import numpy as np

logger = logging.getLogger("local-stt")

ROOT = Path(__file__).resolve().parent.parent
WHISPER_DIRS = [ROOT / "user_data" / "models" / "whisper"]


def _add_nvidia_dll_dirs() -> None:
    paths = []
    try:
        for sp in site.getsitepackages():
            paths.append(Path(sp) / "nvidia" / "cudnn" / "bin")
            paths.append(Path(sp) / "nvidia" / "cublas" / "bin")
        paths.append(Path(sys_prefix()) / "Lib" / "site-packages" / "nvidia" / "cudnn" / "bin")
        paths.append(Path(sys_prefix()) / "Lib" / "site-packages" / "nvidia" / "cublas" / "bin")
        for d in paths:
            if d.is_dir():
                os.add_dll_directory(str(d))
                os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
                logger.info("Added NVIDIA DLL dir: %s", d)
    except Exception as exc:
        logger.warning("Could not add NVIDIA DLL dirs: %s", exc)


def sys_prefix() -> str:
    import sys
    return sys.prefix


def local_model_dir(name: str):
    for base in WHISPER_DIRS:
        d = base / name
        if (d / "model.bin").is_file():
            return d
    return None


def resolve_device(device: str, compute: str):
    import ctranslate2

    gpu = ctranslate2.get_cuda_device_count() > 0
    if device == "auto":
        device = "cuda" if gpu else "cpu"
    if device == "cuda" and not gpu:
        logger.warning("CUDA requested but not available - falling back to CPU")
        device = "cpu"
    if compute == "auto":
        compute = "float16" if device == "cuda" else "int8"
    return device, compute, gpu


def create_stt(
    *,
    model: str = "small",
    device: str = "auto",
    compute_type: str = "auto",
    language: str = "en",
    beam_size: int = 1,
    vad_filter: bool = True,
    temperature: float = 0.0,
    min_logprob: float = -1.0,
    max_compratio: float = 2.4,
    initial_prompt: str = "",
    no_speech: float = 0.6,
):
    from livekit.agents import stt as lk_stt
    from faster_whisper import WhisperModel

    class BatchWhisperSTT(lk_stt.STT):
        def __init__(self) -> None:
            super().__init__(
                capabilities=lk_stt.STTCapabilities(streaming=False, interim_results=False)
            )
            # Accuracy notes:
            # - condition_on_previous_text=False stops hallucination carry-over
            #   between short utterances (the #1 cause of ghost words).
            # - vad_filter=True trims leading/trailing non-speech noise, so
            #   breaths/clicks no longer become phantom words like "Thank you.".
            # - an explicit language beats auto-detect on short clips; pass
            #   WHISPER_INITIAL_PROMPT to bias names/jargon your mic hears often.
            self._decode = dict(
                language=None if language in ("auto", "") else language,
                beam_size=beam_size,
                vad_filter=vad_filter,
                temperature=temperature,
                log_prob_threshold=min_logprob,
                compression_ratio_threshold=max_compratio,
                no_speech_threshold=no_speech,
                condition_on_previous_text=False,
            )
            if initial_prompt and initial_prompt.strip():
                self._decode["initial_prompt"] = initial_prompt.strip()

        async def _recognize_impl(self, buffer, *, language=None, conn_options=None):
            t0 = time.time()
            text = await asyncio.to_thread(self._transcribe_sync, buffer)
            dt = time.time() - t0
            if text:
                logger.info("STT heard %r (decode %.2fs)", text, dt)
            lang = language or self._decode["language"] or "en"
            duration = getattr(buffer, "duration", 0.0)
            return lk_stt.SpeechEvent(
                type=lk_stt.SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[
                    lk_stt.SpeechData(
                        language=lang, text=text, start_time=0.0, end_time=duration
                    )
                ],
            )

        def _transcribe_sync(self, buffer) -> str:
            arr = buffer.data
            if isinstance(arr, (bytes, bytearray, memoryview)):
                arr = np.frombuffer(arr, dtype=np.int16)
            else:
                arr = np.asarray(arr).astype(np.int16, copy=False).reshape(-1)
            num_ch = getattr(buffer, "num_channels", 1) or 1
            if num_ch > 1:
                arr = arr.reshape(-1, num_ch).mean(axis=1).astype(np.int16)
            audio = arr.astype(np.float32) / 32768.0
            segments, _info = self._model.transcribe(audio, **self._decode)
            return " ".join(s.text.strip() for s in segments).strip()

    _add_nvidia_dll_dirs()
    device, compute, gpu = resolve_device(device, compute_type)
    local = local_model_dir(model)
    if local:
        logger.info("Using local Whisper model folder: %s", local)
        src = str(local)
    else:
        logger.info("Whisper '%s' not in models folder - using HF cache", model)
        src = model
    logger.info("STT device plan: %s on %s (%s) [gpu_detected=%s]", model, device, compute, gpu)
    t0 = time.time()
    logger.info("Loading Whisper '%s' on %s (%s)...", model, device, compute)
    try:
        wm = WhisperModel(src, device=device, compute_type=compute)
    except Exception as exc:
        if device == "cpu":
            raise
        logger.warning("Whisper failed on %s (%s). Falling back to CPU int8.", device, exc)
        device, compute = "cpu", "int8"
        wm = WhisperModel(src, device=device, compute_type=compute)
    logger.info("Whisper '%s' loaded on %s", model, device)
    _warmup(wm, model)
    logger.info(
        "STT decode: beam=%s temperature=%s (single pass), logprob floor %s, "
        "compression-ratio ceiling %s, no-speech floor %s, vad_filter=%s, "
        "language=%s, initial_prompt=%s",
        beam_size, temperature, min_logprob, max_compratio, no_speech,
        vad_filter, language or "auto", "yes" if initial_prompt else "no",
    )

    stt_obj = BatchWhisperSTT()
    stt_obj._model = wm
    return stt_obj


def _warmup(wm, name: str) -> None:
    t0 = time.time()
    try:
        silence = np.zeros(16000, dtype=np.float32)
        segments, _info = wm.transcribe(silence, language="en", beam_size=1)
        list(segments)
        logger.info("STT warmup done in %.2fs", time.time() - t0)
    except Exception as exc:
        logger.warning("STT warmup skipped (%s)", exc)
