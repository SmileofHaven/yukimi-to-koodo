"""Batch mode driver: scan the phone, then pull and merge every series (pipelined)."""
from __future__ import annotations

import queue
import re
import shutil
import tempfile
import threading
from pathlib import Path

from .ui import ask, err, info, ok, paint, warn
from .textutil import nat, safe_filename
from .ranges import parse_ranges
from .adb import AdbError
from .adb_setup import connect_adb, tools_dir
from .state import range_signature, State
from .prompts import ask_folder, ask_range
from .jobs import SkippedSeries
from .transfer import fast_stream_remote_series, finish_staged, stage_remote_series


_SENTINEL = object()


def _note_result(results, idx_num, name, res, taken):
    """Store and print one finished series (written, or skipped as unchanged)."""
    if isinstance(res, SkippedSeries):
        taken.add(res.out_path)  # its filename stays reserved for later series with the same title
        results[idx_num] = (name, (res.out_path, res.pages, res.chapters, []), "unchanged")
        ok(f"[{name}] Up to date (unchanged since {res.finished_at}); nothing to transfer "
           f"-> {paint(res.out_path.name, 'bold')}")
        return
    results[idx_num] = (name, res, None)
    out_path, pages, written, skipped = res
    extra = f", {len(skipped)} skipped" if skipped else ""
    ok(f"Wrote {paint(out_path, 'bold')} ({pages} pages, {written} chapters{extra})")


def _print_summary(results, total):
    print(f"\n{paint('Summary', 'bold')}")
    for entry in results:
        if entry is None:
            continue
        name, res, why = entry
        if res and why == "unchanged":
            print(f"  {paint('SKIP', 'cyan', 'bold')} {name} -> {res[0].name} (unchanged)")
        elif res:
            print(f"  {paint('OK  ', 'green', 'bold')} {name} -> {res[0].name}")
        else:
            print(f"  {paint('FAIL', 'red', 'bold')} {name}: {why}")
    good = sum(1 for entry in results if entry and entry[1])
    return 0 if good == total else 1


def _cleanup_root(tmp_root, persistent, keep_temp):
    if keep_temp:
        info(f"Temporary files kept in {tmp_root}")
    elif persistent:
        for d in (tmp_root, tmp_root.parent):  # persistent staging: remove only if empty
            try:
                d.rmdir()
            except OSError:
                pass
    else:
        shutil.rmtree(tmp_root, ignore_errors=True)


