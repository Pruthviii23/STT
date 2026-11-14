import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from murmur import notes, storage  # noqa: E402


def _seed(nd):
    storage.append_note("first", nd, datetime(2026, 11, 14, 14, 32))
    storage.append_note("second\nline two\n\nafter blank", nd, datetime(2026, 11, 14, 15, 1))
    storage.append_note("next day", nd, datetime(2026, 11, 15, 9, 0))
    storage.append_note("dec — café", nd, datetime(2026, 12, 1, 8, 0))


def test_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        nd = Path(d)
        _seed(nd)
        ns = notes.load_notes(nd)
        assert [n.text for n in ns] == ["dec — café", "next day", "second\nline two\n\nafter blank", "first"], ns
        assert ns[0].when == datetime(2026, 12, 1, 8, 0)
        assert ns[3].when == datetime(2026, 11, 14, 14, 32)
        assert notes.notes_mtime(nd) > 0
        assert notes.notes_mtime(nd / "nope") == 0


def test_legacy_and_tolerance():
    with tempfile.TemporaryDirectory() as d:
        nd = Path(d)
        (nd / "2026-10.md").write_text(
            "# Notes — October 2026\r\n\r\njunk line\r\n## Friday, 2 Oct 2026\r\n\r\n"
            "- **09:05** — old note <!-- from: PC-1 -->\r\n- **10:00** — two\r\n  more\r\n"
            "- **11:00** - hyphen\r\n", encoding="utf-8", newline="")
        (nd / "2026-09.md").write_text("- **08:00** — no header\n", encoding="utf-8")
        (nd / "readme.md").write_text("- **08:00** — ignored\n", encoding="utf-8")
        ns = notes.load_notes(nd)
        by = {n.text: n for n in ns}
        assert set(by) == {"old note", "two\nmore", "hyphen", "no header"}, set(by)
        assert by["old note"].when == datetime(2026, 10, 2, 9, 5)
        assert by["no header"].when == datetime(2026, 9, 1, 8, 0)


def test_delete():
    with tempfile.TemporaryDirectory() as d:
        nd = Path(d)
        _seed(nd)
        ns = {n.text: n for n in notes.load_notes(nd)}
        assert notes.delete_note(ns["second\nline two\n\nafter blank"])
        left = [n.text for n in notes.load_notes(nd)]
        assert left == ["dec — café", "next day", "first"], left
        ns = {n.text: n for n in notes.load_notes(nd)}
        assert notes.delete_note(ns["next day"])
        txt = (nd / "2026-11.md").read_text(encoding="utf-8")
        assert "15 Nov" not in txt and "14 Nov" in txt and txt.startswith("# Notes")
        ns = {n.text: n for n in notes.load_notes(nd)}
        assert notes.delete_note(ns["first"])
        txt = (nd / "2026-11.md").read_text(encoding="utf-8")
        assert "##" not in txt and "# Notes — November 2026" in txt
        storage.append_note("again", nd, datetime(2026, 11, 20, 1, 0))
        assert "again" in [n.text for n in notes.load_notes(nd)]
        assert not [p for p in nd.iterdir() if p.name.endswith(".tmp")]


def test_delete_stale_and_crlf():
    with tempfile.TemporaryDirectory() as d:
        nd = Path(d)
        _seed(nd)
        n = [x for x in notes.load_notes(nd) if x.text == "first"][0]
        p = n.path
        p.write_text(p.read_text(encoding="utf-8").replace("first", "edited"), encoding="utf-8")
        assert notes.delete_note(n) is False
        assert "edited" in p.read_text(encoding="utf-8")
        p.write_bytes(p.read_bytes().replace(b"\n", b"\r\n"))
        n = [x for x in notes.load_notes(nd) if x.text == "edited"][0]
        assert notes.delete_note(n)
        out = p.read_bytes()
        assert b"edited" not in out and b"\r\n" in out and b"\n" not in out.replace(b"\r\n", b"")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
