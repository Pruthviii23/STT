"""Deterministic state-machine test for HoldHotkey (no real input, no focus needed).

``typed`` models what the focused app receives: every event whose _handle() returned True
(pass-through) plus every injected event (fed back through _handle like the real hook does).
Run: .venv\\Scripts\\python tests\\test_hotkey_logic.py
"""
import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import murmur.hotkey as hkmod  # noqa: E402
from murmur.hotkey import HoldHotkey  # noqa: E402

NAMES = {53: "/", 31: "s", 30: "a", 42: "shift", 41: "`", 50: "m"}
HOLD_MS = 100


def ev(scan, kind):
    return SimpleNamespace(name=NAMES.get(scan, str(scan)), scan_code=scan, event_type=kind, is_keypad=False)


class Harness:
    def __init__(self, mods=False, enabled=True, keys="/"):
        self.typed = []          # (name, kind) as seen by the target app
        self.injected = []       # (scan, kind) as recorded by _send_scan
        self.cb = []
        self.cb_lock = threading.Lock()
        self.modes = []
        self.hk = HoldHotkey(keys, HOLD_MS, on_hold_start=lambda m: self._cb("start", m),
                             on_hold_end=lambda m: self._cb("end", m))
        self.hk.enabled = enabled
        self.patches = [
            mock.patch.object(hkmod, "_send_scan", self._send_scan),
            mock.patch.object(hkmod.keyboard, "hook", lambda *a, **k: object()),
            mock.patch.object(hkmod.keyboard, "unhook", lambda *a, **k: None),
            mock.patch.object(HoldHotkey, "_mods_down", staticmethod(lambda: mods)),
        ]

    def _cb(self, name, mode):
        with self.cb_lock:
            self.cb.append(name)
            self.modes.append(mode)

    def _send_scan(self, scan, up, ext=False):
        kind = "up" if up else "down"
        self.injected.append((scan, kind))
        e = ev(scan, kind)
        if self.hk._handle(e):
            self.typed.append((e.name, kind))

    def feed(self, scan, kind):
        e = ev(scan, kind)
        if self.hk._handle(e):
            self.typed.append((e.name, kind))

    def __enter__(self):
        for p in self.patches:
            p.start()
        self.hk.start()
        return self

    def __exit__(self, *a):
        self.hk.stop()
        time.sleep(0.05)
        for p in self.patches:
            p.stop()

    def settle(self, t=0.15):
        time.sleep(t)
        deadline = time.time() + 2
        while self.hk._busy and time.time() < deadline:
            time.sleep(0.01)


SL, S, SH, BT, M = 53, 31, 42, 41, 50
MULTI = {"/": "note", "`": "dictate", "m": "formal"}


