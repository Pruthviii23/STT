"""Murmur popup card (Tk, runs on the main thread).

Decisions:
- Everything is drawn on one Canvas; states (listening / processing / result / message) just redraw.
- Never steals focus: WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW applied to the real HWND; topmost re-asserted
  with SWP_NOACTIVATE on every show.
- Rounded corners + border colour via DWM (Win11); falls back to a SetWindowRgn rounded region.
- Thread safety: public methods only enqueue closures; a root.after(15ms) pump on the Tk thread runs them.
  set_level just stores a float (atomic), the animation tick reads it.
- Construct on the Tk main thread with the app's hidden root; the root's mainloop must be running.
"""
from __future__ import annotations

import ctypes
import logging
import math
import queue
import time
import tkinter as tk
from ctypes import wintypes
from datetime import datetime
from tkinter import font as tkfont

log = logging.getLogger(__name__)

BG = "#1e1f24"
FG = "#e8e8ea"
MUTED = "#8b8d98"
ACCENT = "#7c9cff"
RED = "#ff5a5f"
BORDER = "#2e3038"
ALPHA = 0.96

W = 340          # card width (logical px)
PAD = 18
MARGIN = 24
NBARS = 16
TICK_MS = 33

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x80
WS_EX_NOACTIVATE = 0x08000000
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x1, 0x2, 0x4, 0x10, 0x20
HWND_TOPMOST = -1


