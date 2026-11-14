"""Murmur application wiring: hotkey -> recorder -> transcriber -> postprocess -> storage -> popup.

Threads: Tk main loop on the main thread; hotkey callbacks on the hotkey's callback thread; one
worker thread does transcription; tray callbacks on the tray thread. Nothing blocks a hotkey callback.
"""
from __future__ import annotations

import ctypes
import logging
import logging.handlers
import os
import queue
import sys
import threading
from datetime import datetime
from pathlib import Path

from . import config, postprocess, sounds, storage

log = logging.getLogger("murmur.app")

MUTEX_NAME = "Local\\MurmurVoiceNotes"
ERROR_ALREADY_EXISTS = 183
_mutex_handle = None


def setup_logging() -> None:
    config.APP_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(config.LOG_PATH, maxBytes=1_000_000, backupCount=3,
                                              encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stderr is not None and getattr(sys.stderr, "isatty", lambda: False)():
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


def acquire_single_instance() -> bool:
    global _mutex_handle
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.restype = ctypes.c_void_p
    _mutex_handle = k32.CreateMutexW(None, False, MUTEX_NAME)
    return k32.GetLastError() != ERROR_ALREADY_EXISTS


def process_audio(audio, cfg, transcriber, when: datetime | None = None):
    """audio -> (cleaned_text, note_path). Returns ("", None) when no speech was detected."""
    when = when or datetime.now()
    raw = transcriber.transcribe(audio)
    log.info("Transcribed raw: %r", raw)
    if not raw or not raw.strip():
        return "", None
    text = postprocess.clean(raw, cfg)
    if not text.strip():
        return "", None
    path = storage.append_note(text, cfg.notes_dir, when=when)
    log.info("Note saved to %s", path)
    return text, path


def _ensure_month_file(cfg) -> Path:
    path = storage.today_file(cfg.notes_dir)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# Notes — {datetime.now():%B %Y}\n", encoding="utf-8")
    return path


def _guard(fn):
    def run(*a, **k):
        try:
            return fn(*a, **k)
        except Exception:
            log.exception("Unhandled error in %s", getattr(fn, "__name__", fn))
    return run


class App:
    def __init__(self, cfg):
        import tkinter as tk
        from .hotkey import HoldHotkey
        from .popup import Popup
        from .recorder import Recorder
        from .transcriber import Transcriber
        from .tray import Tray

        self.cfg = cfg
        self.root = tk.Tk()
        self.root.withdraw()
        self.popup = Popup(self.root)
        self.recorder = Recorder(on_level=self.popup.set_level)
        self.transcriber = Transcriber(cfg.whisper_model, cfg.whisper_device, cfg.whisper_compute,
                                       cfg.language)
        self.model_ready = threading.Event()
        self.jobs: queue.Queue = queue.Queue()
        self.quit_flag = threading.Event()
        self._lock = threading.Lock()
        self._sid = 0
        self._active_sid = 0           # 0 = no active session
        self._start_time: dict[int, datetime] = {}
        self._max_timer: threading.Timer | None = None
        self.tray = Tray(on_toggle=_guard(self._on_toggle), on_open_notes=_guard(self._open_notes),
                         on_open_folder=_guard(self._open_folder), on_settings=_guard(self._open_settings),
                         on_quit=_guard(self.quit_flag.set))
        self.hotkey = HoldHotkey(cfg.hotkey, cfg.hold_ms, on_hold_start=_guard(self._hold_start),
                                 on_hold_end=_guard(self._hold_end))

    # ---------------------------------------------------------------- tray
    def _on_toggle(self, enabled: bool) -> None:
        self.hotkey.enabled = enabled
        log.info("Enabled = %s", enabled)

    def _open_notes(self) -> None:
        os.startfile(str(_ensure_month_file(self.cfg)))

    def _open_folder(self) -> None:
        self.cfg.notes_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(self.cfg.notes_dir))

    def _open_settings(self) -> None:
        os.startfile(str(config.CONFIG_PATH))

    # ---------------------------------------------------------------- hold flow
    def _hold_start(self) -> None:
        from .recorder import RecorderError
        with self._lock:
            self._sid += 1
            sid = self._sid
        log.info("Hold start (session %d)", sid)
        if self.cfg.sound_cues:
            sounds.cue_start()
        self.popup.show_listening()
        try:
            self.recorder.start()
        except RecorderError as e:
            log.error("Recorder error: %s", e)
            if self.cfg.sound_cues:
                sounds.cue_error()
            self.popup.show_message("Can't record", str(e), 4.0)
            return
        with self._lock:
            self._active_sid = sid
            self._start_time[sid] = datetime.now()
            t = threading.Timer(float(self.cfg.max_record_s), self._auto_stop, args=(sid,))
            t.daemon = True
            self._max_timer = t
            t.start()
        log.info("Recording started")

    def _auto_stop(self, sid: int) -> None:
        log.warning("max_record_s reached; auto-stopping session %d", sid)
        _guard(self._finish)(sid)

    def _hold_end(self) -> None:
        with self._lock:
            sid = self._active_sid
        if sid:
            self._finish(sid)

    def _finish(self, sid: int) -> None:
        with self._lock:
            if self._active_sid != sid:
                return                      # already ended (auto-stop vs. hold end)
            self._active_sid = 0
            if self._max_timer:
                self._max_timer.cancel()
                self._max_timer = None
            when = self._start_time.pop(sid, datetime.now())
        audio = self.recorder.stop()
        dur = len(audio) / 16000.0
        log.info("Recording stopped: %.2fs of audio (session %d)", dur, sid)
        if self.cfg.sound_cues:
            sounds.cue_stop()
        if dur < self.cfg.min_record_s:
            log.info("Too short; ignoring")
            self.popup.hide()
            return
        self.popup.show_processing()
        self.jobs.put((audio, when))

    # ---------------------------------------------------------------- worker
    def _worker(self) -> None:
        while True:
            job = self.jobs.get()
            if job is None:
                return
            audio, when = job
            try:
                self.model_ready.wait()
                text, _ = process_audio(audio, self.cfg, self.transcriber, when)
                if text:
                    self.popup.show_result(text, when, self.cfg.popup_seconds)
                else:
                    self.popup.show_message("Didn't catch that", "No speech detected.")
                    log.info("Empty transcription")
            except Exception as e:
                log.exception("Processing failed")
                if self.cfg.sound_cues:
                    sounds.cue_error()
                self.popup.show_message("Something went wrong", str(e)[:200], 4.0)

    def _load_model(self) -> None:
        self.tray.set_status("Murmur — loading model…")
        try:
            self.transcriber.load()
            self.model_ready.set()
            log.info("Model ready (%s/%s)", self.transcriber.active_device, self.transcriber.active_compute)
            self.tray.set_status(f"Murmur — ready (hold {self.cfg.hotkey} to talk)")
        except Exception:
            log.exception("Model load failed")
            self.tray.set_status("Murmur — model failed to load (see log)")
            self.model_ready.set()          # let the worker run; transcribe() will retry and report
        if self.cfg.llm_cleanup:
            self._prewarm_ollama()

    def _prewarm_ollama(self) -> None:
        try:
            import requests
            requests.post(self.cfg.ollama_url.rstrip("/") + "/api/generate",
                          json={"model": self.cfg.ollama_model, "prompt": "ok", "stream": False,
                                "keep_alive": "30m", "think": False, "options": {"num_predict": 1}},
                          timeout=30)
            log.info("Ollama pre-warmed")
        except Exception as e:
            log.info("Ollama pre-warm skipped: %s", e)

    # ---------------------------------------------------------------- lifecycle
    def _poll(self) -> None:
        if self.quit_flag.is_set():
            self.shutdown()
            return
        self.root.after(200, self._poll)

    def shutdown(self) -> None:
        log.info("Shutting down")
        for fn in (self.hotkey.stop, self.tray.stop, self.recorder.stop, lambda: self.jobs.put(None)):
            try:
                fn()
            except Exception:
                log.exception("shutdown step failed")
        self.root.quit()

    def run(self) -> None:
        threading.Thread(target=self._worker, name="murmur-worker", daemon=True).start()
        threading.Thread(target=self._load_model, name="murmur-model", daemon=True).start()
        self.tray.start()
        self.hotkey.start()
        self.root.after(200, self._poll)
        log.info("Murmur started (hotkey %r, notes in %s)", self.cfg.hotkey, self.cfg.notes_dir)
        self.root.mainloop()


def main() -> int:
    setup_logging()
    if not acquire_single_instance():
        log.info("Another Murmur instance is already running; exiting")
        return 0
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    sys.excepthook = lambda t, v, tb: log.error("Uncaught", exc_info=(t, v, tb))
    try:
        cfg = config.load_config()
        App(cfg).run()
    except Exception:
        log.exception("Fatal error")
        return 1
    return 0
