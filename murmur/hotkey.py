"""Hold-to-talk global hotkeys (default: "/" for notes; more keys can map to other modes).

Design notes
- One global ``keyboard.hook(suppress=True)`` serves every hold key. The callback does only
  short, non-blocking work under a lock and returns True at once for anything it does not own.
- ``keys`` maps a key name to a mode string; the mode is passed to on_hold_start / on_hold_end.
  Keys are matched by (non-keypad) scan code. Only one key can be pending/holding at a time.
- Hold-key down (no Shift/Ctrl/Alt/Win) is suppressed and starts a hold timer.
  Tap (up before hold_ms) -> a worker thread injects that key's down+up.
  Another key down while pending -> that key is ALSO suppressed and the worker injects
  the hold key's tap followed by that key's press, so order is preserved deterministically.
  While the worker still has injections outstanding ("busy"), every further key event
  is suppressed and queued behind them, so later events (e.g. the key-up) can never
  overtake the replay.
- The keyboard lib does not surface LLKHF_INJECTED, so our own injected events are
  recognised via an "expected injections" deque of (scan_code, event_type, deadline).
- on_hold_start / on_hold_end run on one dedicated callback thread, strictly serialised and
  paired (end is only run if start ran).
- Watchdog: while holding, a thread caps a hold at MAX_HOLD_S.
- ``paused()`` lets every event through (used while Murmur itself types text).
- Limitation: a non-elevated process does not see keys destined for elevated windows.
"""
from __future__ import annotations

import collections
import contextlib
import ctypes
import logging
import queue
import threading
import time
from typing import Callable

import keyboard

log = logging.getLogger(__name__)

SLASH_SCAN = 53
_KNOWN_SCANS = {"/": 53, "`": 41, "m": 50}
MAX_HOLD_S = 600.0
_EXPECT_TTL = 1.0

_EXT_NAMES = {"left", "right", "up", "down", "home", "end", "page up", "page down", "insert", "delete",
              "right ctrl", "right alt", "right windows", "left windows", "menu", "print screen",
              "num lock"}


def _is_ext(e) -> bool:
    name = e.name or ""
    if getattr(e, "is_keypad", False):
        return name in ("enter", "/")
    return name in _EXT_NAMES


_ULONG = ctypes.c_size_t


