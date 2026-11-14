"""Run the unit tests (hotkey logic, postprocess, storage). Exits non-zero on any failure.

Usage: .venv\Scripts\python tests\run_all.py
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SUITES = ["test_hotkey_logic.py", "test_postprocess.py", "test_storage.py"]


def main() -> int:
    failed = []
    for name in SUITES:
        print(f"=== {name}", flush=True)
        if subprocess.run([sys.executable, str(HERE / name)], cwd=ROOT).returncode != 0:
            failed.append(name)
    if failed:
        print("FAILED:", ", ".join(failed))
        return 1
    print("All unit tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
