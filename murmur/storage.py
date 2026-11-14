"""Append-only monthly Markdown notes: one file per month, one heading per day, one bullet per note."""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

_HEADER_RE = re.compile(r"^## (.+)$", re.M)


def _file_for(notes_dir: Path, when: datetime) -> Path:
    return Path(notes_dir) / f"{when:%Y-%m}.md"


def today_file(notes_dir: Path) -> Path:
    return _file_for(notes_dir, datetime.now())


def _day_header(when: datetime) -> str:
    return f"{when:%A}, {when.day} {when:%b %Y}"


def _last_header(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    found = _HEADER_RE.findall(data[-65536:].decode("utf-8", errors="ignore"))
    return found[-1].strip() if found else None


def append_note(text: str, notes_dir: Path, when: datetime | None = None) -> Path:
    when = when or datetime.now()
    notes_dir = Path(notes_dir)
    notes_dir.mkdir(parents=True, exist_ok=True)
    path = _file_for(notes_dir, when)
    out = ""
    exists = path.exists() and path.stat().st_size > 0
    if not exists:
        out += f"# Notes — {when:%B %Y}\n"
    header = _day_header(when)
    if not exists or _last_header(path) != header:
        out += f"\n## {header}\n\n"
    lines = text.strip().split("\n")
    body = lines[0] + "".join(("\n  " + ln) if ln.strip() else "\n" for ln in lines[1:])
    out += f"- **{when:%H:%M}** — {body}\n"
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(out)
        f.flush()
        os.fsync(f.fileno())
    return path
