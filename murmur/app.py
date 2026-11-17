"""Murmur application wiring: hotkey -> recorder -> transcriber -> postprocess -> storage / inserter -> popup.

Modes (one per hold key): "note" saves to the notes file; "dictate" types the text into the focused
text field, or shows it in the popup and copies it to the clipboard when no field is focused.

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

from . import config, postprocess, sounds, storage, winapp

log = logging.getLogger("murmur.app")

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


def transcribe_clean(audio, cfg, transcriber) -> str:
    """audio -> cleaned text ("" when no speech was detected)."""
    raw = transcriber.transcribe(audio)
    log.info("Transcribed raw: %r", raw)
    if not raw or not raw.strip():
        return ""
    return postprocess.clean(raw, cfg).strip()


def process_audio(audio, cfg, transcriber, when: datetime | None = None):
    """audio -> (cleaned_text, note_path). Returns ("", None) when no speech was detected."""
    when = when or datetime.now()
    text = transcribe_clean(audio, cfg, transcriber)
    if not text:
        return "", None
    path = storage.append_note(text, cfg.notes_dir, when=when)
    log.info("Note saved to %s", path)
    return text, path


def _ensure_ico() -> Path | None:
    """Path to the app icon: the repo asset, else one generated into the app data folder."""
    repo_ico = Path(__file__).resolve().parent.parent / "assets" / "murmur.ico"
    if repo_ico.exists():
        return repo_ico
    try:
        from .icon import save_ico
        ico = config.APP_DIR / "murmur.ico"
        if not ico.exists():
            save_ico(ico)
        return ico
    except Exception:
        log.exception("Could not create icon")
        return None


def _guard(fn):
    def run(*a, **k):
        try:
            return fn(*a, **k)
        except Exception:
            log.exception("Unhandled error in %s", getattr(fn, "__name__", fn))
    return run


class App:
    def __init__(self, cfg, show_on_start: bool = True):
        import tkinter as tk
        from .hotkey import HoldHotkey
        from .popup import Popup
        from .recorder import Recorder
        from .transcriber import Transcriber
        from .tray import Tray

        self.cfg = cfg
        self.show_on_start = show_on_start
        self.root = tk.Tk()
        self.root.withdraw()
        self.popup = Popup(self.root)
        self.notes_window = None
        try:
            from .notes_window import NotesWindow
            self.notes_window = NotesWindow(self.root, lambda: self.cfg.notes_dir, self._open_folder)
            self.notes_window.set_icon(_ensure_ico())
        except Exception:
            log.exception("Notes window unavailable")
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
        self._mode: dict[int, str] = {}
        self._max_timer: threading.Timer | None = None
        self.tray = Tray(on_toggle=_guard(self._on_toggle), on_open=_guard(self.show_window),
                         on_open_folder=_guard(self._open_folder), on_settings=_guard(self._open_settings),
                         on_quit=_guard(self.quit_flag.set))
        keys = {cfg.hotkey: "note"}
        if cfg.dictate_hotkey:
            keys[cfg.dictate_hotkey] = "dictate"
        if cfg.formal_hotkey:
            keys[cfg.formal_hotkey] = "formal"
        self.hotkey = HoldHotkey(keys, cfg.hold_ms, on_hold_start=_guard(self._hold_start),
                                 on_hold_end=_guard(self._hold_end))

    # ---------------------------------------------------------------- tray
    def _on_toggle(self, enabled: bool) -> None:
        self.hotkey.enabled = enabled
        log.info("Enabled = %s", enabled)

    def show_window(self) -> None:
        if self.notes_window is not None:
            self.notes_window.show()

    def _open_folder(self) -> None:
        self.cfg.notes_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(self.cfg.notes_dir))

    def _open_settings(self) -> None:
        os.startfile(str(config.CONFIG_PATH))

    # ---------------------------------------------------------------- hold flow
    _LISTEN_TITLE = {"note": "Listening…", "dictate": "Dictating…", "formal": "Dictating (formal)…"}
    _PROCESS_TITLE = {"formal": "Writing formally"}

    def _hold_start(self, mode: str = "note") -> None:
        from .recorder import RecorderError
        with self._lock:
            self._sid += 1
            sid = self._sid
        log.info("Hold start (session %d, mode %s)", sid, mode)
        if self.cfg.sound_cues:
            sounds.cue_start()
        self.popup.show_listening(self._LISTEN_TITLE.get(mode, "Listening…"))
        if mode == "formal":     # load the formal model while the user is still speaking
            threading.Thread(target=self._prewarm_ollama, args=(self.cfg.formal_model or None,),
                             name="murmur-prewarm", daemon=True).start()
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
            self._mode[sid] = mode
            t = threading.Timer(float(self.cfg.max_record_s), self._auto_stop, args=(sid,))
            t.daemon = True
            self._max_timer = t
            t.start()
        log.info("Recording started")

    def _auto_stop(self, sid: int) -> None:
        log.warning("max_record_s reached; auto-stopping session %d", sid)
        _guard(self._finish)(sid)

    def _hold_end(self, mode: str = "note") -> None:
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
            mode = self._mode.pop(sid, "note")
        audio = self.recorder.stop()
        dur = len(audio) / 16000.0
        log.info("Recording stopped: %.2fs of audio (session %d)", dur, sid)
        if self.cfg.sound_cues:
            sounds.cue_stop()
        if dur < self.cfg.min_record_s:
            log.info("Too short; ignoring")
            self.popup.hide()
            return
        self.popup.show_processing(self._PROCESS_TITLE.get(mode, "Transcribing"))
        self.jobs.put((audio, when, mode))

    # ---------------------------------------------------------------- worker
    def _worker(self) -> None:
        while True:
            job = self.jobs.get()
            if job is None:
                return
            audio, when, mode = job
            try:
                self.model_ready.wait()
                if mode == "formal":
                    raw = self.transcriber.transcribe(audio)
                    log.info("Transcribed raw: %r", raw)
                    text, rewritten = postprocess.formal(raw or "", self.cfg)
                    log.info("Formal rewrite %s: %r", "ok" if rewritten else "unavailable", text)
                    self._deliver(mode, text, when, rewritten)
                    continue
                if mode != "note":
                    self._deliver(mode, transcribe_clean(audio, self.cfg, self.transcriber), when)
                    continue
                text, _ = process_audio(audio, self.cfg, self.transcriber, when)
                if text:
                    if self.notes_window is not None:
                        self.notes_window.notify_changed()
                    self.popup.show_result(text, when, self.cfg.popup_seconds)
                else:
                    self.popup.show_message("Didn't catch that", "No speech detected.")
                    log.info("Empty transcription")
            except Exception as e:
                log.exception("Processing failed")
                if self.cfg.sound_cues:
                    sounds.cue_error()
                self.popup.show_message("Something went wrong", str(e)[:200], 4.0)

    def _deliver(self, mode: str, text: str, when: datetime, rewritten: bool = True) -> None:
        """Dictation: type into the focused field, else show in the popup + copy to clipboard.

        rewritten=False (formal mode only) means the LLM was unavailable and the plain text is used.
        """
        from . import inserter
        if not text:
            self.popup.show_message("Didn't catch that", "No speech detected.")
            log.info("Empty transcription (%s)", mode)
            return
        note = "" if rewritten else " (not formal: Ollama unavailable)"
        if inserter.focused_text_input():
            with self.hotkey.paused():
                inserter.type_text(text)
            log.info("Typed %d chars into focused field (%s)", len(text), mode)
            self.popup.show_result(text, when, min(2.0, self.cfg.popup_seconds) if rewritten else 5.0,
                                   tag="✓ Typed" + note)
            return
        copied = inserter.copy_to_clipboard(text)
        log.info("No text field focused; shown in popup (copied=%s)", copied)
        self.popup.show_result(text, when, max(6.0, self.cfg.popup_seconds),
                               tag=("No text field · copied" if copied else "No text field") + note)

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

    def _prewarm_ollama(self, model: str | None = None) -> None:
        try:
            import requests
            requests.post(self.cfg.ollama_url.rstrip("/") + "/api/generate",
                          json={"model": model or self.cfg.ollama_model, "prompt": "ok", "stream": False,
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
        winapp.start_show_listener(self.show_window)
        if self.show_on_start:
            self.root.after(300, self.show_window)
        self.root.after(200, self._poll)
        log.info("Murmur started (hotkey %r, notes in %s)", self.cfg.hotkey, self.cfg.notes_dir)
        self.root.mainloop()


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--install-shortcuts" in argv or "--uninstall-shortcuts" in argv:
        if "--install-shortcuts" in argv:
            for p in winapp.install_shortcuts():
                print("Created", p)
        else:
            for p in winapp.uninstall_shortcuts():
                print("Removed", p)
        return 0
    background = "--background" in argv
    winapp.set_app_id()
    setup_logging()
    if not winapp.acquire_single_instance():
        if background:
            log.info("Another Murmur instance is already running; exiting (background)")
        else:
            log.info("Another Murmur instance is running; asking it to show the window")
            winapp.signal_show()
        return 0
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    sys.excepthook = lambda t, v, tb: log.error("Uncaught", exc_info=(t, v, tb))
    try:
        cfg = config.load_config()
        App(cfg, show_on_start=not background).run()
    except Exception:
        log.exception("Fatal error")
        return 1
    return 0
