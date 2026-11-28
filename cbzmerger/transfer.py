"""Getting one series off the phone: staged pull, tar streaming and the merge hand-off."""
from __future__ import annotations

import shutil

from .ui import Bar, info, paint, warn
from .textutil import compress
from .writer import (
    finish,
    IncrementalError,
    merge_from_stream,
    plan_output_path,
    rename_for_skipped,
)
from .adb import audit_remote_dir
from .jobs import (
    discover_series,
    layout_rows,
    plan_incremental,
    record_success,
    StagedSeries,
    unchanged_job,
)


def stage_remote_series(adb, remote_root, name, entries, args, ranges, tmp_root):
    """Discover and pull one series from the phone into `tmp_root` using pre-scanned `entries`.
    With a usable previous output, only new or changed chapters are pulled."""
    (rdir, files, tmp, idx, series, all_count, selected, cover_name, wanted,
     expected_files, fp, mtimes) = discover_series(
        adb, remote_root, name, entries, args, ranges, tmp_root
    )

    hit = unchanged_job(args, rdir, fp, selected, all_count, series, tmp, name)
    if hit:
        return hit

    plan = plan_incremental(args, rdir, selected, cover_name, files, mtimes, name)
    reused = set()
    if plan:
        reused = set(plan.chapters) | ({cover_name} if plan.cover_entry and cover_name else set())
    to_pull = [w for w in wanted if w not in reused]
    pull_files = {w: expected_files[w] for w in to_pull}

    # Resume: keep only cached files the database vouches for (same size AND remote mtime).
    st = args.state
    cached = 0
    for w in to_pull:
        local = tmp / w
        if not local.is_file():
            continue
        try:
            good = (st is not None and not args.force
                    and local.stat().st_size == expected_files[w]
                    and st.pull_valid(args.device, f"{rdir}/{w}", expected_files[w], mtimes.get(w, "")))
        except OSError:
            good = False
        if good:
            cached += 1
        else:
            local.unlink(missing_ok=True)
    if cached:
        info(f"[{name}] Resuming: {cached}/{len(to_pull)} file(s) already on disk")

    def on_done(fname):
        if st is not None:
            st.mark_pulled(args.device, f"{rdir}/{fname}", expected_files[fname],
                           mtimes.get(fname, ""), str(tmp / fname))

    total = sum(pull_files.values())
    pull_all_chapters = plan is None and len(selected) == all_count and len(wanted) > 1 and cached == 0
    safe_bulk, audit_reason = (
        audit_remote_dir(entries, expected_files) if pull_all_chapters else (False, "")
    )
    if pull_all_chapters and not safe_bulk:
        warn(f"[{name}] skipping direct folder pull ({audit_reason}); pulling expected files individually")

    pulled_ok: set[str] = set()
    if to_pull:
        bar = Bar(total, f"Pulling ({name})")
        finished = False
        try:
            if safe_bulk:
                pulled_ok = adb.pull_dir(
                    rdir, tmp, expected_files, bar=bar, fallback_workers=args.workers, on_done=on_done
                )
            else:
                items = [(f"{rdir}/{w}", tmp / w, expected_files[w]) for w in to_pull]
                pulled_ok = adb.pull_many(items, bar=bar, workers=args.workers, on_done=on_done)
            finished = True
        finally:
            bar.close() if finished else bar.abort()

    # Never merge (or record as done) a series that is missing files: fail it instead.
    # With the database on, the files that did arrive stay in staging, so the next run resumes.
    failed = [w for w in to_pull if w not in pulled_ok]
    if failed:
        nums = [c.number for c in selected if c.path.name in failed]
        what = f"chapter(s) {compress(nums)}" if nums else "the cover"
        raise RuntimeError(f"{len(failed)} file(s) could not be pulled ({what})"
                           + ("; run again to resume" if st is not None else ""))

    return StagedSeries(selected, all_count, idx, series, tmp, rdir, fp, plan=plan,
                         cover_name=cover_name, sizes=files, mtimes=mtimes,
                         wanted_all=wanted, expected_all=expected_files)


