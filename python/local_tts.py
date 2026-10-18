"""Local Piper TTS adapter for LiveKit."""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

from livekit import rtc
from livekit.agents import APIConnectOptions, tts

logger = logging.getLogger("local-tts")

ROOT = Path(__file__).resolve().parent.parent
PIPER_DIRS = [ROOT / "user_data" / "models" / "piper"]
SAMPLES_PER_FRAME = 4096


def find_voice(name: str):
    if not name:
        return None
    for base in PIPER_DIRS:
        v = base / name
        if v.is_file():
            return v
    p = Path(name)
    return p if p.is_file() else None


def build_syn_config(speed: float):
    if abs(speed - 1.0) < 0.01:
        return None
    length_scale = 1.0 / max(0.1, speed)
    try:
        from piper import SynthesisConfig

        logger.info("Piper speed set to %.2fx (length_scale %.2f)", speed, length_scale)
        return SynthesisConfig(length_scale=length_scale)
    except Exception:
        try:
            from piper import PiperConfig

            logger.info("Piper speed config unsupported by this piper version")
        except Exception:
            pass
        return None


def create_piper_tts(*, voice_name: str = "en_US-amy-medium.onnx", speed: float = 1.0):
    voice_path = find_voice(voice_name)
    if voice_path is None:
        raise FileNotFoundError(
            f"Piper voice not found: {voice_name}\n"
            "Run setup.bat (menu 4) or drop <name>.onnx + <name>.onnx.json "
            "into user_data\\models\\piper\\"
        )

    from piper import PiperVoice

    logger.info("Loading Piper voice: %s", voice_path)
    try:
        voice = PiperVoice.load(str(voice_path))
    except TypeError:
        voice = PiperVoice.load(str(voice_path), str(voice_path) + ".json")

    sample_rate = int(getattr(voice, "config", None) and voice.config.sample_rate or 22050)
    logger.info("Piper voice loaded (%d Hz)", sample_rate)
    syn_cfg = build_syn_config(speed)

    class PiperStream(tts.ChunkedStream):
        def __init__(self, tts_obj, text, conn_options=None):
            self._text = text
            self._request_id = uuid.uuid4().hex[:12]
            super().__init__(
                tts=tts_obj,
                input_text=text,
                conn_options=conn_options or APIConnectOptions(),
            )

        async def _run(self, output_emitter) -> None:
            raise RuntimeError("PiperStream uses _main_task, not _run")

        def _pcm_pieces(self, text: str):
            pieces = []
            if syn_cfg is not None:
                chunks = voice.synthesize(text, syn_config=syn_cfg)
            else:
                chunks = voice.synthesize(text)
            for chunk in chunks:
                pcm = getattr(chunk, "audio_int16_bytes", None)
                if pcm is None and isinstance(chunk, (bytes, bytearray)):
                    pcm = bytes(chunk)
                if not pcm:
                    continue
                sr = int(getattr(chunk, "sample_rate", 0) or sample_rate)
                pieces.append((bytes(pcm), sr))
            return pieces

        async def _main_task(self) -> None:
            try:
                pieces = await asyncio.to_thread(self._pcm_pieces, self._text)
                if not pieces:
                    logger.warning("Piper produced no audio for %r", self._text[:80])
                for pcm, sr in pieces:
                    n_total = len(pcm) // 2
                    offset = 0
                    while offset < n_total:
                        n = min(SAMPLES_PER_FRAME, n_total - offset)
                        start = offset * 2
                        frame = rtc.AudioFrame(
                            data=pcm[start : start + n * 2],
                            sample_rate=sr,
                            num_channels=1,
                            samples_per_channel=n,
                        )
                        self._event_ch.send_nowait(
                            tts.SynthesizedAudio(frame=frame, request_id=self._request_id)
                        )
                        offset += n
            except Exception:
                logger.exception("Piper synthesis failed")
                raise

    class PiperTTS(tts.TTS):
        def __init__(self) -> None:
            super().__init__(
                capabilities=tts.TTSCapabilities(streaming=False),
                sample_rate=sample_rate,
                num_channels=1,
            )

        def synthesize(self, text: str, *, conn_options=None):
            return PiperStream(self, text, conn_options)

    return PiperTTS()
