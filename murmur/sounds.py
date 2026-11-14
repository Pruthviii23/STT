"""Tiny audio cues. Soft sine tones generated in memory, played async via winsound. Never raises."""
from __future__ import annotations

import io
import logging
import wave

import numpy as np

try:
    import winsound
except ImportError:  # non-Windows
    winsound = None

log = logging.getLogger(__name__)
_SR = 22050
_VOL = 0.22
_cache: dict[str, bytes] = {}
_current: bytes | None = None  # keep reference alive during async playback


def _tone(freq: float, ms: int) -> np.ndarray:
    n = int(_SR * ms / 1000)
    t = np.arange(n) / _SR
    y = np.sin(2 * np.pi * freq * t)
    f = min(int(_SR * 0.012), n // 2)
    env = np.ones(n)
    env[:f] = np.linspace(0, 1, f)
    env[-f:] = np.linspace(1, 0, f)
    return y * env * _VOL


def _wav(parts: list[tuple[float, int]]) -> bytes:
    y = np.concatenate([_tone(f, ms) for f, ms in parts])
    pcm = (y * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_SR)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


_DEFS = {
    "start": [(880, 60), (1175, 70)],
    "stop": [(1175, 60), (880, 70)],
    "error": [(330, 90), (247, 150)],
}


def _play(name: str) -> None:
    global _current
    try:
        if winsound is None:
            return
        data = _cache.get(name)
        if data is None:
            data = _cache[name] = _wav(_DEFS[name])
        _current = data
        winsound.PlaySound(data, winsound.SND_MEMORY | winsound.SND_ASYNC)
    except Exception:
        log.debug("sound cue failed", exc_info=True)


def cue_start() -> None:
    _play("start")


def cue_stop() -> None:
    _play("stop")


def cue_error() -> None:
    _play("error")