def complete_stage(adb, name, args, staged):
    """The incremental rebuild failed: pull whatever a full rebuild still needs."""
    st = args.state
    missing = {}
    for w in staged.wanted_all:
        local = staged.tmp / w
        try:
            have = local.is_file() and local.stat().st_size == staged.expected_all[w]
        except OSError:
            have = False
        if not have:
            missing[w] = staged.expected_all[w]
    if not missing:
        return
    info(f"[{name}] pulling {len(missing)} remaining file(s) for the full rebuild")

    def on_done(fname):
        if st is not None:
            st.mark_pulled(args.device, f"{staged.rdir}/{fname}", staged.expected_all[fname],
                           (staged.mtimes or {}).get(fname, ""), str(staged.tmp / fname))

    bar = Bar(sum(missing.values()), f"Pulling ({name})")
    finished = False
    try:
        pulled = adb.pull_many(
            [(f"{staged.rdir}/{w}", staged.tmp / w, sz) for w, sz in missing.items()],
            bar=bar, workers=args.workers, on_done=on_done)
        finished = True
    finally:
        bar.close() if finished else bar.abort()
    failed = [w for w in missing if w not in pulled]
    if failed:
        raise RuntimeError(f"{len(failed)} file(s) could not be pulled for the full rebuild; "
                           "run again to resume")


def finish_staged(adb, name, args, staged, out_arg, default_dir, taken):
    """Merge a staged series (incrementally when planned), falling back to a full rebuild if
    the previous output turns out to be unusable, then record the job."""
    layout: dict = {}
    snapshot = set(taken)
    try:
        res = finish(staged.selected, staged.all_count, staged.idx, staged.series, staged.tmp,
                     out_arg, default_dir, taken, name, show_progress=True,
                     plan=staged.plan, layout_out=layout)
    except IncrementalError as e:
        warn(f"[{name}] incremental rebuild failed ({e}); doing a full rebuild")
        args.no_incr.add(staged.rdir)
        taken.clear()
        taken.update(snapshot)
        complete_stage(adb, name, args, staged)
        layout = {}
        res = finish(staged.selected, staged.all_count, staged.idx, staged.series, staged.tmp,
                     out_arg, default_dir, taken, name, show_progress=True,
                     plan=None, layout_out=layout)
    record_success(args, staged.rdir, staged.fp, res,
                   layout_rows(staged.selected, staged.cover_name, staged.sizes or {},
                              staged.mtimes or {}, layout))
    return res


def fast_stream_remote_series(adb, remote_root, name, entries, args, ranges, tmp_root,
                              out_arg, default_dir, taken):
    """Stream `tar -cf -` over `adb exec-out` straight into the output `.cbz`
    without writing chapter `.cbz` files to temp disk. With a usable previous output, only
    new or changed chapters are streamed; the rest are copied from the old CBZ."""
    (rdir, files, tmp, idx, series, all_count, selected, cover_name, wanted,
     expected_files, fp, mtimes) = discover_series(
        adb, remote_root, name, entries, args, ranges, tmp_root
    )
    try:
        hit = unchanged_job(args, rdir, fp, selected, all_count, series, tmp, name)
        if hit:
            return hit
        plan = plan_incremental(args, rdir, selected, cover_name, files, mtimes, name)
        reused = set()
        if plan:
            reused = set(plan.chapters) | ({cover_name} if plan.cover_entry and cover_name else set())
        stream_names = [w for w in wanted if w not in reused]
        total = sum(expected_files[w] for w in stream_names)

        out_path = plan_output_path(selected, all_count, series, out_arg, default_dir, taken, name)
        info(f"Streaming {len(stream_names)} file(s) -> {paint(out_path, 'bold')}")
        layout: dict = {}
        stream = adb.stream_tar(rdir, stream_names) if stream_names else iter(())
        try:
            pages, written, skipped = merge_from_stream(
                stream, selected, idx, series, cover_name, out_path, total,
                label=f"Streaming ({name})", reuse=plan, layout=layout
            )
        except IncrementalError:
            args.no_incr.add(rdir)  # the caller falls back to a staged full rebuild
            raise
        out_path = rename_for_skipped(out_path, selected, skipped, all_count, series, out_arg,
                                      default_dir, taken, name)
        res = (out_path, pages, written, skipped)
        record_success(args, rdir, fp, res, layout_rows(selected, cover_name, files, mtimes, layout))
        return res
    finally:
        if not args.keep_temp:
            shutil.rmtree(tmp, ignore_errors=True)
