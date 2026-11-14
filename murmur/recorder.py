"""Microphone recorder. Opens the default input at its native rate (mono float32),
resamples to 16 kHz on stop(). on_level is called per callback block (~20/s)."""
from __future__ import annotations

import logging
import threading
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)


class RecorderError(Exception):
    pass


def resample(audio: np.ndarray, src_rate: int, dst_rate: int = 16000) -> np.ndarray:
    """Linear-interpolation resample of a 1-D array."""
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if src_rate == dst_rate or audio.size == 0:
        return audio
    n_out = int(round(audio.size * dst_rate / src_rate))
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)
    x_old = np.arange(audio.size, dtype=np.float64)
    x_new = np.linspace(0, audio.size - 1, n_out)
    return np.interp(x_new, x_old, audio).astype(np.float32)


class Recorder:
    def __init__(self, samplerate: int = 16000, on_level: Callable[[float], None] | None = None):
        self.samplerate = samplerate
        self.on_level = on_level
        self._stream = None
        self._chunks: list[np.ndarray] = []
        self._native_rate = samplerate
        self._lock = threading.Lock()

    @property
    def recording(self) -> bool:
        return self._stream is not None

    def _callback(self, indata, frames, time_info, status):
        chunk = indata[:, 0].copy() if indata.ndim > 1 else indata.copy()
        self._chunks.append(chunk)
        cb = self.on_level
        if cb is not None:
            try:
                rms = float(np.sqrt(np.mean(chunk * chunk))) if chunk.size else 0.0
                # speech RMS is typically 0.01-0.1; compress to 0..1
                cb(min(1.0, (rms * 8.0) ** 0.7))
            except Exception:
                pass

    def start(self) -> None:
        with self._lock:
            if self._stream is not None:
                return
            try:
                import sounddevice as sd
                dev = sd.query_devices(kind="input")
                self._native_rate = int(dev["default_samplerate"]) or self.samplerate
                self._chunks = []
                stream = sd.InputStream(
                    samplerate=self._native_rate, channels=1, dtype="float32",
                    blocksize=max(1, self._native_rate // 20), callback=self._callback)
                stream.start()
            except Exception as e:
                log.exception("Could not open microphone")
                raise RecorderError(f"Microphone unavailable: {e}") from e
            self._stream = stream

    def stop(self) -> np.ndarray:
        with self._lock:
            stream, self._stream = self._stream, None
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    log.exception("Error closing stream")
            chunks, self._chunks = self._chunks, []
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate(chunks).astype(np.float32)
        return resample(audio, self._native_rate, self.samplerate)