def run_batch(args, interactive):
    default_dir = Path.cwd()
    if interactive:
        out_arg = str(ask_folder("Output folder", default=default_dir, must_exist=False))
        ranges = None
    else:
        out_arg = args.output
        try:
            ranges = parse_ranges(args.range_spec)
        except ValueError as e:
            err(e)
            return 2

    adb = connect_adb(args.serial, interactive, args.install_adb)
    if not adb:
        return 1

    remote_root = args.remote_path
    info(f"Scanning {paint(remote_root, 'bold')}")
    try:
        series_tree = adb.ls_recursive(remote_root)
    except AdbError as e:
        err(f"could not list {remote_root}: {e}")
        warn("Is Kotatsu installed, and has it downloaded something? Use --remote-path for another location.")
        return 1
    series = sorted(series_tree.keys(), key=nat)
    if not series:
        err("no series folders found there.")
        return 1

    print(f"\n{paint('Found', 'bold')} {len(series)} series:")
    for i, n in enumerate(series, 1):
        print(f"  {paint(str(i).rjust(2), 'cyan')}. {n}")

    if args.series:
        chosen = [n for n in series if any(q.lower() in n.lower() for q in args.series)]
        if not chosen:
            err("none of the --series filters matched.")
            return 1
    elif interactive:
        while True:
            raw = ask("\nWhich series? [Enter = all, or numbers like 1,3]:").strip()
            if not raw:
                chosen = series
                break
            try:
                picks = sorted({int(t) for t in re.split(r"[,\s]+", raw) if t})
                if picks and all(1 <= p <= len(series) for p in picks):
                    chosen = [series[p - 1] for p in picks]
                    break
            except ValueError:
                pass
            print(f"  {paint('Enter numbers between 1 and ' + str(len(series)), 'red')}")
    else:
        chosen = series

    if interactive:
        ranges = ask_range()

    state = None if args.no_db else State.open(Path(args.db) if args.db else tools_dir() / "state.db")
    args.state = state
    args.device = adb.serial or "default"
    args.out_arg = out_arg
    args.default_dir = default_dir
    args.range_sig = range_signature(ranges)
    args.no_incr = set()  # series whose incremental rebuild failed this run
    persistent = state is not None
    if persistent:
        # Resume needs staging that survives between runs: one folder per device.
        base = Path(args.temp) if args.temp else tools_dir()
        tmp_root = base / "staging" / safe_filename(args.device)
        tmp_root.mkdir(parents=True, exist_ok=True)
    else:
        tmp_root = Path(tempfile.mkdtemp(prefix="cbzmerge_", dir=args.temp))
    results: list[tuple[str, tuple | None, str | None] | None] = [None] * len(chosen)
    taken: set[Path] = set()  # output paths already written in this run

    if args.fast:
        if args.keep_temp:
            warn("--keep-temp requested; falling back to standard staged pull instead of --fast streaming")
        else:
            info("Fast mode enabled: streaming tar directly over adb exec-out (zero temp disk writes)")
            try:
                for idx_num, name in enumerate(chosen):
                    print()
                    info(f"[{idx_num + 1}/{len(chosen)}] {paint(name, 'bold', 'magenta')}")
                    taken_snapshot = set(taken)
                    try:
                        res = fast_stream_remote_series(
                            adb, remote_root, name, series_tree[name], args, ranges,
                            tmp_root, out_arg, default_dir, taken
                        )
                        _note_result(results, idx_num, name, res, taken)
                    except (AdbError, RuntimeError) as e:
                        # Restore `taken` and fall back to normal staged pull for this series
                        taken.clear()
                        taken.update(taken_snapshot)
                        warn(f"[{name}] fast tar stream failed ({e}); falling back to standard pull")
                        try:
                            staged = stage_remote_series(
                                adb, remote_root, name, series_tree[name], args, ranges, tmp_root
                            )
                            if isinstance(staged, SkippedSeries):
                                _note_result(results, idx_num, name, staged, taken)
                                shutil.rmtree(staged.tmp, ignore_errors=True)
                            else:
                                cleanup = True
                                try:
                                    res = finish_staged(adb, name, args, staged, out_arg,
                                                         default_dir, taken)
                                    _note_result(results, idx_num, name, res, taken)
                                except BaseException:
                                    cleanup = False  # interrupted: keep pulled files for resume
                                    raise
                                finally:
                                    if cleanup and not args.keep_temp:
                                        shutil.rmtree(staged.tmp, ignore_errors=True)
                        except (AdbError, RuntimeError, OSError, ValueError) as fb_err:
                            err(f"{name}: {fb_err}")
                            results[idx_num] = (name, None, str(fb_err))
                    except (OSError, ValueError) as e:
                        err(f"{name}: {e}")
                        results[idx_num] = (name, None, str(e))
            except BaseException:
                adb.abort()
                raise
            finally:
                _cleanup_root(tmp_root, persistent, False)

            return _print_summary(results, len(chosen))

    ready_q: queue.Queue = queue.Queue(maxsize=2)  # backpressure: stage at most 2 series ahead
    stop_event = threading.Event()

    def _enqueue(payload):
        while not stop_event.is_set():
            try:
                ready_q.put(payload, timeout=0.2)
                return True
            except queue.Full:
                continue
        return False

    def _pull_worker():
        try:
            for idx_num, name in enumerate(chosen):
                if stop_event.is_set():
                    break
                print()
                info(f"[{idx_num + 1}/{len(chosen)}] {paint(name, 'bold', 'magenta')}")
                try:
                    staged = stage_remote_series(
                        adb, remote_root, name, series_tree[name], args, ranges, tmp_root
                    )
                    if not _enqueue((idx_num, name, staged, None)):
                        break
                except (AdbError, RuntimeError, OSError, ValueError) as e:
                    if not persistent and not args.keep_temp:  # persistent staging is kept for resume
                        shutil.rmtree(tmp_root / safe_filename(name), ignore_errors=True)
                    if stop_event.is_set():
                        break
                    if not _enqueue((idx_num, name, None, str(e))):
                        break
        finally:
            while True:
                try:
                    ready_q.put(_SENTINEL, timeout=0.2)
                    break
                except queue.Full:
                    if stop_event.is_set():
                        try:
                            ready_q.get_nowait()
                        except queue.Empty:
                            pass

    puller = threading.Thread(target=_pull_worker, name="adb-puller", daemon=True)
    puller.start()

    try:
        while True:
            try:
                item = ready_q.get(timeout=0.5)  # timeout keeps Ctrl+C responsive on Windows
            except queue.Empty:
                continue
            if item is _SENTINEL:
                break
            idx_num, name, staged, pull_err = item
            if pull_err is not None or staged is None:
                err(f"{name}: {pull_err}")
                results[idx_num] = (name, None, pull_err)
                continue
            if isinstance(staged, SkippedSeries):
                _note_result(results, idx_num, name, staged, taken)
                shutil.rmtree(staged.tmp, ignore_errors=True)
                continue
            cleanup = True
            try:
                res = finish_staged(adb, name, args, staged, out_arg, default_dir, taken)
                _note_result(results, idx_num, name, res, taken)
            except (RuntimeError, OSError, ValueError) as e:
                err(f"{name}: {e}")
                results[idx_num] = (name, None, str(e))
            except BaseException:
                cleanup = False  # interrupted: keep the pulled files so the next run resumes
                raise
            finally:
                if cleanup and not args.keep_temp:
                    shutil.rmtree(staged.tmp, ignore_errors=True)
    except BaseException:
        stop_event.set()
        adb.abort()
        puller.join(timeout=3)
        raise
    finally:
        stop_event.set()
        puller.join(timeout=3)
        _cleanup_root(tmp_root, persistent, args.keep_temp)

    return _print_summary(results, len(chosen))