class HotkeyLogic(unittest.TestCase):
    def test_quick_tap(self):
        with Harness() as h:
            h.feed(SL, "down")
            time.sleep(0.02)
            h.feed(SL, "up")
            h.settle()
            self.assertEqual(h.injected, [(SL, "down"), (SL, "up")])
            self.assertEqual(h.typed, [("/", "down"), ("/", "up")])
            self.assertEqual(h.cb, [])
            self.assertEqual(h.hk._state, "idle")

    def test_slash_then_s_before_up(self):
        with Harness() as h:
            h.feed(SL, "down")
            time.sleep(0.02)
            h.feed(S, "down")
            h.feed(S, "up")
            h.feed(SL, "up")
            h.settle()
            self.assertEqual(h.typed[:3], [("/", "down"), ("/", "up"), ("s", "down")])
            self.assertEqual(h.typed.count(("/", "down")), 1)
            self.assertEqual(h.typed.count(("/", "up")), 1)
            self.assertIn(("s", "up"), h.typed)
            self.assertEqual(h.typed.index(("s", "up")), 3)
            self.assertEqual(h.cb, [])
            self.assertEqual(h.hk._state, "idle")
            self.assertEqual(h.hk._busy, 0)
            # and the machine still works afterwards
            h.typed.clear()
            h.feed(SL, "down"); h.feed(SL, "up"); h.settle()
            self.assertEqual(h.typed, [("/", "down"), ("/", "up")])

    def test_s_up_after_slash_up(self):
        with Harness() as h:
            h.feed(SL, "down")
            h.feed(S, "down")
            h.feed(SL, "up")
            h.feed(S, "up")
            h.settle()
            self.assertEqual(h.typed, [("/", "down"), ("/", "up"), ("s", "down"), ("s", "up")])
            self.assertEqual(h.hk._state, "idle")

    def test_shift_passthrough(self):
        with Harness(mods=True) as h:
            h.feed(SL, "down")
            h.feed(SL, "up")
            h.settle()
            self.assertEqual(h.typed, [("/", "down"), ("/", "up")])
            self.assertEqual(h.injected, [])
            self.assertEqual(h.cb, [])

    def test_hold_with_autorepeat(self):
        with Harness() as h:
            t_end = time.time() + 0.45
            h.feed(SL, "down")
            while time.time() < t_end:
                time.sleep(0.03)
                h.feed(SL, "down")          # auto-repeat
            h.feed(SL, "up")
            h.settle()
            self.assertEqual(h.cb, ["start", "end"])
            self.assertEqual(h.injected, [])
            self.assertEqual(h.typed, [])
            self.assertEqual(h.hk._state, "idle")

    def test_disabled_passthrough(self):
        with Harness(enabled=False) as h:
            h.feed(SL, "down")
            time.sleep(HOLD_MS / 1000 + 0.1)
            h.feed(SL, "up")
            h.settle()
            self.assertEqual(h.typed, [("/", "down"), ("/", "up")])
            self.assertEqual(h.cb, [])
            self.assertEqual(h.injected, [])

    def test_stop_during_pending_types_slash(self):
        h = Harness()
        with h:
            h.feed(SL, "down")
            time.sleep(0.02)
        # __exit__ called stop()
        time.sleep(0.1)
        self.assertEqual(h.injected, [(SL, "down"), (SL, "up")])
        self.assertEqual(h.cb, [])

    def test_stop_during_hold_ends_it(self):
        h = Harness()
        with h:
            h.feed(SL, "down")
            time.sleep(HOLD_MS / 1000 + 0.1)
        time.sleep(0.1)
        self.assertEqual(h.cb, ["start", "end"])


    def test_multi_key_modes(self):
        with Harness(keys=MULTI) as h:
            for scan, mode in ((SL, "note"), (BT, "dictate"), (M, "formal")):
                h.cb.clear(); h.modes.clear()
                h.feed(scan, "down")
                time.sleep(HOLD_MS / 1000 + 0.1)
                h.feed(scan, "down")        # auto-repeat
                h.feed(scan, "up")
                h.settle()
                self.assertEqual(h.cb, ["start", "end"])
                self.assertEqual(h.modes, [mode, mode])
            self.assertEqual(h.typed, [])

    def test_multi_key_taps_type_their_own_key(self):
        with Harness(keys=MULTI) as h:
            for scan in (BT, M, SL):
                h.feed(scan, "down"); time.sleep(0.02); h.feed(scan, "up"); h.settle()
            self.assertEqual(h.typed, [("`", "down"), ("`", "up"), ("m", "down"), ("m", "up"),
                                       ("/", "down"), ("/", "up")])
            self.assertEqual(h.cb, [])

    def test_m_rollover_keeps_order(self):
        # fast typing "ms": m down, s down, m up, s up
        with Harness(keys=MULTI) as h:
            h.feed(M, "down"); h.feed(S, "down"); h.feed(M, "up"); h.feed(S, "up")
            h.settle()
            self.assertEqual(h.typed, [("m", "down"), ("m", "up"), ("s", "down"), ("s", "up")])
            self.assertEqual(h.cb, [])

    def test_other_hold_key_while_pending_is_typed(self):
        # "m`" typed fast: the second hold key is just a key, no hold starts
        with Harness(keys=MULTI) as h:
            h.feed(M, "down"); h.feed(BT, "down"); h.feed(M, "up"); h.feed(BT, "up")
            h.settle()
            self.assertEqual(h.typed, [("m", "down"), ("m", "up"), ("`", "down"), ("`", "up")])
            self.assertEqual(h.cb, [])
            self.assertEqual(h.hk._state, "idle")

    def test_paused_passes_everything(self):
        with Harness(keys=MULTI) as h:
            with h.hk.paused():
                h.feed(M, "down")
                time.sleep(HOLD_MS / 1000 + 0.1)
                h.feed(M, "up")
            h.settle()
            self.assertEqual(h.typed, [("m", "down"), ("m", "up")])
            self.assertEqual(h.cb, [])

    def test_empty_key_disabled(self):
        hk = HoldHotkey({"/": "note", "": "dictate"}, HOLD_MS)
        self.assertEqual(list(hk.keys.values()), ["note"])


if __name__ == "__main__":
    unittest.main()