class _KBI(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", _ULONG)]


class _MI(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", _ULONG)]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KBI), ("mi", _MI)]
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


def _send_scan(scan: int, up: bool, ext: bool = False) -> None:
    flags = 0x8 | (0x2 if up else 0) | (0x1 if ext else 0)   # SCANCODE | KEYUP | EXTENDEDKEY
    inp = _INPUT(type=1, ki=_KBI(0, scan, flags, 0, 0))
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


def scan_for(key: str) -> int | None:
    """Main-keyboard scan code for a key name ("/", "`", "m", "f8", ...), or None."""
    key = (key or "").strip().lower()
    if not key:
        return None
    if key in _KNOWN_SCANS:
        return _KNOWN_SCANS[key]
    try:
        codes = [c for c in keyboard.key_to_scan_codes(key) if c < 0x100]
        return codes[0] if codes else None
    except Exception:
        log.warning("Unknown hotkey %r", key)
        return None


IDLE, PENDING, HOLDING = "idle", "pending", "holding"


class HoldHotkey:
    def __init__(self, keys: dict[str, str] | str = "/", hold_ms: int = 350,
                 on_hold_start: Callable[[str], None] = lambda mode: None,
                 on_hold_end: Callable[[str], None] = lambda mode: None):
        if isinstance(keys, str):
            keys = {keys: "note"}
        self.keys: dict[int, str] = {}           # scan code -> mode
        for name, mode in keys.items():
            sc = scan_for(name)
            if sc is None:
                continue
            if sc in self.keys:
                log.warning("Hotkey %r for %s clashes with %s; ignored", name, mode, self.keys[sc])
                continue
            self.keys[sc] = mode
        self.hold_ms = hold_ms
        self.on_hold_start = on_hold_start
        self.on_hold_end = on_hold_end
        self.enabled = True
        self._lock = threading.RLock()
        self._state = IDLE
        self._timer: threading.Timer | None = None
        self._hook = None
        self._expected: collections.deque = collections.deque()
        self._busy = 0
        self._inject_q: queue.Queue = queue.Queue()
        self._cb_q: queue.Queue = queue.Queue()
        self._running = False
        self._hold_started_at = 0.0
        self._hold_id = 0
        self._active_scan = 0      # hold key that is pending / holding
        self._active_mode = ""
        self._swallow_up: set[int] = set()   # physical hold-key ups still owed after an early replay
        self._paused = 0

    # ---------------------------------------------------------------- public
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._state = IDLE
        self._inject_q = queue.Queue()
        self._cb_q = queue.Queue()
        self._busy = 0
        self._expected.clear()
        threading.Thread(target=self._inject_loop, args=(self._inject_q,), name="murmur-inject", daemon=True).start()
        threading.Thread(target=self._cb_loop, args=(self._cb_q,), name="murmur-callbacks", daemon=True).start()
        self._hook = keyboard.hook(self._on_event, suppress=True)

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._hook is not None:
            try:
                keyboard.unhook(self._hook)
            except Exception:
                log.exception("unhook failed")
            self._hook = None
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            if self._state == HOLDING:
                self._cb_q.put("end")
            elif self._state == PENDING:
                self._busy += 1
                self._inject_q.put(("tap", self._active_scan))   # don't lose a pending key
            self._state = IDLE
        self._inject_q.put(None)
        self._cb_q.put(None)

    @contextlib.contextmanager
    def paused(self):
        """Pass every key event through while active (Murmur typing text itself)."""
        with self._lock:
            self._paused += 1
        try:
            yield
        finally:
            with self._lock:
                self._paused -= 1

    # ------------------------------------------------------------ hook thread
    def _hot_mode(self, e) -> str | None:
        if getattr(e, "is_keypad", False):
            return None
        return self.keys.get(e.scan_code)

    @staticmethod
    def _mods_down() -> bool:
        try:
            for m in ("shift", "ctrl", "alt", "windows"):
                if keyboard.is_pressed(m):
                    return True
        except Exception:
            pass
        return False

    def _on_event(self, e) -> bool:
        """Return True to pass, False to suppress. Must be fast."""
        try:
            return self._handle(e)
        except Exception:
            log.exception("hotkey hook error")
            return True

    def _handle(self, e) -> bool:
        now = time.monotonic()
        is_down = e.event_type == "down"
        with self._lock:
            # 1. our own injected events pass straight through
            exp = self._expected
            while exp and exp[0][2] < now:
                exp.popleft()
            if exp and exp[0][0] == e.scan_code and exp[0][1] == e.event_type:
                exp.popleft()
                return True

            if not self.enabled or not self._running or self._paused:
                return True

            mode = self._hot_mode(e)
            scan = e.scan_code
            st = self._state
            is_active = mode is not None and st != IDLE and scan == self._active_scan

            # 1b. physical hold-key release after we already replayed it (another key interrupted):
            # the replay included an up, so swallow this one (no stray key-up).
            if mode is not None and not is_down and scan in self._swallow_up:
                self._swallow_up.discard(scan)
                return False

            # 2. replay backlog in progress: keep strict ordering
            if self._busy:
                if is_active:
                    return False
                self._enqueue_locked([("key", scan, e.event_type, _is_ext(e))])
                return False

            if mode is not None and (st == IDLE or is_active):
                if is_down:
                    if st != IDLE:
                        return False            # auto-repeat
                    self._swallow_up.discard(scan)
                    if self._mods_down():
                        return True
                    self._state = PENDING
                    self._active_scan, self._active_mode = scan, mode
                    self._timer = threading.Timer(self.hold_ms / 1000.0, self._timer_fired)
                    self._timer.daemon = True
                    self._timer.start()
                    return False
                if st == PENDING:
                    self._timer.cancel()
                    self._state = IDLE
                    self._enqueue_locked([("tap", scan)])
                    return False
                if st == HOLDING:
                    self._end_hold_locked()
                    return False
                return True

            if st == PENDING and is_down:
                self._timer.cancel()
                self._state = IDLE
                self._swallow_up.add(self._active_scan)
                self._enqueue_locked([("tap", self._active_scan), ("key", scan, "down", _is_ext(e))])
                return False
            return True

    # ----------------------------------------------------------------- timers
    def _timer_fired(self) -> None:
        with self._lock:
            if self._state != PENDING:
                return
            self._state = HOLDING
            self._hold_started_at = time.monotonic()
            self._hold_id += 1
            hid = self._hold_id
            self._cb_q.put(("start", self._active_mode))
        threading.Thread(target=self._watchdog, args=(hid,), name="murmur-watchdog", daemon=True).start()

    def _end_hold_locked(self) -> None:
        self._state = IDLE
        self._cb_q.put("end")

    def _watchdog(self, hid: int) -> None:
        while self._running:
            time.sleep(0.2)
            with self._lock:
                if self._state != HOLDING or self._hold_id != hid:
                    return
                if time.monotonic() - self._hold_started_at > MAX_HOLD_S:
                    log.warning("hold exceeded max; ending")
                    self._end_hold_locked()
                    return

    # ---------------------------------------------------------------- workers
    def _enqueue_locked(self, items) -> None:
        self._busy += len(items)
        for it in items:
            self._inject_q.put(it)

    def _send(self, scan: int, etype: str, ext: bool = False) -> None:
        # NOTE: keyboard.send/press set a *global* is_replaying flag that makes the lib's hook
        # ignore every event (including real user keystrokes) while it runs, so we use our own
        # SendInput. The hook sees our events and passes them via the expected-deque.
        with self._lock:
            self._expected.append((scan, etype, time.monotonic() + _EXPECT_TTL))
        _send_scan(scan, etype == "up", ext)

    def _inject_loop(self, q: queue.Queue) -> None:
        while True:
            it = q.get()
            if it is None:
                return
            try:
                if it[0] == "tap":
                    self._send(it[1], "down")
                    self._send(it[1], "up")
                else:
                    self._send(it[1], it[2], it[3])
            except Exception:
                log.exception("inject failed")
            finally:
                with self._lock:
                    self._busy -= 1

    def _cb_loop(self, q: queue.Queue) -> None:
        started = None          # mode of the running hold
        while True:
            it = q.get()
            if it is None:
                if started is not None:
                    self._safe(self.on_hold_end, started)
                return
            if isinstance(it, tuple) and it[0] == "start":
                started = it[1]
                self._safe(self.on_hold_start, started)
            elif it == "end" and started is not None:
                mode, started = started, None
                self._safe(self.on_hold_end, mode)

    @staticmethod
    def _safe(fn, *args) -> None:
        try:
            fn(*args)
        except Exception:
            log.exception("hotkey callback failed")
