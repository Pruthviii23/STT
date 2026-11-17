"""Put text where the user's cursor is.

- ``focused_text_input()`` decides whether keyboard focus is in an editable text field:
  1. no foreground window, or the desktop / taskbar -> no;
  2. the focused thread owns a system caret (Win32 edits, Office, and Chromium/Electron apps,
     which create an accessibility caret for editable fields) -> yes;
  3. UI Automation (raw COM via ctypes, no extra dependency): the focused element is an Edit, or
     exposes a writable ValuePattern -> yes.
  Anything else (including errors) -> no, so text is never typed into nowhere.
- ``type_text()`` types with SendInput KEYEVENTF_UNICODE (no clipboard clobbering). Newlines are
  sent as Shift+Enter so chat apps (Teams, Slack) insert a line break instead of sending.
- ``copy_to_clipboard()`` is the fallback when no field is focused.
- Limitation: Windows blocks input into elevated (administrator) windows from a normal process.
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
import uuid
from ctypes import wintypes

log = logging.getLogger(__name__)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
ole32 = ctypes.windll.ole32
oleaut32 = ctypes.windll.oleaut32

_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}

# --------------------------------------------------------------------------- caret check


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hwndActive", wintypes.HWND),
                ("hwndFocus", wintypes.HWND), ("hwndCapture", wintypes.HWND),
                ("hwndMenuOwner", wintypes.HWND), ("hwndMoveSize", wintypes.HWND),
                ("hwndCaret", wintypes.HWND), ("rcCaret", wintypes.RECT)]


user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(_GUITHREADINFO)]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]


def _class_name(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _foreground():
    """(hwnd, gui_thread_info) of the foreground window, or (None, None)."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None, None
    tid = user32.GetWindowThreadProcessId(hwnd, None)
    gti = _GUITHREADINFO(cbSize=ctypes.sizeof(_GUITHREADINFO))
    if not user32.GetGUIThreadInfo(tid, ctypes.byref(gti)):
        return hwnd, None
    return hwnd, gti


# --------------------------------------------------------------------------- UI Automation (raw COM)


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, s: str) -> "_GUID":
        u = uuid.UUID(s)
        return cls(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))


class _VARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                ("r3", ctypes.c_ushort), ("val", ctypes.c_longlong), ("pad", ctypes.c_longlong)]


VT_I4, VT_BOOL = 3, 11
CLSID_CUIAutomation = _GUID.of("ff48dba4-60ef-4201-aa87-54103eef594e")
IID_IUIAutomation = _GUID.of("30cbe57d-d9d0-452a-ab13-7ac5ac4825ee")

# IUIAutomation vtable index / IUIAutomationElement vtable index
_IDX_RELEASE = 2
_IDX_GET_FOCUSED = 8
_IDX_GET_PROPERTY = 10

UIA_ControlTypePropertyId = 30003
UIA_IsKeyboardFocusablePropertyId = 30009
UIA_IsValuePatternAvailablePropertyId = 30043
UIA_ValueIsReadOnlyPropertyId = 30046
UIA_EditControlTypeId = 50004
UIA_DocumentControlTypeId = 50030
UIA_ComboBoxControlTypeId = 50003

_tls = threading.local()


def _vcall(obj, idx: int, restype, *argtypes):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[idx])


def _release(obj) -> None:
    if obj:
        _vcall(obj, _IDX_RELEASE, ctypes.c_ulong)(obj)


def _uia():
    a = getattr(_tls, "uia", None)
    if a:
        return a
    ole32.CoInitializeEx(None, 0)       # MTA; S_FALSE / RPC_E_CHANGED_MODE are fine
    p = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(ctypes.byref(CLSID_CUIAutomation), None, 1 | 4,  # INPROC | LOCAL
                                ctypes.byref(IID_IUIAutomation), ctypes.byref(p))
    if hr != 0 or not p.value:
        raise OSError(f"CoCreateInstance(CUIAutomation) failed: 0x{hr & 0xFFFFFFFF:08x}")
    _tls.uia = p.value
    return p.value


