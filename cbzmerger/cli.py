"""Merge chapter-wise CBZ files into a single CBZ with ComicInfo.xml
(chapter bookmarks included) so Koodo Reader shows a proper chapter list.

Local folder
  cbz_merger.py                         # no arguments -> interactive menu
  cbz_merger.py INPUT [-o OUT] [-m auto|index|filename] [-r RANGE] [--index FILE]

Batch (pull straight from your phone over adb, Kotatsu layout)
  cbz_merger.py --batch -o OUT [-r RANGE] [--series NAME ...] [--fast]

Examples
  cbz_merger.py ./Series -o ./out -r 7-20
  cbz_merger.py ./Series -m filename -r "1-5,8,10-"
  cbz_merger.py -b -o ./out --series "shadow slave" -r 1-10
  cbz_merger.py -b --fast -o ./out"""
from __future__ import annotations

import argparse
import sys

from .constants import DEFAULT_PULL_WORKERS, REMOTE_DEFAULT
from .ui import Bar, init_ui
from .prompts import ask_menu
from .local import run_local
from .batch import run_batch


def build_parser():
    p = argparse.ArgumentParser(
        description="Merge chapter-wise CBZ files into one CBZ (with chapter bookmarks).",
        epilog="With no arguments at all, the script shows an interactive menu.",
    )
    p.add_argument("input", nargs="?", help="input folder with chapter CBZs (+ index.json)")
    p.add_argument("-i", "--input", dest="input_opt", metavar="DIR", help="same as positional input")
    p.add_argument("-o", "--output", help="output folder (or a path ending in .cbz); default: parent "
                                         "of the input folder (batch: current folder)")
    p.add_argument("-m", "--mode", choices=("auto", "index", "filename"), default="auto",
                   help="chapter detection: 'index' = index.json metadata, 'filename' = parse "
                        "filenames, 'auto' (default) = index.json if present else filenames")
    p.add_argument("-r", "--range", dest="range_spec", metavar="RANGE",
                   help="chapters to include, e.g. 7-20, 7-, -20, 5, 1-5,8 (default: all)")
    p.add_argument("--index", metavar="FILE", help="use this index.json instead of INPUT/index.json")

    b = p.add_argument_group("batch mode (phone over adb)")
    b.add_argument("-b", "--batch", action="store_true",
                   help="scan the phone with adb and merge every series found")
    b.add_argument("-f", "--fast", action="store_true",
                   help="stream tar directly over adb exec-out with zero temp disk writes "
                        "(falls back to normal pull on failure)")
    b.add_argument("--remote-path", default=REMOTE_DEFAULT, metavar="PATH",
                   help="manga folder on the phone (default: Kotatsu nightly)")
    b.add_argument("--series", action="append", metavar="NAME",
                   help="only these series (case-insensitive substring; repeatable)")
    b.add_argument("--serial", metavar="SERIAL", help="adb device serial when several are connected")
    b.add_argument("--workers", type=int, default=DEFAULT_PULL_WORKERS, metavar="N",
                   help=f"parallel adb pull workers for ranged downloads (default: {DEFAULT_PULL_WORKERS})")
    b.add_argument("--temp", metavar="DIR", help="where to stage files pulled from the phone")
    b.add_argument("--db", metavar="PATH",
                   help="progress database (default: state.db next to the installed adb)")
    b.add_argument("--no-db", action="store_true",
                   help="don't record or use progress (always transfer everything)")
    b.add_argument("--force", action="store_true",
                   help="ignore recorded progress and redo everything")
    b.add_argument("--install-adb", action="store_true",
                   help="if adb is missing, download and install it without asking")
    b.add_argument("--keep-temp", action="store_true", help="keep pulled files after merging")

    p.add_argument("--no-color", action="store_true", help="disable colored output")
    return p


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)
    init_ui(args.no_color)
    interactive = len(argv) == 0
    batch = args.batch
    if interactive:
        batch = ask_menu() == "batch"
    return run_batch(args, interactive) if batch else run_local(args, interactive)


def run():
    """Entry point for `python -m cbzmerger` and the cbz_merger.py launcher."""
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        if Bar.active:
            Bar.active.abort()
        print("\nCancelled.")
        sys.exit(130)
