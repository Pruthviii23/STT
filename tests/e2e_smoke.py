"""End-to-end smoke test. Run: .venv\\Scripts\\python tests\\e2e_smoke.py

Part 1: launches `python -m murmur --background` (MURMUR_INSTANCE=e2e) with a temp APPDATA, simulates a '/' hold + a quick tap via
SendInput and checks the log. Part 2: TTS WAV -> app.process_audio -> note in a temp dir.
Warning: part 1 installs a real global keyboard hook in the subprocess for a few seconds.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from murmur.hotkey import _send_scan  # noqa: E402

PY = ROOT / ".venv" / "Scripts" / "python.exe"


def wait_for(path: Path, needle: str, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if needle in path.read_text(encoding="utf-8", errors="ignore"):
                return True
        except FileNotFoundError:
            pass
        time.sleep(0.3)
    return False


def part1(tmp: Path) -> None:
    appdata = tmp / "appdata"
    notes = tmp / "notes1"
    (appdata / "Murmur").mkdir(parents=True)
    (appdata / "Murmur" / "config.json").write_text(
        json.dumps({"notes_dir": str(notes), "llm_cleanup": False, "sound_cues": False}), encoding="utf-8")
    log = appdata / "Murmur" / "murmur.log"
    env = dict(os.environ, APPDATA=str(appdata), MURMUR_INSTANCE="e2e")
    proc = subprocess.Popen([str(PY), "-m", "murmur", "--background"], cwd=ROOT, env=env)
    try:
        assert wait_for(log, "Model ready", 90), "model never became ready"
        time.sleep(1.0)
        _send_scan(53, False)
        t_end = time.time() + 2.5
        while time.time() < t_end:
            time.sleep(0.033)
            _send_scan(53, False)
        _send_scan(53, True)
        assert wait_for(log, "Empty transcription", 30) or wait_for(log, "Note saved", 5), "no result in log"
        time.sleep(0.5)
        # quick tap
        _send_scan(53, False)
        time.sleep(0.05)
        _send_scan(53, True)
        time.sleep(1.0)
        assert proc.poll() is None, "app died"
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
    text = log.read_text(encoding="utf-8")
    print(text)
    for needle in ("Hold start", "Recording started", "Recording stopped", "Transcribed raw"):
        assert needle in text, f"missing log line: {needle}"
    assert "Traceback" not in text and "ERROR" not in text, "errors in log"
    print("PART 1 OK")


def part2(tmp: Path) -> None:
    wav = tmp / "tts.wav"
    ps = ("Add-Type -AssemblyName System.Speech; $s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
          f"$s.SetOutputToWaveFile('{wav}'); $s.Speak('Remember to call the dentist tomorrow morning, and "
          "buy milk on the way home.'); $s.Dispose()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    with wave.open(str(wav)) as w:
        sr, n, ch, sw = w.getframerate(), w.getnframes(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(n)
    assert sw == 2
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    from murmur.recorder import resample
    audio = resample(a, sr, 16000)
    from murmur import app
    from murmur.config import Config
    from murmur.transcriber import Transcriber
    cfg = Config(notes_dir=tmp / "notes2", llm_cleanup=False)
    t = Transcriber(cfg.whisper_model, cfg.whisper_device, cfg.whisper_compute, cfg.language)
    text, path = app.process_audio(audio, cfg, t)
    print("TEXT:", text)
    assert path and path.exists(), "no note written"
    print("---", path)
    print(path.read_text(encoding="utf-8"))
    assert "dentist" in text.lower()
    print("PART 2 OK")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        which = sys.argv[1] if len(sys.argv) > 1 else "all"
        if which in ("all", "1"):
            part1(tmp)
        if which in ("all", "2"):
            part2(tmp)
