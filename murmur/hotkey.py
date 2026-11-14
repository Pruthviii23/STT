"""Hold-to-talk global hotkey (default: "/").

Design notes
- One global ``keyboard.hook(suppress=True)``. The callback does only short, non-blocking
  work under a lock and returns True at once for anything it does not own.
- Slash down (no Shift/Ctrl/Alt/Win) is suppressed and starts a hold timer.
  Tap (up before hold_ms) -> a worker thread injects '/' down+up.
  Another key down while pending -> that key is ALSO suppressed and the worker injects
  '/' tap followed by that key's press, so order is preserved deterministically.
  While the worker still has injections outstanding ("busy"), every further key event
  is suppressed and queued behind them, so later events (e.g. the key-up) can never
  overtake the replay.
- The keyboard lib does not surface LLKHF_INJECTED, so our own injected events are
  recognised via an "expected injections" deque of (scan_code, event_type, deadline).
- on_hold_start / on_hold_end run on one dedicated callback thread, strictly serialised and
  paired (end is only run if start ran).
- Watchdog: while holding, a thread caps a hold at MAX_HOLD_S.
- Limitation: a non-elevated process does not see keys destined for elevated windows.
"""
from __future__ import annotations

import collections
import ctypes
import logging
import queue
import threading
import time
from typing import Callable

import keyboard

log = logging.getLogger(__name__)

SLASH_SCAN = 53
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


IDLE, PENDING, HOLDING = "idle", "pending", "holding"


class HoldHotkey:
    def __init__(self, key: str = "/", hold_ms: int = 350,
                 on_hold_start: Callable[[], None] = lambda: None,
                 on_hold_end: Callable[[], None] = lambda: None):
        self.key = key
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
        self._swallow_up = False   # physical '/' key-up still owed after a tap was replayed early

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
                self._inject_q.put(("tap",))   # don't lose a pending '/'
            self._state = IDLE
        self._inject_q.put(None)
        self._cb_q.put(None)

    # ------------------------------------------------------------ hook thread
    def _is_slash(self, e) -> bool:
        if getattr(e, "is_keypad", False):
            return False
        if self.key == "/":
            return e.name == "/" or e.scan_code == SLASH_SCAN
        return e.name == self.key

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

            if not self.enabled or not self._running:
                return True

            slash = self._is_slash(e)
            st = self._state

            # 1b. physical '/' release after we already replayed it (another key interrupted):
            # the replay included an up, so swallow this one (no stray key-up).
            if slash and not is_down and self._swallow_up:
                self._swallow_up = False
                return False

            # 2. replay backlog in progress: keep strict ordering
            if self._busy:
                if slash and st != IDLE:
                    return False
                self._enqueue_locked([("key", e.scan_code, e.event_type, _is_ext(e))])
                return False

            if slash:
                if is_down:
                    if st != IDLE:
                        return False            # auto-repeat
                    self._swallow_up = False
                    if self._mods_down():
                        return True
                    self._state = PENDING
                    self._timer = threading.Timer(self.hold_ms / 1000.0, self._timer_fired)
                    self._timer.daemon = True
                    self._timer.start()
                    return False
                if st == PENDING:
                    self._timer.cancel()
                    self._state = IDLE
                    self._enqueue_locked([("tap",)])
                    return False
                if st == HOLDING:
                    self._end_hold_locked()
                    return False
                return True

            if st == PENDING and is_down:
                self._timer.cancel()
                self._state = IDLE
                self._swallow_up = True
                self._enqueue_locked([("tap",), ("key", e.scan_code, "down", _is_ext(e))])
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
            self._cb_q.put("start")
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
                    self._send(SLASH_SCAN, "down")
                    self._send(SLASH_SCAN, "up")
                else:
                    self._send(it[1], it[2], it[3])
            except Exception:
                log.exception("inject failed")
            finally:
                with self._lock:
                    self._busy -= 1

    def _cb_loop(self, q: queue.Queue) -> None:
        started = False
        while True:
            it = q.get()
            if it is None:
                if started:
                    self._safe(self.on_hold_end)
                return
            if it == "start":
                started = True
                self._safe(self.on_hold_start)
            elif it == "end" and started:
                started = False
                self._safe(self.on_hold_end)

    @staticmethod
    def _safe(fn) -> None:
        try:
            fn()
        except Exception:
            log.exception("hotkey callback failed")
