"""Murmur notes viewer (Tk, main thread).

Decisions:
- A normal Toplevel of the app's hidden root (taskbar button, title "Murmur"); X / Esc / Ctrl+W only withdraw it.
- Built lazily on first show. Public methods are thread-safe: they enqueue closures that a root.after(30ms) pump
  runs on the Tk thread (same pattern as popup.py). The pump is the only permanent after() loop; the 2 s
  file-change poll only runs while the window is shown, and every per-card timer is tracked and cancelled.
- The Markdown files are the source of truth: notes.load_notes() parses them, delete goes through
  notes.delete_note() (refuses if the file changed underneath us).
- Feed = Canvas + inner Frame of card Frames; note text is a read-only tk.Text (selectable, Ctrl+C) whose height
  is computed with `count -displaylines` and re-fitted on resize. At most MAX_SHOWN newest matches are rendered.
- Construct on the Tk main thread after DPI awareness has been set (sizes scale by winfo_fpixels like popup.py).
"""
from __future__ import annotations

import ctypes
import logging
import queue
import tkinter as tk
from ctypes import wintypes
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import font as tkfont
from typing import Callable

from . import notes as notes_mod

log = logging.getLogger(__name__)

BG = "#1e1f24"
SIDE = "#18191d"
CARD = "#24252b"
CARD_HOT = "#2a2b32"
FG = "#e8e8ea"
MUTED = "#8b8d98"
ACCENT = "#7c9cff"
RED = "#ff6b70"
BORDER = "#2e3038"

MAX_SHOWN = 300
POLL_MS = 2000


def _hex(c: str):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def _mix(a: str, b: str, t: float) -> str:
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


def _is_inside(w, ancestor) -> bool:
    while w is not None:
        if w is ancestor:
            return True
        w = getattr(w, "master", None)
    return False


