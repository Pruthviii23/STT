"""End-to-end smoke: SAPI TTS -> WAV -> Transcriber -> postprocess.clean. Downloads model on first run."""
import os
import subprocess
import sys
import tempfile
import time
import types
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from murmur.postprocess import clean
from murmur.recorder import resample
from murmur.transcriber import Transcriber

SENTENCE = "Remind me to call the dentist tomorrow at three pm, and um, buy milk."


def make_wav(path):
    ps = ("Add-Type -AssemblyName System.Speech; "
          "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          f"$s.SetOutputToWaveFile('{path}'); $s.Speak('{SENTENCE}'); $s.Dispose()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)


def load_wav(path):
    with wave.open(path, "rb") as w:
        rate, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    assert sw == 2, f"expected 16-bit, got {sw * 8}"
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return resample(a, rate, 16000)


def main():
    path = os.path.join(tempfile.mkdtemp(), "speech.wav")
    make_wav(path)
    audio = load_wav(path)
    print(f"audio: {len(audio) / 16000:.1f}s")
    t = Transcriber("small.en")
    t0 = time.time()
    t.load()
    print(f"load: {time.time() - t0:.1f}s on {t.active_device}/{t.active_compute}")
    t0 = time.time()
    raw = t.transcribe(audio)
    dt = time.time() - t0
    print(f"raw:     {raw!r}  ({dt:.2f}s)")
    cfg = types.SimpleNamespace(llm_cleanup=True, ollama_url="http://127.0.0.1:11434",
                                ollama_model="llama3.2", llm_timeout_s=20.0)
    t0 = time.time()
    print(f"cleaned: {clean(raw, cfg)!r}  ({time.time() - t0:.2f}s)")
    print("silence ->", repr(t.transcribe(np.zeros(16000, dtype=np.float32))))
    assert "dentist" in raw.lower()


if __name__ == "__main__":
    main()
