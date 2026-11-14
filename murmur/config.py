"""Configuration for Murmur. Stored as JSON at %APPDATA%\\Murmur\\config.json.

Unknown keys are ignored; missing keys take defaults, and the file is (re)written with
all keys so the user can discover every setting.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

log = logging.getLogger(__name__)

APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "Murmur"
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "murmur.log"


def _documents_dir() -> Path:
    """The user's real Documents folder (may be relocated); falls back to ~/Documents."""
    try:
        import uuid
        from ctypes import wintypes

        class _GUID(ctypes.Structure):
            _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                        ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

        g = uuid.UUID("FDD39AD0-238F-46AF-ADB4-6C85480369C7")   # FOLDERID_Documents
        guid = _GUID(g.time_low, g.time_mid, g.time_hi_version, (ctypes.c_ubyte * 8)(*g.bytes[8:]))
        out = ctypes.c_wchar_p()
        shell32 = ctypes.windll.shell32
        shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(_GUID), wintypes.DWORD,
                                                 wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
        hr = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out))
        try:
            if hr == 0 and out.value:
                return Path(out.value)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
    except Exception:
        log.debug("SHGetKnownFolderPath failed", exc_info=True)
    return Path.home() / "Documents"


def _default_notes_dir() -> Path:
    return _documents_dir() / "Murmur Notes"


@dataclass
class Config:
    hotkey: str = "/"
    hold_ms: int = 350
    notes_dir: Path = field(default_factory=_default_notes_dir)
    whisper_model: str = "small.en"
    whisper_device: str = "auto"
    whisper_compute: str = "auto"
    language: str = "en"
    llm_cleanup: bool = True
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "llama3.2"
    llm_timeout_s: float = 8.0
    popup_seconds: float = 4.0
    min_record_s: float = 0.4
    max_record_s: float = 300
    sound_cues: bool = True

    def save(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        data["notes_dir"] = str(self.notes_dir)
        CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_config() -> Config:
    cfg = Config()
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            valid = {f.name for f in fields(Config)}
            for k, v in data.items():
                if k in valid:
                    setattr(cfg, k, Path(os.path.expandvars(v)) if k == "notes_dir" else v)
        except Exception:
            log.exception("Bad config file %s; using defaults", CONFIG_PATH)
    try:
        cfg.save()
    except Exception:
        log.exception("Could not write config")
    return cfg