class NotesWindow:
    def __init__(self, root: tk.Tk, get_notes_dir: Callable[[], Path], on_open_folder: Callable[[], None]):
        self.root = root
        self._get_dir = get_notes_dir
        self._open_folder = on_open_folder
        self.s = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self._q: queue.Queue = queue.Queue()
        self._ico: Path | None = None
        self.win: tk.Toplevel | None = None
        self._notes: list = []
        self._sig = None
        self._mtime = 0.0
        self._filter = "all"
        self._poll_job = None
        self._search_job = None
        self._fit_job = None
        self._card_jobs: set = set()
        self._texts: list[tk.Text] = []
        self._placed = False
        self._styled = False
        self._thumb = (0.0, 1.0)
        self._drag = None
        self._cw = 0
        self.root.after(30, self._pump)

    # ---------------- public, thread-safe ----------------
    def show(self) -> None:
        self._q.put(self._do_show)

    def notify_changed(self) -> None:
        self._q.put(self._do_changed)

    def set_icon(self, ico_path: Path | None) -> None:
        self._q.put(lambda: self._do_icon(ico_path))

    # ---------------- plumbing ----------------
    def _px(self, n: float) -> int:
        return int(round(n * self.s))

    def _pump(self) -> None:
        try:
            while True:
                fn = self._q.get_nowait()
                try:
                    fn()
                except Exception:
                    log.exception("notes window action failed")
        except queue.Empty:
            pass
        try:
            self.root.after(30, self._pump)
        except tk.TclError:
            pass

    def _cancel(self, name: str) -> None:
        j = getattr(self, name)
        if j is not None:
            try:
                self.root.after_cancel(j)
            except Exception:
                pass
            setattr(self, name, None)

    def _later(self, ms: int, fn) -> str:
        """Per-card timer; tracked so a re-render can cancel it."""
        holder = []

        def run():
            self._card_jobs.discard(holder[0])
            try:
                fn()
            except tk.TclError:
                pass
        holder.append(self.root.after(ms, run))
        self._card_jobs.add(holder[0])
        return holder[0]

    def _cancel_card_jobs(self) -> None:
        for j in list(self._card_jobs):
            try:
                self.root.after_cancel(j)
            except Exception:
                pass
        self._card_jobs.clear()

    def _shown(self) -> bool:
        try:
            return self.win is not None and self.win.state() != "withdrawn"
        except tk.TclError:
            return False

    # ---------------- build ----------------
    def _build(self) -> None:
        s, px = self.s, self._px
        w = self.win = tk.Toplevel(self.root)
        w.withdraw()
        w.title("Murmur")
        w.configure(bg=BG)
        w.minsize(px(640), px(420))
        w.protocol("WM_DELETE_WINDOW", self._hide)
        w.bind("<Escape>", self._on_escape)
        w.bind("<Control-f>", self._focus_search)
        w.bind("<Control-F>", self._focus_search)
        w.bind("<Control-w>", lambda e: self._hide())
        self._apply_icon()

        fams = set(tkfont.families(self.root))
        base = "Segoe UI Variable Text" if "Segoe UI Variable Text" in fams else "Segoe UI"
        disp = "Segoe UI Variable Display" if "Segoe UI Variable Display" in fams else "Segoe UI"
        fs = lambda pt: -int(round(pt * 96 / 72 * s))
        mk = lambda fam, pt, wt="normal": tkfont.Font(root=self.root, family=fam, size=fs(pt), weight=wt)
        self.f_brand = mk(disp, 15, "bold")
        self.f_nav = mk(base, 10.5)
        self.f_nav_b = mk(base, 10.5, "bold")
        self.f_small = mk(base, 9)
        self.f_body = mk(base, 11)
        self.f_time = mk(base, 10)
        self.f_day = mk(disp, 11, "bold")
        self.f_act = mk(base, 9.5)
        self.f_empty = mk(disp, 15, "bold")

        # sidebar
        side = tk.Frame(w, bg=SIDE, width=px(216))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Frame(w, bg=BORDER, width=1).pack(side="left", fill="y")
        head = tk.Frame(side, bg=SIDE)
        head.pack(fill="x", padx=px(20), pady=(px(22), px(18)))
        tk.Label(head, text="Murmur", bg=SIDE, fg=FG, font=self.f_brand, anchor="w").pack(fill="x")
        tk.Label(head, text="Hold / to capture", bg=SIDE, fg=MUTED, font=self.f_small, anchor="w").pack(fill="x")
        self.nav = tk.Frame(side, bg=SIDE)
        self.nav.pack(fill="both", expand=True, padx=px(10))

        # main
        main = tk.Frame(w, bg=BG)
        main.pack(side="left", fill="both", expand=True)
        top = tk.Frame(main, bg=BG)
        top.pack(fill="x", padx=px(24), pady=(px(18), px(6)))
        self.sbox = tk.Frame(top, bg=CARD, highlightthickness=1, highlightbackground=BORDER,
                             highlightcolor=ACCENT)
        self.sbox.pack(side="left", fill="x", expand=True)
        ic = tk.Canvas(self.sbox, width=px(18), height=px(18), bg=CARD, highlightthickness=0, bd=0)
        ic.pack(side="left", padx=(px(12), px(2)), pady=px(8))
        ic.create_oval(px(3), px(3), px(11), px(11), outline=MUTED, width=max(1, px(1.5)))
        ic.create_line(px(10), px(10), px(15), px(15), fill=MUTED, width=max(1, px(1.5)), capstyle="round")
        self.svar = tk.StringVar()
        self.entry = tk.Entry(self.sbox, textvariable=self.svar, bg=CARD, fg=FG, insertbackground=FG,
                              relief="flat", bd=0, highlightthickness=0, font=self.f_body,
                              selectbackground=_mix(CARD, ACCENT, 0.5), selectforeground=FG)
        self.entry.pack(side="left", fill="x", expand=True, padx=(px(6), 0), pady=px(8), ipady=px(1))
        self.clear_btn = tk.Label(self.sbox, text="✕", bg=CARD, fg=MUTED, font=self.f_small, cursor="hand2")
        self.clear_btn.bind("<Button-1>", lambda e: self._clear_search())
        self.clear_btn.bind("<Enter>", lambda e: self.clear_btn.configure(fg=FG))
        self.clear_btn.bind("<Leave>", lambda e: self.clear_btn.configure(fg=MUTED))
        self.ph = tk.Label(self.sbox, text="Search notes", bg=CARD, fg=MUTED, font=self.f_body)
        self.ph.place(in_=self.entry, x=0, rely=0.5, anchor="w")
        self.ph.bind("<Button-1>", lambda e: self.entry.focus_set())
        self.entry.bind("<FocusIn>", lambda e: self.sbox.configure(highlightbackground=ACCENT))
        self.entry.bind("<FocusOut>", lambda e: self.sbox.configure(highlightbackground=BORDER))
        self.svar.trace_add("write", self._on_search_change)

        self.count_lbl = tk.Label(top, text="", bg=BG, fg=MUTED, font=self.f_small)
        self.count_lbl.pack(side="left", padx=(px(16), px(12)))
        self.open_btn = self._button(top, "Open folder", self._open_folder)
        self.open_btn.pack(side="left")

        # feed
        area = tk.Frame(main, bg=BG)
        area.pack(fill="both", expand=True, pady=(px(4), 0))
        self.sb = tk.Canvas(area, width=px(12), bg=BG, highlightthickness=0, bd=0)
        self.sb.pack(side="right", fill="y")
        self.cv = tk.Canvas(area, bg=BG, highlightthickness=0, bd=0, yscrollincrement=px(24))
        self.cv.pack(side="left", fill="both", expand=True)
        self.cv.configure(yscrollcommand=self._sb_set)
        self.feed = tk.Frame(self.cv, bg=BG)
        self._feed_id = self.cv.create_window(0, 0, window=self.feed, anchor="nw")
        self.cv.bind("<Configure>", self._on_cv_configure)
        self.feed.bind("<Configure>", self._on_feed_configure)
        self.sb.bind("<Button-1>", self._sb_press)
        self.sb.bind("<B1-Motion>", self._sb_motion)
        self.sb.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None))
        self.sb.bind("<Configure>", lambda e: self._sb_draw())
        self.root.bind_all("<MouseWheel>", self._on_wheel, add="+")

    def _button(self, parent, text, cmd) -> tk.Label:
        px = self._px
        b = tk.Label(parent, text=text, bg=CARD, fg=FG, font=self.f_act, cursor="hand2",
                     padx=px(14), pady=px(7), highlightthickness=1, highlightbackground=BORDER)
        b.bind("<Enter>", lambda e: b.configure(bg=CARD_HOT, highlightbackground=ACCENT))
        b.bind("<Leave>", lambda e: b.configure(bg=CARD, highlightbackground=BORDER))
        b.bind("<Button-1>", lambda e: cmd())
        return b

    # ---------------- window level ----------------
    def _do_icon(self, p) -> None:
        self._ico = Path(p) if p else None
        if self.win is not None:
            self._apply_icon()

    def _apply_icon(self) -> None:
        try:
            if self._ico and self._ico.exists() and self.win is not None:
                self.win.iconbitmap(str(self._ico))
        except Exception:
            log.debug("iconbitmap failed", exc_info=True)

    def _style_dark(self) -> None:
        try:
            h = _hwnd_of(self.win)
            if not h:
                return
            dwm = ctypes.windll.dwmapi

            def put(attr, val):
                v = ctypes.c_int(val)
                dwm.DwmSetWindowAttribute(wintypes.HWND(h), attr, ctypes.byref(v), 4)
            put(20, 1)
            r, g, b = _hex(SIDE)
            put(35, r | (g << 8) | (b << 16))
            r, g, b = _hex(FG)
            put(36, r | (g << 8) | (b << 16))
        except Exception:
            log.debug("dark title bar failed", exc_info=True)

    def _do_show(self) -> None:
        first = self.win is None
        if first:
            self._build()
        w = self.win
        if not self._placed:
            self._placed = True
            ww, wh = self._px(960), self._px(680)
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            ww, wh = min(ww, sw - self._px(40)), min(wh, sh - self._px(80))
            w.geometry(f"{ww}x{wh}+{(sw - ww) // 2}+{max(0, (sh - wh) // 3)}")
        try:
            if w.state() == "iconic":
                w.state("normal")
            w.deiconify()
            w.update_idletasks()
            self._style_dark()
            w.lift()
            w.attributes("-topmost", True)
            w.attributes("-topmost", False)
            w.focus_force()
        except tk.TclError:
            return
        self._cw = 0
        self._reload(force=True)
        if self._poll_job is None:
            self._poll_job = self.root.after(POLL_MS, self._poll)

    def _hide(self) -> None:
        if self.win is None:
            return
        for n in ("_poll_job", "_search_job", "_fit_job"):
            self._cancel(n)
        self._cancel_card_jobs()
        try:
            self.win.withdraw()
        except tk.TclError:
            pass

    def _on_escape(self, _e=None):
        if self.svar.get():
            self._clear_search()
        else:
            self._hide()
        return "break"

    def _focus_search(self, _e=None):
        self.entry.focus_set()
        self.entry.selection_range(0, "end")
        return "break"

    def _do_changed(self) -> None:
        if self._shown():
            self._reload()

    def _poll(self) -> None:
        self._poll_job = None
        if not self._shown():
            return
        try:
            m = notes_mod.notes_mtime(self._get_dir())
            if m != self._mtime:
                self._reload()
        except Exception:
            log.exception("notes poll failed")
        if self._shown() and self._poll_job is None:
            self._poll_job = self.root.after(POLL_MS, self._poll)

    # ---------------- data ----------------
    def _reload(self, force: bool = False) -> None:
        d = self._get_dir()
        self._mtime = notes_mod.notes_mtime(d)
        ns = notes_mod.load_notes(d)
        sig = tuple((n.path.name, n.line_start, n.when, n.text) for n in ns)
        if sig == self._sig and not force:
            return
        self._sig = sig
        self._notes = ns
        self._rebuild_nav()
        self._render(preserve=True)

    def _months(self):
        out: dict[str, int] = {}
        for n in self._notes:
            k = f"{n.when:%Y-%m}"
            out[k] = out.get(k, 0) + 1
        return sorted(out.items(), reverse=True)

    def _rebuild_nav(self) -> None:
        for c in self.nav.winfo_children():
            c.destroy()
        today = datetime.now().date()
        n_today = sum(1 for n in self._notes if n.when.date() == today)
        months = self._months()
        if self._filter.startswith("m:") and self._filter[2:] not in dict(months):
            self._filter = "all"
        items = [("all", "All notes", len(self._notes)), ("today", "Today", n_today)]
        items += [(f"m:{k}", datetime.strptime(k, "%Y-%m").strftime("%B %Y"), c) for k, c in months]
        for i, (key, label, count) in enumerate(items):
            self._nav_item(key, label, count, gap=(i == 2))

    def _nav_item(self, key, label, count, gap=False) -> None:
        px = self._px
        sel = key == self._filter
        bg = _mix(SIDE, ACCENT, 0.16) if sel else SIDE
        outer = tk.Frame(self.nav, bg=SIDE)
        outer.pack(fill="x", pady=(px(14) if gap else px(1), 0))
        row = tk.Frame(outer, bg=bg, cursor="hand2")
        row.pack(fill="x")
        bar = tk.Frame(row, bg=ACCENT if sel else bg, width=px(3))
        bar.pack(side="left", fill="y")
        nm = tk.Label(row, text=label, bg=bg, fg=FG if sel else "#c4c5cc", font=self.f_nav_b if sel else self.f_nav,
                      anchor="w", cursor="hand2")
        nm.pack(side="left", padx=(px(12), 0), pady=px(8))
        ct = tk.Label(row, text=str(count), bg=bg, fg=ACCENT if sel else MUTED, font=self.f_small, cursor="hand2")
        ct.pack(side="right", padx=px(12))
        hover = _mix(SIDE, FG, 0.06)
        parts = (row, nm, ct)
        for p in parts:
            p.bind("<Button-1>", lambda e, k=key: self._set_filter(k))
            if not sel:
                p.bind("<Enter>", lambda e, ps=parts: [x.configure(bg=hover) for x in ps])
                p.bind("<Leave>", lambda e, ps=parts: [x.configure(bg=SIDE) for x in ps])

    def _set_filter(self, key: str) -> None:
        self._filter = key
        self._rebuild_nav()
        self._render(preserve=False)

    # ---------------- search ----------------
    def _on_search_change(self, *_):
        txt = self.svar.get()
        if txt:
            self.ph.place_forget()
            self.clear_btn.pack(side="right", padx=(0, self._px(12)))
        else:
            self.ph.place(in_=self.entry, x=0, rely=0.5, anchor="w")
            self.clear_btn.pack_forget()
        self._cancel("_search_job")
        self._search_job = self.root.after(140, self._search_fire)

    def _search_fire(self) -> None:
        self._search_job = None
        if self._shown():
            self._render(preserve=False)

    def _clear_search(self) -> None:
        self.svar.set("")
        self._cancel("_search_job")
        self._render(preserve=False)

    def _terms(self) -> list[str]:
        return self.svar.get().lower().split()

    def _visible_notes(self):
        today = datetime.now().date()
        ns = self._notes
        if self._filter == "today":
            ns = [n for n in ns if n.when.date() == today]
        elif self._filter.startswith("m:"):
            k = self._filter[2:]
            ns = [n for n in ns if f"{n.when:%Y-%m}" == k]
        terms = self._terms()
        if terms:
            ns = [n for n in ns if all(t in n.text.lower() for t in terms)]
        return ns

    # ---------------- feed rendering ----------------
    @staticmethod
    def _day_label(d) -> str:
        today = datetime.now().date()
        if d == today:
            return "Today"
        if d == today - timedelta(days=1):
            return "Yesterday"
        s = f"{d:%A}, {d.day} {d:%b}"
        return s if d.year == today.year else f"{s} {d.year}"

    def _render(self, preserve: bool) -> None:
        if not self._shown():
            return
        px = self._px
        y0 = self.cv.yview()[0] * max(1, self._feed_h()) if preserve else 0
        self._cancel_card_jobs()
        for c in self.feed.winfo_children():
            c.destroy()
        self._texts = []
        vis = self._visible_notes()
        total = len(vis)
        self.count_lbl.configure(text=f"{total} note{'' if total == 1 else 's'}")
        pad = px(24)
        if not self._notes or not vis:
            if not self._notes:
                a, b = "No notes yet", "Hold / anywhere and start talking."
            elif self.svar.get().strip():
                a, b = f"No notes match “{self.svar.get().strip()}”.", ""
            else:
                a, b = "Nothing here yet", "Notes you capture will show up in this view."
            box = tk.Frame(self.feed, bg=BG)
            box.pack(fill="x", pady=(px(120), 0))
            tk.Label(box, text=a, bg=BG, fg=FG, font=self.f_empty).pack()
            if b:
                tk.Label(box, text=b, bg=BG, fg=MUTED, font=self.f_body).pack(pady=(px(6), 0))
            self._finish_layout(0)
            return
        shown = vis[:MAX_SHOWN]
        terms = self._terms()
        last_day = None
        for n in shown:
            d = n.when.date()
            if d != last_day:
                last_day = d
                tk.Label(self.feed, text=self._day_label(d), bg=BG, fg="#cfd0d8", font=self.f_day,
                         anchor="w").pack(fill="x", padx=pad, pady=(px(16), px(6)))
            self._card(n, terms, pad)
        if total > len(shown):
            tk.Label(self.feed, text=f"Showing {len(shown)} of {total} — refine search", bg=BG, fg=MUTED,
                     font=self.f_small).pack(pady=px(16))
        tk.Frame(self.feed, bg=BG, height=px(20)).pack()
        self._finish_layout(y0)

    def _feed_h(self) -> int:
        return self.feed.winfo_reqheight()

    def _finish_layout(self, y0: float) -> None:
        self.win.update_idletasks()
        cw = self.cv.winfo_width()
        self._cw = cw
        self.cv.itemconfigure(self._feed_id, width=cw)
        self.win.update_idletasks()
        self._fit_texts()
        self.win.update_idletasks()
        h = max(self.feed.winfo_reqheight(), 1)
        self.cv.configure(scrollregion=(0, 0, cw, h))
        self.cv.yview_moveto(min(max(0.0, y0 / h), 1.0) if y0 else 0.0)

    def _lines_needed(self, text: str, maxw: int) -> int:
        """Word-wrap line count (Text's `count -displaylines` proved unreliable before first redraw)."""
        meas = self.f_body.measure
        total = 0
        for para in text.split("\n"):
            words = para.split(" ")
            cur = ""
            lines = 1
            for wd in words:
                trial = f"{cur} {wd}" if cur else wd
                if meas(trial) <= maxw:
                    cur = trial
                    continue
                if cur:
                    lines += 1
                cur = wd
                while meas(cur) > maxw and len(cur) > 1:  # very long token: character wrap
                    k = len(cur)
                    while k > 1 and meas(cur[:k]) > maxw:
                        k -= 1
                    cur = cur[k:]
                    lines += 1
            total += lines
        return max(1, total)

    def _fit_texts(self) -> None:
        for tw in self._texts:
            try:
                maxw = tw.winfo_width() - 3
                if maxw < 40:
                    continue
                tw.configure(height=self._lines_needed(tw.get("1.0", "end-1c"), maxw))
            except tk.TclError:
                pass

    def _on_cv_configure(self, e) -> None:
        if e.width == self._cw or not self._shown():
            return
        self._cw = e.width
        self.cv.itemconfigure(self._feed_id, width=e.width)
        self._cancel("_fit_job")
        self._fit_job = self.root.after(60, self._refit)

    def _refit(self) -> None:
        self._fit_job = None
        if not self._shown():
            return
        self.win.update_idletasks()
        self._fit_texts()
        self.win.update_idletasks()
        self._on_feed_configure()

    def _on_feed_configure(self, _e=None) -> None:
        try:
            self.cv.configure(scrollregion=(0, 0, self.cv.winfo_width(), max(self.feed.winfo_reqheight(), 1)))
        except tk.TclError:
            pass

    # ---------------- cards ----------------
    def _card(self, n, terms, pad) -> None:
        px = self._px
        card = tk.Frame(self.feed, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        card.pack(fill="x", padx=pad, pady=px(3))
        card.grid_columnconfigure(1, weight=1)
        tl = tk.Label(card, text=f"{n.when:%H:%M}", bg=CARD, fg=MUTED, font=self.f_time, anchor="nw")
        tl.grid(row=0, column=0, sticky="nw", padx=(px(18), px(8)), pady=(px(15), px(14)))
        tl.configure(width=5)
        tw = tk.Text(card, wrap="word", bd=0, highlightthickness=0, bg=CARD, fg=FG, font=self.f_body,
                     width=1, height=1, padx=0, pady=0, relief="flat", spacing1=0, spacing3=0,
                     selectbackground=_mix(CARD, ACCENT, 0.5), selectforeground=FG,
                     inactiveselectbackground=_mix(CARD, ACCENT, 0.3), cursor="arrow")
        tw.insert("1.0", n.text)
        tw.tag_configure("hl", foreground=ACCENT, background=_mix(CARD, ACCENT, 0.18))
        for t in terms:
            idx = "1.0"
            cnt = tk.IntVar()
            while True:
                idx = tw.search(t, idx, stopindex="end", nocase=True, count=cnt)
                if not idx or not cnt.get():
                    break
                end = f"{idx}+{cnt.get()}c"
                tw.tag_add("hl", idx, end)
                idx = end
        tw.configure(state="disabled")
        tw.bind("<Button-1>", lambda e: tw.focus_set(), add="+")
        tw.grid(row=0, column=1, sticky="ew", pady=(px(14), px(14)))
        acts = tk.Frame(card, bg=CARD, width=px(150))
        acts.grid(row=0, column=2, sticky="ne", padx=(px(8), px(14)), pady=(px(14), 0))
        self._texts.append(tw)

        st = {"confirm": False, "hot": False}
        copy = tk.Label(acts, text="Copy", bg=CARD, fg=CARD, font=self.f_act, cursor="hand2")
        dele = tk.Label(acts, text="Delete", bg=CARD, fg=CARD, font=self.f_act, cursor="hand2")
        dele.pack(side="right")
        copy.pack(side="right", padx=(0, px(14)))

        def paint():
            hot = st["hot"] or st["confirm"]
            bgc = CARD_HOT if st["hot"] else CARD
            for wdg in (card, tl, tw, acts, copy, dele):
                wdg.configure(bg=bgc)
            card.configure(highlightbackground=_mix(BORDER, ACCENT, 0.45) if st["hot"] else BORDER)
            copy.configure(fg=MUTED if hot else bgc)
            dele.configure(fg=(RED if st["confirm"] else MUTED) if hot else bgc)

        def rebase():
            if st["confirm"]:
                return
            cur = card.winfo_pointerxy()
            wd = card.winfo_containing(*cur)
            st["hot"] = _is_inside(wd, card)
            paint()

        def enter(_e=None):
            st["hot"] = True
            paint()

        def leave(_e=None):
            self._later(40, rebase)

        def bind_tree(wdg):
            wdg.bind("<Enter>", enter, add="+")
            wdg.bind("<Leave>", leave, add="+")
            for c in wdg.winfo_children():
                bind_tree(c)
        bind_tree(card)

        def do_copy(_e=None):
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(n.text)
            except tk.TclError:
                return
            copy.configure(text="Copied", fg=ACCENT)

            def back():
                copy.configure(text="Copy")
                paint()
            self._later(1200, back)

        def do_delete(_e=None):
            if not st["confirm"]:
                st["confirm"] = True
                dele.configure(text="Confirm delete?")
                paint()

                def cancel():
                    st["confirm"] = False
                    dele.configure(text="Delete")
                    rebase()
                self._later(3000, cancel)
                return
            notes_mod.delete_note(n)
            self._reload(force=True)

        for wdg, fn, hv in ((copy, do_copy, FG), (dele, do_delete, RED)):
            wdg.bind("<Button-1>", fn)
            wdg.bind("<Enter>", lambda e, w2=wdg, c=hv: (enter(), w2.configure(fg=c)), add="+")
            wdg.bind("<Leave>", lambda e: (paint(), leave()), add="+")

    # ---------------- scrolling ----------------
    def _on_wheel(self, e) -> None:
        if not self._shown():
            return
        try:
            wd = self.win.winfo_containing(e.x_root, e.y_root)
        except (tk.TclError, KeyError):
            return
        if wd is None or not (_is_inside(wd, self.feed) or wd is self.cv or wd is self.sb):
            return
        if self._thumb[0] <= 0 and self._thumb[1] >= 1:
            return
        steps = -int(e.delta / 120) if abs(e.delta) >= 120 else (-1 if e.delta > 0 else 1)
        self.cv.yview_scroll(steps * 3, "units")
        return "break"

    def _sb_set(self, lo, hi) -> None:
        self._thumb = (float(lo), float(hi))
        self._sb_draw()

    def _sb_draw(self) -> None:
        sb, px = self.sb, self._px
        sb.delete("all")
        lo, hi = self._thumb
        if lo <= 0 and hi >= 1:
            return
        H = max(sb.winfo_height(), 1)
        w = px(5)
        x = sb.winfo_width() // 2
        y1 = max(px(8), int(lo * H) + w // 2)
        y2 = max(y1 + px(24), int(hi * H) - w // 2)
        y2 = min(y2, H - w // 2)
        sb.create_line(x, y1, x, y2, width=w, capstyle="round", fill=_mix(BG, FG, 0.22), tags="thumb")

    def _sb_press(self, e) -> None:
        lo, hi = self._thumb
        H = max(self.sb.winfo_height(), 1)
        if lo <= 0 and hi >= 1:
            return
        if lo * H <= e.y <= hi * H:
            self._drag = e.y - lo * H
        else:
            self._drag = (hi - lo) * H / 2
            self._sb_motion(e)

    def _sb_motion(self, e) -> None:
        if self._drag is None:
            return
        H = max(self.sb.winfo_height(), 1)
        self.cv.yview_moveto(max(0.0, (e.y - self._drag) / H))
