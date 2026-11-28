#!/usr/bin/env python3
"""Launcher: keeps `python cbz_merger.py ...` working. The code lives in the cbzmerger package."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cbzmerger.cli import run  # noqa: E402

if __name__ == "__main__":
    run()