def _hex(c: str):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def _mix(a: str, b: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    ra, rb = _hex(a), _hex(b)
    return "#%02x%02x%02x" % tuple(int(x + (y - x) * t) for x, y in zip(ra, rb))


def _hwnd_of(win: tk.Misc) -> int:
    try:
        u = ctypes.windll.user32
        u.GetParent.restype = wintypes.HWND
        u.GetParent.argtypes = [wintypes.HWND]
        i = win.winfo_id()
        return u.GetParent(i) or i
    except Exception:
        return 0


class Popup:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.s = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self._q: queue.Queue = queue.Queue()
        self._level = 0.0
        self._state = "hidden"
        self._tick_job = self._hide_job = self._fade_job = None
        self._t0 = 0.0
        self._lvl_s = 0.0
        self._bars = [0.0] * NBARS
        self._dyn: dict = {}
        self._hwnd = 0
        self._styled = False
        self._need_region = False

        w = self.win = tk.Toplevel(root)
        w.withdraw()
        w.overrideredirect(True)
        w.configure(bg=BG)
        w.wm_attributes("-topmost", True)
        w.wm_attributes("-alpha", 0.0)
        self.cv = tk.Canvas(w, bg=BG, highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)

        fams = set(tkfont.families(root))
        base = "Segoe UI Variable Text" if "Segoe UI Variable Text" in fams else "Segoe UI"
        disp = "Segoe UI Variable Display" if "Segoe UI Variable Display" in fams else "Segoe UI"
        fs = lambda pt: -int(round(pt * 96 / 72 * self.s))
        self.f_title = tkfont.Font(root=root, family=disp, size=fs(11.5), weight="bold")
        self.f_body = tkfont.Font(root=root, family=base, size=fs(11))
        self.f_small = tkfont.Font(root=root, family=base, size=fs(9))
        self.f_time = tkfont.Font(root=root, family=base, size=fs(10.5))
        self.root.after(15, self._pump)

    # ---------------- public, thread-safe ----------------
    def show_listening(self) -> None:
        self._q.put(self._do_listening)

    def set_level(self, level: float) -> None:
        try:
            self._level = max(0.0, min(1.0, float(level)))
        except Exception:
            pass

    def show_processing(self) -> None:
        self._q.put(self._do_processing)

    def show_result(self, text: str, when: datetime, seconds: float = 4.0) -> None:
        self._q.put(lambda: self._do_result(text, when, seconds))

    def show_message(self, title: str, body: str, seconds: float = 3.0) -> None:
        self._q.put(lambda: self._do_message(title, body, seconds))

    def hide(self) -> None:
        self._q.put(self._do_hide)

    # ---------------- plumbing ----------------
    def _pump(self) -> None:
        try:
            while True:
                fn = self._q.get_nowait()
                try:
                    fn()
                except Exception:
                    log.exception("popup action failed")
        except queue.Empty:
            pass
        try:
            self.root.after(15, self._pump)
        except tk.TclError:
            pass

    def _cancel(self, *names) -> None:
        for n in names:
            j = getattr(self, n)
            if j is not None:
                try:
                    self.root.after_cancel(j)
                except Exception:
                    pass
                setattr(self, n, None)

    def _style_window(self) -> None:
        try:
            self.win.update_idletasks()
            self._hwnd = h = _hwnd_of(self.win)
            if not h:
                return
            u = ctypes.windll.user32
            u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
            u.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
            u.SetWindowLongPtrW.restype = ctypes.c_ssize_t
            u.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
            ex = u.GetWindowLongPtrW(h, GWL_EXSTYLE)
            u.SetWindowLongPtrW(h, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
            u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, wintypes.UINT]
            u.SetWindowPos(h, None, 0, 0, 0, 0,
                           SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)
            if not self._styled:
                self._round_corners(h)
                self._styled = True
        except Exception:
            log.debug("window styling failed", exc_info=True)

    def _round_corners(self, h: int) -> None:
        ok = False
        try:
            dwm = ctypes.windll.dwmapi
            pref = ctypes.c_int(2)  # DWMWCP_ROUND
            r1 = dwm.DwmSetWindowAttribute(wintypes.HWND(h), 33, ctypes.byref(pref), 4)
            r, g, b = _hex(BORDER)
            col = ctypes.c_uint(r | (g << 8) | (b << 16))
            dwm.DwmSetWindowAttribute(wintypes.HWND(h), 34, ctypes.byref(col), 4)  # border colour
            ok = (r1 == 0)
        except Exception:
            pass
        self._need_region = not ok

    def _apply_region(self, w: int, h: int) -> None:
        if not self._need_region or not self._hwnd:
            return
        try:
            rad = int(16 * self.s)
            rgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, rad, rad)
            ctypes.windll.user32.SetWindowRgn(wintypes.HWND(self._hwnd), rgn, True)
        except Exception:
            pass

    def _workarea(self):
        try:
            rc = wintypes.RECT()
            ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rc), 0)
            if rc.right > rc.left:
                return rc.left, rc.top, rc.right, rc.bottom
        except Exception:
            pass
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def _place(self, h: int) -> None:
        w = int(W * self.s)
        l, t, r, b = self._workarea()
        x = r - w - int(MARGIN * self.s)
        y = t + (b - t - h) // 2
        self.win.geometry(f"{w}x{h}+{x}+{y}")
        self.cv.configure(width=w, height=h)
        self._apply_region(w, h)

    def _begin(self, state: str, h: int) -> None:
        """Common state switch: cancel timers, resize, draw frame, make visible."""
        self._cancel("_tick_job", "_hide_job", "_fade_job")
        self._state = state
        self._dyn = {}
        h = int(h)
        was_hidden = not self.win.winfo_viewable()
        self._place(h)
        self.cv.delete("all")
        w = int(W * self.s)
        self.cv.create_rectangle(0, 0, w - 1, h - 1, outline=BORDER, fill=BG, width=1)
        if was_hidden:
            self.win.wm_attributes("-alpha", 0.0)
            self.win.deiconify()
            self._style_window()
            self._place(h)
        self._reassert_top()

    def _reassert_top(self) -> None:
        try:
            self.win.wm_attributes("-topmost", True)
            if self._hwnd:
                ctypes.windll.user32.SetWindowPos(wintypes.HWND(self._hwnd), wintypes.HWND(HWND_TOPMOST),
                                                  0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        except Exception:
            pass

    def _fade_to(self, target: float, ms: int, then=None) -> None:
        self._cancel("_fade_job")
        try:
            start = float(self.win.wm_attributes("-alpha"))
        except Exception:
            start = 0.0
        t0 = time.perf_counter()

        def step():
            self._fade_job = None
            k = min(1.0, (time.perf_counter() - t0) * 1000 / max(ms, 1))
            k = k * k * (3 - 2 * k)
            try:
                self.win.wm_attributes("-alpha", start + (target - start) * k)
            except tk.TclError:
                return
            if k < 1.0:
                self._fade_job = self.root.after(15, step)
            elif then:
                then()
        step()

    def _schedule_tick(self, fn) -> None:
        def loop():
            self._tick_job = None
            fn()
            if self._tick_job is None and self._state in ("listening", "processing", "result"):
                self._tick_job = self.root.after(TICK_MS, loop)
        self._tick_job = self.root.after(TICK_MS, loop)

    # ---------------- text helpers ----------------
    def _wrap(self, text: str, font: tkfont.Font, maxw: int, maxlines: int) -> list[str]:
        lines: list[str] = []
        for para in text.replace("\r", "").split("\n"):
            words = para.split()
            if not words:
                lines.append("")
                continue
            cur = ""
            for wd in words:
                while font.measure(wd) > maxw:  # break very long tokens
                    k = len(wd)
                    while k > 1 and font.measure(wd[:k]) > maxw:
                        k -= 1
                    if cur:
                        lines.append(cur)
                        cur = ""
                    lines.append(wd[:k])
                    wd = wd[k:]
                trial = f"{cur} {wd}".strip()
                if font.measure(trial) <= maxw:
                    cur = trial
                else:
                    lines.append(cur)
                    cur = wd
            lines.append(cur)
        while lines and not lines[-1]:
            lines.pop()
        if len(lines) > maxlines:
            lines = lines[:maxlines]
            last = lines[-1].rstrip(" .,;:")
            while last and font.measure(last + "…") > maxw:
                last = last[:-1]
            lines[-1] = last.rstrip() + "…"
        return lines

    # ---------------- states ----------------
    def _row_y(self) -> float:
        return PAD * self.s + 12 * self.s  # centre of the 24px header row

    def _do_listening(self) -> None:
        s = self.s
        self._begin("listening", (PAD + 24 + 12 + 30 + PAD) * s)
        self._t0 = time.perf_counter()
        self._bars = [0.0] * NBARS
        self._lvl_s = 0.0
        cv, cy = self.cv, self._row_y()
        self._dyn["halo"] = cv.create_oval(0, 0, 0, 0, fill=BG, outline="")
        cv.create_oval(PAD * s + 4 * s, cy - 5 * s, PAD * s + 14 * s, cy + 5 * s, fill=RED, outline="")
        self._dot_c = (PAD * s + 9 * s, cy)
        cv.create_text((PAD + 26) * s, cy, text="Listening…", anchor="w", fill=FG, font=self.f_title)
        self._dyn["time"] = cv.create_text((W - PAD) * s, cy, text="00:00", anchor="e", fill=MUTED,
                                           font=self.f_time)
        bw = 8 * s
        avail = (W - 2 * PAD) * s
        gap = (avail - NBARS * bw) / (NBARS - 1)
        by = (PAD + 24 + 12 + 15) * s
        self._bar_geo = (bw, gap, by)
        self._dyn["bars"] = [
            cv.create_line(0, 0, 0, 0, width=bw, capstyle="round", fill=ACCENT) for _ in range(NBARS)]
        self._tick_listening()
        self._schedule_tick(self._tick_listening)
        self._fade_to(ALPHA, 140)

    def _tick_listening(self) -> None:
        s, cv = self.s, self.cv
        t = time.perf_counter() - self._t0
        p = (math.sin(t * 4.2) + 1) / 2
        cx, cy = self._dot_c
        r = (6 + 4 * p) * s
        cv.coords(self._dyn["halo"], cx - r, cy - r, cx + r, cy + r)
        cv.itemconfigure(self._dyn["halo"], fill=_mix(BG, RED, 0.38 * (1 - p) + 0.08))
        sec = int(t)
        cv.itemconfigure(self._dyn["time"], text=f"{sec // 60:02d}:{sec % 60:02d}")
        # level smoothing (fast attack, slow decay)
        raw = min(1.0, math.sqrt(self._level) * 1.5)
        self._lvl_s = raw if raw > self._lvl_s else self._lvl_s * 0.86 + raw * 0.14
        bw, gap, by = self._bar_geo
        minh, maxh = 3 * s, 28 * s
        for i, item in enumerate(self._dyn["bars"]):
            prof = 0.55 + 0.45 * abs(math.sin(t * 5.5 + i * 0.85))
            tgt = self._lvl_s * prof
            cur = self._bars[i]
            cur += (tgt - cur) * (0.55 if tgt > cur else 0.18)
            self._bars[i] = cur
            h = minh + (maxh - minh) * cur
            x = PAD * s + bw / 2 + i * (bw + gap)
            cv.coords(item, x, by - h / 2 + bw / 2, x, by + h / 2 - bw / 2)
            cv.itemconfigure(item, fill=_mix("#3a3d4a", ACCENT, 0.35 + 0.65 * min(1.0, cur * 1.6)))

    def _do_processing(self) -> None:
        s = self.s
        self._begin("processing", (PAD * 2 + 24) * s)
        self._t0 = time.perf_counter()
        cv, cy = self.cv, self._row_y()
        n, R = 8, 7 * s
        cx = PAD * s + 9 * s
        self._dyn["spin"] = []
        for i in range(n):
            a = 2 * math.pi * i / n - math.pi / 2
            x, y = cx + R * math.cos(a), cy + R * math.sin(a)
            self._dyn["spin"].append(cv.create_oval(x - 1.8 * s, y - 1.8 * s, x + 1.8 * s, y + 1.8 * s,
                                                    fill=ACCENT, outline=""))
        cv.create_text((PAD + 26) * s, cy, text="Transcribing", anchor="w", fill=FG, font=self.f_title)
        tw = self.f_title.measure("Transcribing")
        self._dyn["dots"] = cv.create_text((PAD + 26) * s + tw + 1 * s, cy, text="", anchor="w",
                                           fill=MUTED, font=self.f_title)
        self._tick_processing()
        self._schedule_tick(self._tick_processing)
        self._fade_to(ALPHA, 120)

    def _tick_processing(self) -> None:
        t = time.perf_counter() - self._t0
        n = 8
        head = (t * 9) % n
        for i, item in enumerate(self._dyn["spin"]):
            d = (head - i) % n
            self.cv.itemconfigure(item, fill=_mix(BG, ACCENT, max(0.12, 1 - d / 5.5)))
        self.cv.itemconfigure(self._dyn["dots"], text="." * (int(t * 2.5) % 4))

    def _do_result(self, text: str, when: datetime, seconds: float) -> None:
        s = self.s
        text = (text or "").strip() or "(nothing heard)"
        maxw = int((W - 2 * PAD) * s)
        lines = self._wrap(text, self.f_body, maxw, 8)
        lh = self.f_body.metrics("linespace") + int(2 * s)
        head_h = 16 * s
        h = PAD * s + head_h + 10 * s + lh * len(lines) + PAD * s - 2 * s
        self._begin("result", h)
        cv = self.cv
        stamp = f"{when.strftime('%a')} {when.day} {when.strftime('%b')} · {when.strftime('%H:%M')}"
        y0 = PAD * s + head_h / 2
        cv.create_text(PAD * s, y0, text=stamp, anchor="w", fill=MUTED, font=self.f_small)
        cv.create_text((W - PAD) * s, y0, text="✓ Saved to notes", anchor="e", fill=ACCENT, font=self.f_small)
        y = PAD * s + head_h + 10 * s
        for ln in lines:
            cv.create_text(PAD * s, y, text=ln, anchor="nw", fill=FG, font=self.f_body)
            y += lh
        w = int(W * s)
        hh = int(h)
        self._dyn["bar"] = cv.create_line(1, hh - 2 * s, w - 1, hh - 2 * s, fill=_mix(BG, ACCENT, 0.55),
                                          width=max(1, int(2 * s)))
        self._countdown(hh, w, seconds)
        self._fade_to(ALPHA, 140)

    def _countdown(self, h: int, w: int, seconds: float) -> None:
        t0 = time.perf_counter()
        total = max(0.2, seconds)
        s = self.s

        def tick():
            k = (time.perf_counter() - t0) / total
            if k >= 1:
                self._begin_fadeout()
                return
            x = 1 + (w - 2) * (1 - k)
            self.cv.coords(self._dyn["bar"], 1, h - 2 * s, x, h - 2 * s)
        self._schedule_tick(tick)

    def _do_message(self, title: str, body: str, seconds: float) -> None:
        s = self.s
        maxw = int((W - 2 * PAD) * s)
        tl = self._wrap(title or "", self.f_title, maxw - int(20 * s), 2)
        bl = self._wrap(body or "", self.f_body, maxw, 5)
        lh_t = self.f_title.metrics("linespace") + int(1 * s)
        lh_b = self.f_body.metrics("linespace") + int(2 * s)
        h = PAD * 2 * s + lh_t * len(tl) + (8 * s + lh_b * len(bl) if bl else 0)
        self._begin("message", h)
        cv = self.cv
        y = PAD * s
        cv.create_oval(PAD * s + 1 * s, y + lh_t / 2 - 4 * s, PAD * s + 9 * s, y + lh_t / 2 + 4 * s,
                       fill=ACCENT, outline="")
        for ln in tl:
            cv.create_text((PAD + 20) * s, y, text=ln, anchor="nw", fill=FG, font=self.f_title)
            y += lh_t
        y += 8 * s
        for ln in bl:
            cv.create_text(PAD * s, y, text=ln, anchor="nw", fill=MUTED, font=self.f_body)
            y += lh_b
        self._fade_to(ALPHA, 140)
        self._hide_job = self.root.after(int(max(0.3, seconds) * 1000), self._begin_fadeout)

    def _begin_fadeout(self) -> None:
        self._cancel("_tick_job", "_hide_job")
        self._state = "fading"
        self._fade_to(0.0, 320, then=self._finish_hide)

    def _finish_hide(self) -> None:
        if self._state == "fading":
            self._do_hide()

    def _do_hide(self) -> None:
        self._cancel("_tick_job", "_hide_job", "_fade_job")
        self._state = "hidden"
        try:
            self.win.wm_attributes("-alpha", 0.0)
            self.win.withdraw()
        except tk.TclError:
            pass
