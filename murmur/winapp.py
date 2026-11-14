"""Windows integration helpers: AppUserModelID, single-instance + show-window event, shortcuts.

Shortcut creation uses raw COM through ctypes (IShellLinkW + IPersistFile + IPropertyStore) so the
System.AppUserModel.ID property can be stamped on the .lnk (needed for the taskbar to merge a pinned
shortcut with the running window). No third-party dependencies.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import uuid
from ctypes import wintypes
from pathlib import Path

AUMID = "Murmur.VoiceNotes"
ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0
INFINITE = 0xFFFFFFFF

log = logging.getLogger("murmur.winapp")


def _suffix() -> str:
    return os.environ.get("MURMUR_INSTANCE", "")


def mutex_name() -> str:
    return "Local\\MurmurVoiceNotes" + _suffix()


def event_name() -> str:
    return "Local\\MurmurVoiceNotes.Show" + _suffix()


def set_app_id() -> None:
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(AUMID))
    except Exception:
        log.debug("SetCurrentProcessExplicitAppUserModelID failed", exc_info=True)


def _k32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.restype = ctypes.c_void_p
    k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k.CreateEventW.restype = ctypes.c_void_p
    k.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    k.SetEvent.argtypes = [ctypes.c_void_p]
    k.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    k.WaitForSingleObject.restype = wintypes.DWORD
    return k


_mutex = None
_event = None


def acquire_single_instance() -> bool:
    """True if we are the first instance (and now own the mutex)."""
    global _mutex
    _mutex = _k32().CreateMutexW(None, False, mutex_name())
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def _open_event():
    return _k32().CreateEventW(None, False, False, event_name())   # auto-reset, non-signalled


def signal_show() -> None:
    """Ask the running instance to show its window."""
    h = _open_event()
    if h:
        _k32().SetEvent(h)


def start_show_listener(callback) -> None:
    """Daemon thread: call callback() each time another launch signals the show event."""
    global _event
    _event = _open_event()
    k = _k32()

    def loop():
        while True:
            if k.WaitForSingleObject(_event, INFINITE) != WAIT_OBJECT_0:
                return
            log.info("show requested")
            try:
                callback()
            except Exception:
                log.exception("show callback failed")

    threading.Thread(target=loop, name="murmur-activate", daemon=True).start()


# ------------------------------------------------------------------ shortcuts (COM via ctypes)
class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]


def _guid(s: str) -> _GUID:
    u = uuid.UUID(s)
    return _GUID(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", wintypes.DWORD)]


class _PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                ("r3", ctypes.c_ushort), ("p", ctypes.c_void_p), ("p2", ctypes.c_void_p)]


CLSID_ShellLink = "00021401-0000-0000-C000-000000000046"
IID_IShellLinkW = "000214F9-0000-0000-C000-000000000046"
IID_IPersistFile = "0000010B-0000-0000-C000-000000000046"
IID_IPropertyStore = "886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"
PKEY_AUMID = ("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3", 5)


def _m(obj, idx, *argtypes):
    vt = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    return ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vt[idx])


def _qi(obj, iid: str) -> ctypes.c_void_p:
    out = ctypes.c_void_p()
    g = _guid(iid)
    _m(obj, 0, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))(obj, ctypes.byref(g), ctypes.byref(out))
    return out


def _release(obj) -> None:
    try:
        vt = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vt[2])(obj)
    except Exception:
        pass


def create_shortcut(lnk_path, target, arguments: str = "", workdir=None, icon=None,
                    description: str = "", app_id: str | None = AUMID, minimized: bool = False) -> Path:
    lnk_path = Path(lnk_path)
    lnk_path.parent.mkdir(parents=True, exist_ok=True)
    ole32 = ctypes.windll.ole32
    ole32.CoInitialize(None)
    link = ctypes.c_void_p()
    clsid, iid = _guid(CLSID_ShellLink), _guid(IID_IShellLinkW)
    ole32.CoCreateInstance.argtypes = [ctypes.POINTER(_GUID), ctypes.c_void_p, wintypes.DWORD,
                                       ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
    hr = ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(link))
    if hr != 0:
        raise OSError(f"CoCreateInstance(ShellLink) failed: {hr:#x}")
    pf = ps = None
    try:
        W = wintypes.LPCWSTR
        _m(link, 20, W)(link, str(target))                       # SetPath
        if arguments:
            _m(link, 11, W)(link, arguments)                     # SetArguments
        if workdir:
            _m(link, 9, W)(link, str(workdir))                   # SetWorkingDirectory
        if description:
            _m(link, 7, W)(link, description)                    # SetDescription
        if icon:
            _m(link, 17, W, ctypes.c_int)(link, str(icon), 0)    # SetIconLocation
        if minimized:
            _m(link, 15, ctypes.c_int)(link, 7)                  # SetShowCmd SW_SHOWMINNOACTIVE
        if app_id:
            ps = _qi(link, IID_IPropertyStore)
            key = _PROPERTYKEY(_guid(PKEY_AUMID[0]), PKEY_AUMID[1])
            pv = _PROPVARIANT()
            buf = (app_id + chr(0)).encode("utf-16-le")
            ole32.CoTaskMemAlloc.restype = ctypes.c_void_p
            ole32.CoTaskMemAlloc.argtypes = [ctypes.c_size_t]
            mem = ole32.CoTaskMemAlloc(len(buf))
            ctypes.memmove(mem, buf, len(buf))
            pv.vt = 31                                           # VT_LPWSTR (freed by PropVariantClear)
            pv.p = mem
            _m(ps, 6, ctypes.POINTER(_PROPERTYKEY), ctypes.POINTER(_PROPVARIANT))(
                ps, ctypes.byref(key), ctypes.byref(pv))
            _m(ps, 7)(ps)                                        # Commit
            ole32.PropVariantClear(ctypes.byref(pv))
        pf = _qi(link, IID_IPersistFile)
        _m(pf, 6, W, wintypes.BOOL)(pf, str(lnk_path), True)     # Save
    finally:
        for o in (ps, pf, link):
            if o:
                _release(o)
    return lnk_path


def _pythonw() -> Path:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return w if w.exists() else exe


def _default_dirs(startup_dir, start_menu_dir):
    appdata = Path(os.environ.get("APPDATA", Path.home()))
    programs = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    return (Path(startup_dir) if startup_dir else programs / "Startup",
            Path(start_menu_dir) if start_menu_dir else programs)


def install_shortcuts(startup_dir=None, start_menu_dir=None, repo=None, icon=None) -> list[Path]:
    from . import config
    from .icon import save_ico
    repo = Path(repo) if repo else Path(__file__).resolve().parent.parent
    if icon is None:
        icon = repo / "assets" / "murmur.ico"
        if not icon.exists():
            icon = save_ico(config.APP_DIR / "murmur.ico")
    startup_dir, start_menu_dir = _default_dirs(startup_dir, start_menu_dir)
    py = _pythonw()
    desc = "Murmur - hold-to-talk voice notes"
    a = create_shortcut(startup_dir / "Murmur.lnk", py, "-m murmur --background", repo, icon, desc,
                        AUMID, minimized=True)
    b = create_shortcut(start_menu_dir / "Murmur.lnk", py, "-m murmur", repo, icon, desc, AUMID)
    return [a, b]


def uninstall_shortcuts(startup_dir=None, start_menu_dir=None) -> list[Path]:
    startup_dir, start_menu_dir = _default_dirs(startup_dir, start_menu_dir)
    removed = []
    for p in (startup_dir / "Murmur.lnk", start_menu_dir / "Murmur.lnk"):
        if p.exists():
            p.unlink()
            removed.append(p)
    return removed
