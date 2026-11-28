"""Merging a local folder."""
from __future__ import annotations

from pathlib import Path

from .ui import err, info, ok, paint, warn
from .textutil import compress
from .ranges import in_ranges, parse_ranges, requested_but_absent
from .chapters import chapters_from_filenames, chapters_from_index, load_index
from .writer import finish
from .prompts import ask_range, clean_path, interactive_local, show_available


def run_local(args, interactive):
    if interactive:
        folder, out_arg, mode, idx_path = interactive_local()
        out_arg = str(out_arg)
        ranges = None  # asked once the chapters are known
    else:
        folder = clean_path(args.input_opt or args.input or ".").resolve()
        out_arg, mode = args.output, args.mode
        idx_path = Path(args.index).expanduser() if args.index else folder / "index.json"
        if args.index and mode == "filename":
            err("--index conflicts with --mode filename")
            return 2
        if args.index and mode == "auto":
            mode = "index"
        try:
            ranges = parse_ranges(args.range_spec)
        except ValueError as e:
            err(e)
            return 2

    if not folder.is_dir():
        err(f"input folder not found: {folder}")
        return 1

    explicit = mode != "auto"
    if mode == "auto":
        if idx_path.is_file():
            mode = "index"
        else:
            mode = "filename"
            info(f"No index.json in {folder}; using filename detection.")
    elif mode == "index" and not idx_path.is_file():
        err(f"index file not found: {idx_path}")
        return 1

    idx = None
    if mode == "index":
        try:
            idx = load_index(idx_path)
        except (OSError, ValueError) as e:
            err(f"could not read {idx_path}: {e}")
            return 1
        chapters, absent = chapters_from_index(idx, folder)
        info(f"Using index metadata: {idx_path}")
        if absent:
            warn(f"listed in index but file missing: {compress(absent)}")
        series = str(idx.get("title") or folder.name)
    else:
        planned = Path(out_arg).expanduser() if out_arg and Path(out_arg).suffix.lower() == ".cbz" else None
        chapters = chapters_from_filenames(folder, exclude=[planned] if planned else [])
        if explicit:
            info("Using filename detection.")
        series = folder.name.replace("_", " ")

    if not chapters:
        err("no chapter files found.")
        return 1

    if interactive:
        while True:
            ranges = ask_range(chapters)
            selected = [c for c in chapters if in_ranges(c.number, ranges)]
            if selected:
                break
            print(f"  {paint('None of the available chapters match that range.', 'red')}")
    else:
        selected = [c for c in chapters if in_ranges(c.number, ranges)]
        if not selected:
            show_available(chapters)
            err("no available chapters fall in the requested range.")
            return 1

    gone = requested_but_absent(ranges, [c.number for c in chapters])
    if gone:
        warn(f"requested but not available, skipping: {compress(gone)}")

    print()
    try:
        out_path, n_pages, n_ch, skipped = finish(
            selected, len(chapters), idx, series, folder, out_arg, folder.parent)
    except RuntimeError as e:
        err(e)
        return 1
    extra = f", {len(skipped)} skipped" if skipped else ""
    ok(f"Wrote {paint(out_path, 'bold')} ({n_pages} pages, {n_ch} chapters{extra})")
    return 0
