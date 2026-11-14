"""faster-whisper wrapper. device='auto': CUDA float16 first, verified with a warm-up
transcription (cuBLAS/cuDNN errors often only surface at inference), else CPU int8.
All model use is under a lock and the segment generator is consumed inside it."""
from __future__ import annotations

import logging
import re
import threading

import numpy as np

log = logging.getLogger(__name__)

PROMPT = "Hello. Here's a quick note, with proper punctuation."

_HALLUCINATIONS = {
    "you", "thank you", "thanks", "thanks for watching", "thank you for watching",
    "thank you so much for watching", "bye", "bye bye", "the end", "please subscribe",
    "subtitles by the amara.org community", "hello",
    "hello heres a quick note with proper punctuation",
    "heres a quick note with proper punctuation",
}


def _norm(s: str) -> str:
    s = s.lower().replace("'", "").replace("’", "")
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", s).split())


_HALL_NORM = {_norm(h) for h in _HALLUCINATIONS}


def _is_hallucination(text: str) -> bool:
    n = _norm(text)
    return not n or n in _HALL_NORM


class Transcriber:
    def __init__(self, model: str, device: str = "auto", compute_type: str = "auto",
                 language: str | None = "en"):
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.active_device: str | None = None
        self.active_compute: str | None = None
        self._model = None
        self._lock = threading.RLock()

    @property
    def ready(self) -> bool:
        return self._model is not None

    def _run(self, model, audio: np.ndarray, vad: bool = True) -> str:
        segments, _info = model.transcribe(
            audio, language=self.language, beam_size=5, vad_filter=vad,
            condition_on_previous_text=False, initial_prompt=PROMPT)
        return " ".join(s.text.strip() for s in segments).strip()  # consumes generator

    def _try(self, device: str, compute: str):
        from faster_whisper import WhisperModel
        model = WhisperModel(self.model_name, device=device, compute_type=compute)
        # Warm-up: 0.5 s of quiet noise with VAD off, so the encoder really runs on the
        # device (silence + VAD would skip the encoder and hide missing cuBLAS DLLs).
        noise = (np.random.default_rng(0).standard_normal(8000) * 0.01).astype(np.float32)
        self._run(model, noise, vad=False)
        return model

    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            if self.device == "auto":
                attempts = [("cuda", "float16"), ("cpu", "int8")]
            else:
                comp = self.compute_type
                if comp == "auto":
                    comp = "float16" if self.device == "cuda" else "int8"
                attempts = [(self.device, comp)]
            last = None
            for dev, comp in attempts:
                try:
                    self._model = self._try(dev, comp)
                    self.active_device, self.active_compute = dev, comp
                    log.info("Whisper %s loaded on %s/%s", self.model_name, dev, comp)
                    return
                except Exception as e:
                    last = e
                    log.warning("Whisper load on %s/%s failed: %s", dev, comp, e)
                    self._model = None
            raise RuntimeError(f"Could not load Whisper model: {last}") from last

    def transcribe(self, audio: np.ndarray) -> str:
        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        if audio.size < 1600:
            return ""
        if float(np.sqrt(np.mean(audio * audio))) < 1e-4:
            return ""
        if self._model is None:
            self.load()
        with self._lock:
            try:
                text = self._run(self._model, audio)
            except Exception:
                if self.active_device != "cuda":
                    raise
                log.exception("CUDA inference failed; falling back to CPU")
                from faster_whisper import WhisperModel
                self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
                self.active_device, self.active_compute = "cpu", "int8"
                text = self._run(self._model, audio)
        return "" if _is_hallucination(text) else text
