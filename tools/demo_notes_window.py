"""Demo of the notes window with seeded temp notes.

Run: .venv\Scripts\python tools\demo_notes_window.py [--shots DIR]
With --shots, saves screenshots (default view, search "dentist", empty search) into DIR and exits.
"""
import argparse
import ctypes
import os
import sys
import tempfile
import tkinter as tk
from ctypes import wintypes
from datetime import datetime, timedelta
from pathlib import Path

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from PIL import ImageGrab  # noqa: E402

from murmur import storage  # noqa: E402
from murmur.notes_window import NotesWindow, _hwnd_of  # noqa: E402

TEXTS = [
    "Call the dentist to reschedule the cleaning, preferably Thursday afternoon.",
    "Idea for the onboarding flow: show one tip at a time instead of a full tour. People skip tours anyway.",
    "Buy oat milk, coffee filters, lemons, and something for dinner on Friday.",
    "Remember to renew the domain before the end of the month.",
    "The meeting with Priya went well. Action items:\n- send the revised proposal\n- book the follow-up for next week\n- loop in finance about the budget",
    "Book recommendation from Sam: The Overstory by Richard Powers.",
    "Look into why the nightly export job is slow. Probably the missing index on the events table.",
    "Dentist said the next appointment can be done in the evening if needed.",
    "Birthday gift for mum: a good pair of gardening gloves and the herb planter she liked.",
    "Thought while walking: the best notes are the ones I can find again. Tag nothing, search everything.",
    "Pick up the parcel from the post office before 5. Bring the ID and the collection card.",
    "Draft for the newsletter intro: keep it short, lead with the one change that matters, link the details below.",
    "Try the new route to the office, along the canal. Should be about the same time and much nicer.",
    "Ask the landlord about the heating, it has been making that knocking sound again at night.",
    "Weekend plan: long run Saturday morning, farmers market, then finish the shelf in the hallway.",
    "Podcast idea: interview people about the one tool they would never give up. Keep episodes under twenty minutes.",
    "Tax documents: gather the receipts, the freelance invoices, and the donation letter.",
    "Remember that the API key rotates on the first of the month. Update the secrets file and restart the worker.",
]


def seed(nd: Path) -> None:
    now = datetime.now().replace(second=0, microsecond=0)
    stamps = []
    # today and yesterday
    for i, (d, h, m) in enumerate([(0, 9, 12), (0, 11, 40), (0, 13, 5), (1, 8, 30), (1, 16, 45), (1, 21, 10)]):
        stamps.append((now - timedelta(days=d)).replace(hour=h, minute=m))
    # older notes across three months
    for i in range(34):
        days = 2 + i * 2 + (i % 3)
        stamps.append((now - timedelta(days=days)).replace(hour=7 + (i * 5) % 14, minute=(i * 13) % 60))
    stamps = [s for s in stamps if s <= now]
    stamps.sort()
    for i, w in enumerate(stamps):
        storage.append_note(TEXTS[(i * 7 + 3) % len(TEXTS)], nd, w)


def grab(root, win, path: Path) -> None:
    root.update()
    h = _hwnd_of(win)
    rc = wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(wintypes.HWND(h), ctypes.byref(rc))
    ImageGrab.grab((rc.left, rc.top, rc.right, rc.bottom), all_screens=True).save(path)
    print("saved", path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", default=None)
    args = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="murmur_notes_demo_")
    nd = Path(tmp)
    seed(nd)
    root = tk.Tk()
    root.withdraw()
    nw = NotesWindow(root, lambda: nd, lambda: os.startfile(nd))
    try:
        from murmur.icon import save_ico
        nw.set_icon(save_ico(Path(tmp) / "murmur.ico"))
    except Exception:
        pass
    nw.show()

    def later_note():
        storage.append_note("Fresh note added live while the window is open.", nd, datetime.now())
        nw.notify_changed()

    root.after(1500, later_note)

    if args.shots:
        out = Path(args.shots)
        out.mkdir(parents=True, exist_ok=True)

        def step(i=[0]):
            win = nw.win
            if i[0] == 0:
                grab(root, win, out / "1_default.png")
                nw.svar.set("dentist")
            elif i[0] == 1:
                grab(root, win, out / "2_search.png")
                nw.svar.set("zzzxqq")
            elif i[0] == 2:
                grab(root, win, out / "3_empty.png")
                root.quit()
                return
            i[0] += 1
            root.after(900, step)
        root.after(3000, step)
    root.mainloop()


if __name__ == "__main__":
    main()
