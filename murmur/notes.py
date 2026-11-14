"""Parse / edit the monthly Markdown note files (the files stay the source of truth and human-editable).

Format written by storage.append_note:
    # Notes — November 2026
    ## Saturday, 14 Nov 2026
    - **14:32** — text            (continuation lines indented 2 spaces, blank lines inside a note are bare)
Parsing is tolerant: unknown lines are ignored, CRLF is fine, a missing day heading falls back to the 1st of
the filename's month. delete_note re-verifies the file before touching it and writes atomically.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

_FILE_RE = re.compile(r"^(\d{4})-(\d{2})\.md$")
_BULLET_RE = re.compile(r"^- \*\*(\d{1,2}):(\d{2})\*\*\s*(?:[—–-]\s*)?(.*)$")
_DAY_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})")
_LEGACY_RE = re.compile(r"\s*<!--\s*from:.*?-->\s*$")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


@dataclass(frozen=True)
class Note:
    when: datetime
    text: str
    path: Path
    line_start: int  # 0-based
    line_end: int    # exclusive
    raw: tuple = field(default=(), compare=False, repr=False)  # the file lines, for stale checks


def _read_lines(path: Path):
    try:
        data = Path(path).read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None
    if data.startswith("﻿"):
        data = data[1:]
    eol = "\r\n" if "\r\n" in data else "\n"
    return [ln.rstrip("\r") for ln in data.split("\n")], eol


def _parse_day(line: str):
    m = _DAY_RE.search(line)
    if not m:
        return None
    mon = _MONTHS.get(m.group(2)[:3].lower())
    if not mon:
        return None
    try:
        return datetime(int(m.group(3)), mon, int(m.group(1))).date()
    except ValueError:
        return None


def _parse_file(path: Path) -> list[Note]:
    fm = _FILE_RE.match(path.name)
    got = _read_lines(path)
    if not fm or got is None:
        return []
    lines = got[0]
    try:
        day = datetime(int(fm.group(1)), int(fm.group(2)), 1).date()
    except ValueError:
        return []
    out: list[Note] = []
    i, n = 0, len(lines)
    while i < n:
        ln = lines[i]
        if ln.startswith("## "):
            d = _parse_day(ln)
            if d:
                day = d
            i += 1
            continue
        m = _BULLET_RE.match(ln)
        if not m:
            i += 1
            continue
        hh, mm = int(m.group(1)), int(m.group(2))
        body = [m.group(3)]
        j = i + 1
        end = i + 1
        while j < n:
            c = lines[j]
            if c.startswith("  ") and c.strip():
                body.append(c[2:])
                j += 1
                end = j
            elif not c.strip():
                body.append("")
                j += 1
            else:
                break
        text = _LEGACY_RE.sub("", "\n".join(body[:end - i]).strip()).strip()
        try:
            when = datetime(day.year, day.month, day.day, hh, mm)
        except ValueError:
            i = end
            continue
        if text:
            out.append(Note(when, text, path, i, end, tuple(lines[i:end])))
        i = end
    return out


def _files(notes_dir: Path) -> list[Path]:
    try:
        return [p for p in Path(notes_dir).iterdir() if _FILE_RE.match(p.name) and p.is_file()]
    except OSError:
        return []


def load_notes(notes_dir: Path) -> list[Note]:
    notes: list[Note] = []
    for p in _files(notes_dir):
        try:
            notes.extend(_parse_file(p))
        except Exception:
            log.exception("failed to parse %s", p)
    notes.sort(key=lambda n: (n.when, n.path.name, n.line_start), reverse=True)
    return notes


def notes_mtime(notes_dir: Path) -> float:
    best = 0.0
    for p in _files(notes_dir):
        try:
            best = max(best, p.stat().st_mtime)
        except OSError:
            pass
    return best


def delete_note(note: Note) -> bool:
    got = _read_lines(note.path)
    if got is None:
        return False
    lines, eol = got
    a, b = note.line_start, note.line_end
    if a < 0 or b > len(lines) or tuple(lines[a:b]) != tuple(note.raw):
        return False
    del lines[a:b]
    # drop the day heading if it no longer holds any note
    h = a - 1
    while h >= 0 and not lines[h].startswith("#"):
        h -= 1
    if h >= 0 and lines[h].startswith("## "):
        e = h + 1
        while e < len(lines) and not lines[e].startswith("#"):
            e += 1
        if not any(_BULLET_RE.match(x) for x in lines[h + 1:e]):
            del lines[h:e]
    fd, tmp = tempfile.mkstemp(dir=str(note.path.parent), prefix=".notes-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(eol.join(lines))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, note.path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        log.exception("delete_note failed")
        return False
    return True