def _prop(el, pid: int):
    v = _VARIANT()
    hr = _vcall(el, _IDX_GET_PROPERTY, ctypes.c_long, ctypes.c_int, ctypes.POINTER(_VARIANT))(
        el, pid, ctypes.byref(v))
    try:
        if hr != 0:
            return None
        if v.vt == VT_I4:
            return ctypes.c_int32(v.val & 0xFFFFFFFF).value
        if v.vt == VT_BOOL:
            return (v.val & 0xFFFF) != 0
        return None
    finally:
        oleaut32.VariantClear(ctypes.byref(v))


def _uia_focus_is_editable() -> bool | None:
    """True/False from UI Automation, None if it could not tell."""
    uia = _uia()
    el = ctypes.c_void_p()
    hr = _vcall(uia, _IDX_GET_FOCUSED, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(uia, ctypes.byref(el))
    if hr != 0 or not el.value:
        return None
    try:
        ctype = _prop(el.value, UIA_ControlTypePropertyId)
        has_value = _prop(el.value, UIA_IsValuePatternAvailablePropertyId)
        read_only = _prop(el.value, UIA_ValueIsReadOnlyPropertyId) if has_value else None
        log.debug("UIA focus: control=%s value=%s readonly=%s", ctype, has_value, read_only)
        if has_value and read_only is False and ctype in (UIA_EditControlTypeId, UIA_DocumentControlTypeId,
                                                          UIA_ComboBoxControlTypeId, None) :
            return True
        if ctype == UIA_EditControlTypeId and read_only is not True:
            return True
        return False
    finally:
        _release(el.value)


def focused_text_input() -> bool:
    try:
        hwnd, gti = _foreground()
        if not hwnd:
            return False
        if _class_name(hwnd) in _SHELL_CLASSES:
            return False
        if gti is not None and gti.hwndCaret:
            return True
        try:
            res = _uia_focus_is_editable()
        except Exception:
            log.debug("UIA focus check failed", exc_info=True)
            res = None
        return bool(res)
    except Exception:
        log.exception("focus check failed")
        return False


# --------------------------------------------------------------------------- typing

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


KEYEVENTF_KEYUP = 0x2
KEYEVENTF_UNICODE = 0x4
VK_SHIFT, VK_RETURN = 0x10, 0x0D


def _vk(vk: int, up: bool) -> _INPUT:
    return _INPUT(type=1, ki=_KBI(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, 0))


def _uni(code: int, up: bool) -> _INPUT:
    return _INPUT(type=1, ki=_KBI(0, code, KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0), 0, 0))


def build_inputs(text: str) -> list[_INPUT]:
    out: list[_INPUT] = []
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for ch in text:
        if ch == "\n":
            out += [_vk(VK_SHIFT, False), _vk(VK_RETURN, False), _vk(VK_RETURN, True), _vk(VK_SHIFT, True)]
            continue
        b = ch.encode("utf-16-le")
        for i in range(0, len(b), 2):            # surrogate pairs go as two code units
            cu = int.from_bytes(b[i:i + 2], "little")
            out += [_uni(cu, False), _uni(cu, True)]
    return out


def type_text(text: str, chunk: int = 64) -> None:
    inputs = build_inputs(text)
    for i in range(0, len(inputs), chunk):
        part = inputs[i:i + chunk]
        arr = (_INPUT * len(part))(*part)
        sent = user32.SendInput(len(part), arr, ctypes.sizeof(_INPUT))
        if sent != len(part):
            raise OSError("SendInput was blocked (is the target window running as administrator?)")
        time.sleep(0.005)


# --------------------------------------------------------------------------- clipboard

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x2
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.OpenClipboard.argtypes = [wintypes.HWND]


def copy_to_clipboard(text: str) -> bool:
    data = (text.replace("\r\n", "\n").replace("\n", "\r\n") + "\0").encode("utf-16-le")
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.03)
    else:
        log.warning("Could not open clipboard")
        return False
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return False
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            kernel32.GlobalFree(h)
            return False
        return True
    finally:
        user32.CloseClipboard()
