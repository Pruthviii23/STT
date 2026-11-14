"""Scripted demo of Popup + Tray + sounds, with a focus-steal check.

Run: .venv\Scripts\python tools\demo_popup.py (opens Notepad briefly; screenshots go to a temp dir).
"""
import ctypes
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from datetime import datetime

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from PIL import ImageGrab  # noqa: E402

from murmur import sounds  # noqa: E402
from murmur.icon import make_icon, save_ico  # noqa: E402
from murmur.popup import Popup  # noqa: E402
from murmur.tray import Tray  # noqa: E402

SHOTS = tempfile.mkdtemp(prefix="murmur_demo_")
u = ctypes.windll.user32
u.GetForegroundWindow.restype = ctypes.c_void_p

LONG = ("So I was thinking about the plan for next week. We should move the design review to Thursday, "
        "ping Alex about the API changes, and book the room for the offsite. Also remember to send the "
        "updated budget to finance before Friday, and double check that the new onboarding flow actually "
        "works on mobile, because last time the keyboard covered the submit button entirely and nobody noticed "
        "until a customer complained about it in the support channel on Monday morning.")


def shot(root, name):
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    box = (sw - 460, sh // 2 - 260, sw, sh // 2 + 260)
    ImageGrab.grab(bbox=box).save(os.path.join(SHOTS, name + ".png"))


def main():
    root = tk.Tk()
    root.withdraw()
    pop = Popup(root)
    save_ico(os.path.join(SHOTS, "murmur.ico"))
    make_icon(128, True).save(os.path.join(SHOTS, "icon_on.png"))
    make_icon(128, False).save(os.path.join(SHOTS, "icon_off.png"))
    tray = Tray(lambda e: print("toggle", e), lambda: print("notes"), lambda: print("folder"),
                lambda: print("settings"), lambda: print("quit"))
    tray.start()
    tray.set_status("Murmur - demo")
    result = {}

    def script():
        np_proc = subprocess.Popen(["notepad.exe"])
        time.sleep(2.0)
        fg0 = u.GetForegroundWindow()
        fgs = []
        sounds.cue_start()
        pop.show_listening()
        t0 = time.time()
        shot_done = False
        while time.time() - t0 < 3.0:
            lvl = max(0.0, 0.08 + 0.1 * math.sin((time.time() - t0) * 3)) * (1 if (time.time() - t0) > 0.5 else 0)
            pop.set_level(lvl)
            fgs.append(u.GetForegroundWindow())
            if not shot_done and time.time() - t0 > 2.0:
                shot(root, "listening")
                shot_done = True
            time.sleep(0.05)
        sounds.cue_stop()
        pop.show_processing()
        time.sleep(0.8)
        shot(root, "processing")
        fgs.append(u.GetForegroundWindow())
        time.sleep(0.7)
        pop.show_result("Remember to call the dentist tomorrow.", datetime.now(), 2.2)
        time.sleep(0.6)
        shot(root, "result_short")
        time.sleep(2.2)
        pop.show_result(LONG, datetime.now(), 3.0)
        time.sleep(0.6)
        shot(root, "result_long")
        fgs.append(u.GetForegroundWindow())
        time.sleep(3.6)
        sounds.cue_error()
        pop.show_message("No microphone found", "Plug in a microphone or check Windows sound settings, then try again.", 2.5)
        time.sleep(0.6)
        shot(root, "message")
        fgs.append(u.GetForegroundWindow())
        time.sleep(3.2)
        result["fg0"], result["fgs"] = fg0, fgs
        subprocess.run(["taskkill", "/f", "/im", "notepad.exe"], capture_output=True)
        tray.stop()
        root.after(0, root.quit)

    threading.Thread(target=script, daemon=True).start()
    root.mainloop()
    ok = all(f == result["fg0"] for f in result["fgs"])
    print("foreground unchanged:", ok, "fg0=", result["fg0"], "samples=", len(result["fgs"]))
    print("distinct fg:", set(result["fgs"]))
    print("screenshots in", SHOTS)


if __name__ == "__main__":
    main()
