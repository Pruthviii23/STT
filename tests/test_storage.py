import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from murmur import storage  # noqa: E402


def test_append():
    with tempfile.TemporaryDirectory() as d:
        nd = Path(d) / "sub" / "Notes"
        t1 = datetime(2026, 11, 14, 14, 32)
        p = storage.append_note("first note", nd, t1)
        assert p.name == "2026-11.md"
        storage.append_note("second\nline two", nd, datetime(2026, 11, 14, 15, 1))
        storage.append_note("next day", nd, datetime(2026, 11, 15, 9, 0))
        storage.append_note("unicode — café", nd, datetime(2026, 11, 15, 9, 5))
        txt = p.read_text(encoding="utf-8")
        lines = txt.split("\n")
        assert lines[0] == "# Notes — November 2026", lines[0]
        assert txt.count("## Saturday, 14 Nov 2026") == 1
        assert txt.count("## Sunday, 15 Nov 2026") == 1
        assert "- **14:32** — first note" in txt
        assert "- **15:01** — second\n  line two" in txt
        assert "<!--" not in txt
        assert "café" in txt
        # new month -> new file
        p2 = storage.append_note("dec", nd, datetime(2026, 12, 1, 8, 0))
        assert p2.name == "2026-12.md"
        assert p2.read_text(encoding="utf-8").startswith("# Notes — December 2026")
        # append-only: earlier content is prefix-preserved
        assert p.read_text(encoding="utf-8").startswith(txt)


def test_today_file():
    assert storage.today_file(Path("x")).name == datetime.now().strftime("%Y-%m") + ".md"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
