"""Per-series planning for batch mode: discovery, skip/incremental decisions, bookkeeping."""
from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path

from .ui import info, warn
from .textutil import compress, safe_filename
from .ranges import in_ranges, requested_but_absent
from .chapters import Chapter, chapters_from_index, chapters_from_names, is_our_output, load_index
from .writer import old_page_groups, plan_output_path, ReusePlan
from .state import job_fingerprint
from .prompts import show_available


@dataclass
class StagedSeries:
    selected: list[Chapter]
    all_count: int
    idx: dict | None
    series: str
    tmp: Path
    rdir: str = ""
    fp: str = ""  # job fingerprint, recorded once the merge succeeds
    plan: ReusePlan | None = None  # chapters to copy from the previous output
    cover_name: str | None = None
    sizes: dict | None = None  # remote file sizes / mtimes, for the layout record
    mtimes: dict | None = None
    wanted_all: list | None = None  # every file a full rebuild would need (incremental fallback)
    expected_all: dict | None = None


@dataclass
class SkippedSeries:
    """A series whose recorded output is still intact: nothing to transfer or merge."""
    out_path: Path
    pages: int
    chapters: int
    finished_at: str
    tmp: Path


def discover_series(adb, remote_root, name, entries, args, ranges, tmp_root):
    """Resolve metadata, chapter list, cover, and expected files for one remote series."""
    rdir = f"{remote_root.rstrip('/')}/{name}"
    files = {e.name: e.size for e in entries if not e.is_dir}
    if not files:
        raise RuntimeError("folder is empty")
    tmp = tmp_root / safe_filename(name)
    tmp.mkdir(parents=True, exist_ok=True)

    idx = None
    mode = args.mode
    if mode == "index" and "index.json" not in files:
        raise RuntimeError("--mode index but this series has no index.json")
    if mode != "filename" and "index.json" in files:
        (tmp / "index.json").unlink(missing_ok=True)  # tiny; always take a fresh copy
        adb.pull(f"{rdir}/index.json", tmp / "index.json", files["index.json"])
        idx = load_index(tmp / "index.json")
        chapters, absent = chapters_from_index(idx, tmp, available=set(files))
        info(f"[{name}] Using index metadata ({len(chapters)} chapters listed on the phone)")
        if absent:
            warn(f"[{name}] listed in index but file missing: {compress(absent)}")
        series = str(idx.get("title") or name.replace("_", " "))
    else:
        info(f"[{name}] No index.json on the phone; using filename detection."
             if mode == "auto" else f"[{name}] Using filename detection.")
        chapters = chapters_from_names(list(files), tmp)
        series = name.replace("_", " ")
    if not chapters:
        raise RuntimeError("no chapter files found")

    selected = [c for c in chapters if in_ranges(c.number, ranges)]
    gone = requested_but_absent(ranges, [c.number for c in chapters])
    if gone:
        warn(f"[{name}] requested but not available, skipping: {compress(gone)}")
    if not selected:
        show_available(chapters)
        raise RuntimeError("no available chapters fall in the requested range")

    if idx and idx.get("cover_entry") in files:
        cover_name = idx["cover_entry"]
    else:
        cover_name = next((f"cover{e}" for e in (".jpg", ".jpeg", ".png", ".webp")
                           if f"cover{e}" in files), None)

    wanted = ([cover_name] if cover_name else []) + [c.path.name for c in selected]
    expected_files = {w: files[w] for w in wanted}
    mtimes = {e.name: e.mtime for e in entries if not e.is_dir}
    fp = job_fingerprint(idx, series, selected, cover_name, files, mtimes)
    return (rdir, files, tmp, idx, series, len(chapters), selected, cover_name, wanted,
            expected_files, fp, mtimes)


def unchanged_job(args, rdir, fp, selected, all_count, series, tmp, name):
    """Return a SkippedSeries if this exact job already finished and its output is still intact."""
    st = args.state
    if st is None or args.force:
        return None
    row = st.job_hit(args.device, rdir, args.range_sig, args.mode, fp)
    if not row:
        return None
    out_str, out_size, pages, chapters, finished_at = row
    out = Path(out_str)
    try:
        intact = out.is_file() and out.stat().st_size == out_size
    except OSError:
        intact = False
    if not intact:
        info(f"[{name}] recorded output is missing or changed; rebuilding")
        return None
    planned = plan_output_path(selected, all_count, series, args.out_arg, args.default_dir, None, name)
    if args.out_arg and Path(args.out_arg).suffix.lower() == ".cbz":
        same_place = out.resolve() == planned
    else:
        same_place = out.resolve().parent == planned.parent
    if not same_place:
        return None  # a different output location was requested: not "done" there
    return SkippedSeries(out, pages, chapters, finished_at, tmp)


def layout_rows(selected, cover_name, sizes, mtimes, layout):
    """Layout rows for the database: which source file became which group of pages."""
    rows = []
    if cover_name and layout.get("cover"):
        rows.append((cover_name, "cover", sizes[cover_name], mtimes.get(cover_name, ""),
                     0, 1, layout["cover"]))
    groups = layout.get("groups", {})
    for ch in selected:
        g = groups.get(ch.path.name)
        if g:
            fname = ch.path.name
            rows.append((fname, "chapter", sizes[fname], mtimes.get(fname, ""), g[0], g[1], g[2]))
    return rows


def record_success(args, rdir, fp, res, files=()):
    """Remember a finished job, but only if every selected chapter made it into the output."""
    st = args.state
    out_path, pages, written, skipped = res
    if st is None or skipped:
        return
    try:
        size = out_path.stat().st_size
    except OSError:
        return
    prev = st.last_job(args.device, rdir, args.range_sig, args.mode)
    st.record_job(args.device, rdir, args.range_sig, args.mode, fp, str(out_path), size,
                  pages, written, files=files)
    if not args.keep_temp:
        st.forget_pulled(args.device, rdir)
    if prev:  # drop the previous output of this job if it was renamed (e.g. the range suffix grew)
        old = Path(prev[1])
        try:
            if (old != out_path and old.parent == out_path.parent and old.is_file()
                    and is_our_output(old)
                    and not st.output_in_use(str(old), args.device, rdir, args.range_sig, args.mode)):
                old.unlink()
                info(f"Removed superseded output {old.name}")
        except OSError:
            pass


def plan_incremental(args, rdir, selected, cover_name, sizes, mtimes, name):
    """Work out which chapters can be copied from this job's previous output instead of
    being pulled again. A chapter is reusable if its source file is unchanged (name, size and
    modified time) and the old CBZ really holds its pages. Returns a ReusePlan or None."""
    st = args.state
    if st is None or args.force or rdir in args.no_incr:
        return None
    job = st.last_job(args.device, rdir, args.range_sig, args.mode)
    if not job:
        return None
    old = Path(job[1])
    try:
        usable_old = old.is_file() and old.stat().st_size == job[2] and is_our_output(old)
    except OSError:
        usable_old = False
    if not usable_old:
        info(f"[{name}] previous output is missing or was modified; doing a full rebuild")
        return None

    wanted_now = {c.path.name for c in selected}
    reuse: dict[str, tuple[int, int, int]] = {}
    cover_entry, cover_bytes = None, 0
    for fname, kind, size, mtime, grp, pages, nbytes in st.job_files(
            args.device, rdir, args.range_sig, args.mode):
        if sizes.get(fname) != size or mtimes.get(fname, "") != mtime:
            continue  # changed on the phone, or gone: handled as new
        if kind == "chapter" and fname in wanted_now:
            reuse[fname] = (grp, pages, nbytes)
        elif kind == "cover" and fname == cover_name:
            cover_entry = "0000_00000" + (Path(fname).suffix.lower() or ".jpg")
            cover_bytes = nbytes
    if not reuse:
        return None

    try:  # cheap structural check of the old file before relying on it
        with zipfile.ZipFile(old) as z:
            groups = old_page_groups(z)
            names = set(z.namelist())
    except (zipfile.BadZipFile, OSError):
        return None
    for fname, (grp, pages, _b) in list(reuse.items()):
        if len(groups.get(grp, [])) != pages:
            del reuse[fname]  # not trustworthy: pull it again
    if cover_entry and cover_entry not in names:
        cover_entry = None
    if not reuse:
        return None
    new_n = len(wanted_now) - len(reuse)
    info(f"[{name}] Incremental: reusing {len(reuse)} chapter(s) from {old.name}; "
         f"pulling {new_n} new/changed chapter(s)")
    return ReusePlan(old, reuse, cover_entry, cover_bytes)
