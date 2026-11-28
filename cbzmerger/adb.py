"""adb wrapper: list, pull and stream files from the phone."""
from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .constants import DEFAULT_PULL_WORKERS
from .ui import fmt_size, warn


class AdbError(Exception):
    pass


@dataclass
class RemoteEntry:
    name: str
    is_dir: bool
    size: int
    mtime: str = ""  # 'YYYY-MM-DD HH:MM' straight from ls (minute precision)


_LS_RE = re.compile(
    r"^([\-dlcbps])\S*\s+\d+\s+\S+\s+\S+\s+(\d+)\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})\s+(.+?)(?:\s+->\s+.*)?$"
)


def audit_remote_dir(entries, expected_files):
    """Verify a remote series directory contains only expected files and no
    subdirectories before running a direct recursive `adb pull`."""
    allowed_names = set(expected_files) | {"index.json"}

    dirs = [e.name for e in entries if e.is_dir]
    if dirs:
        sample = ", ".join(dirs[:3]) + ("..." if len(dirs) > 3 else "")
        return False, f"contains {len(dirs)} subdirectory(ies): {sample}"

    extra = [e for e in entries if not e.is_dir and e.name not in allowed_names]
    if extra:
        sample = ", ".join(f"{e.name} ({fmt_size(e.size)})" for e in extra[:3])
        if len(extra) > 3:
            sample += "..."
        return False, f"contains {len(extra)} unexpected file(s): {sample}"

    return True, ""


class Adb:
    def __init__(self, exe, serial=None):
        self.exe = exe
        self.serial = serial
        self._procs: set[subprocess.Popen] = set()
        self._proc_lock = threading.Lock()
        self._aborted = threading.Event()

    def abort(self):
        """Terminate any in-flight adb pull processes immediately (frees Windows file locks)."""
        self._aborted.set()
        with self._proc_lock:
            procs = list(self._procs)
        for p in procs:
            try:
                p.terminate()
            except OSError:
                pass
        for p in procs:
            try:
                p.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    p.kill()
                except OSError:
                    pass

    def _cmd(self, *args):
        return [self.exe] + (["-s", self.serial] if self.serial else []) + list(args)

    def _spawn(self, *args):
        if self._aborted.is_set():
            raise AdbError("cancelled")
        proc = subprocess.Popen(
            self._cmd(*args), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
        )
        with self._proc_lock:
            if self._aborted.is_set():
                proc.terminate()
                raise AdbError("cancelled")
            self._procs.add(proc)
        return proc

    def _reap(self, proc):
        try:
            _, stderr = proc.communicate()
            return proc.returncode, stderr
        finally:
            with self._proc_lock:
                self._procs.discard(proc)

    def run(self, *args, timeout=60):
        if self._aborted.is_set():
            raise AdbError("cancelled")
        try:
            return subprocess.run(self._cmd(*args), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            raise AdbError(f"adb {args[0]} timed out (is the phone unlocked and still connected?)")

    def ls(self, remote_dir):
        r = self.run("shell", f"ls -la {shlex.quote(remote_dir)}")
        out = r.stdout.replace("\r", "")
        if r.returncode != 0 or "No such file" in out or "Permission denied" in out:
            raise AdbError((out + r.stderr).strip() or f"adb exited with {r.returncode}")
        entries = []
        for line in out.splitlines():
            m = _LS_RE.match(line.strip())
            if not m or m.group(4) in (".", ".."):
                continue
            kind, size, mtime, name = m.group(1), int(m.group(2)), m.group(3), m.group(4)
            if kind in ("-", "d"):
                entries.append(RemoteEntry(name, kind == "d", size, mtime))
        return entries

    def ls_recursive(self, remote_root, timeout=120):
        """Scan `remote_root` and all series subdirectories in a single `ls -laR`
        round-trip. Returns `{series_name: [RemoteEntry, ...]}`."""
        root = remote_root.rstrip("/")
        r = self.run("shell", f"ls -laR {shlex.quote(root)}", timeout=timeout)
        out = r.stdout.replace("\r", "")
        if r.returncode != 0 or "No such file" in out or "Permission denied" in out:
            raise AdbError((out + r.stderr).strip() or f"adb exited with {r.returncode}")

        tree: dict[str, list[RemoteEntry]] = {}
        current_rel: str | None = ""  # "" = root directory, "<series>" = top-level series folder
        saw_root_header = False

        for raw_line in out.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("total "):
                continue
            if line.endswith(":") and (line.startswith("/") or line.startswith(".")):
                header = line[:-1].rstrip("/")
                if header in (root, "."):
                    current_rel = ""
                    saw_root_header = True
                elif header.startswith(root + "/"):
                    current_rel = header[len(root) + 1:]
                elif header.startswith("./"):
                    current_rel = header[2:]
                else:
                    current_rel = None
                if current_rel and "/" not in current_rel:
                    tree.setdefault(current_rel, [])
                continue

            m = _LS_RE.match(line)
            if not m or m.group(4) in (".", "..") or current_rel is None:
                continue
            kind, size, mtime, name = m.group(1), int(m.group(2)), m.group(3), m.group(4)
            if kind not in ("-", "d"):
                continue
            if current_rel == "":
                if kind == "d":
                    tree.setdefault(name, [])
            elif "/" not in current_rel:
                tree.setdefault(current_rel, []).append(RemoteEntry(name, kind == "d", size, mtime))

        # Sanity check: if `ls -laR` output had no directory headers (e.g. a device
        # where `-R` is ignored), fall back to individual `ls` calls.
        if not saw_root_header and not any(tree.values()):
            top = self.ls(root)
            tree = {}
            for e in top:
                if e.is_dir:
                    tree[e.name] = self.ls(f"{root}/{e.name}")
        return tree

    def stream_tar(self, remote_dir, filenames):
        """Stream `filenames` from `remote_dir` via `adb exec-out tar -cf -`,
        yielding `(basename, bytes)` in order without touching local disk."""
        if not filenames:
            return
        if self._aborted.is_set():
            raise AdbError("cancelled")
        quoted_files = " ".join(shlex.quote(f) for f in filenames)
        remote_cmd = f"cd {shlex.quote(remote_dir)} && tar -cf - {quoted_files} 2>/dev/null"
        proc = subprocess.Popen(
            self._cmd("exec-out", remote_cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        with self._proc_lock:
            if self._aborted.is_set():
                proc.terminate()
                raise AdbError("cancelled")
            self._procs.add(proc)

        try:
            assert proc.stdout is not None
            with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
                for member in tf:
                    if self._aborted.is_set():
                        raise AdbError("cancelled")
                    if not member.isfile():
                        continue
                    extracted = tf.extractfile(member)
                    if extracted is None:
                        continue
                    yield Path(member.name).name, extracted.read()
        except (tarfile.TarError, OSError) as e:
            proc.terminate()
            raise AdbError(f"tar stream error: {e}") from e
        finally:
            try:
                if proc.stdout:
                    proc.stdout.close()
            except OSError:
                pass
            rc, stderr_bytes = self._reap(proc)
            if not self._aborted.is_set() and rc != 0:
                err_txt = (stderr_bytes or b"").decode("utf-8", errors="replace").strip()
                raise AdbError(err_txt or f"adb exec-out tar exited with {rc}")

    def pull(self, remote, local, size, bar=None):
        """Pull one file, optionally advancing `bar` smoothly by watching the growing local file."""
        local = Path(local)
        if local.is_file() and local.stat().st_size == size:  # left over from an earlier run
            if bar:
                bar.advance(size, local.name)
            return
        base = bar.value if bar else 0
        proc = self._spawn("pull", remote, str(local))
        while proc.poll() is None:
            if bar:
                try:
                    cur = local.stat().st_size
                except OSError:
                    cur = 0
                bar.set(base + min(cur, size), local.name)
            time.sleep(0.1)
        rc, stderr = self._reap(proc)
        if self._aborted.is_set():
            raise AdbError("cancelled")
        if rc != 0:
            raise AdbError(stderr.strip() or f"adb pull failed ({rc})")
        got = local.stat().st_size if local.is_file() else -1
        if got != size:
            raise AdbError(f"{local.name}: pulled {got} bytes, expected {size}")
        if bar:
            bar.set(base + size, local.name)

    def pull_many(self, items, bar=None, workers=DEFAULT_PULL_WORKERS, on_done=None):
        """Pull multiple (remote, local_path, size) tuples concurrently across `workers`
        ADB streams while updating `bar` smoothly. Returns set of successfully pulled names."""
        pulled_ok: set[str] = set()
        pending: list[tuple[str, Path, int]] = []
        done_bytes = 0

        for remote, local, size in items:
            local = Path(local)
            if local.is_file() and local.stat().st_size == size:
                pulled_ok.add(local.name)
                done_bytes += size
            else:
                pending.append((remote, local, size))

        if bar:
            bar.set(done_bytes)
        if not pending:
            return pulled_ok

        max_workers = max(1, min(workers, len(pending)))

        def _worker(item):
            remote, local, size = item
            self.pull(remote, local, size, bar=None)
            if on_done:
                on_done(local.name)  # recorded the moment the file is complete
            return local.name

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_worker, item): item for item in pending}
            while not self._aborted.is_set():
                if all(f.done() for f in futures):
                    break
                if bar:
                    in_flight = 0
                    active_name = ""
                    for f, (_, local, size) in futures.items():
                        if f.done():
                            if f.exception() is None:
                                in_flight += size
                        else:
                            try:
                                cur = min(local.stat().st_size, size)
                            except OSError:
                                cur = 0
                            if cur > 0 and not active_name:
                                active_name = local.name
                            in_flight += cur
                    bar.set(done_bytes + in_flight, active_name or None)
                time.sleep(0.1)

            for f, (_, local, size) in futures.items():
                if self._aborted.is_set():
                    f.cancel()
                    continue
                try:
                    name = f.result()
                    pulled_ok.add(name)
                    done_bytes += size
                except AdbError as e:
                    warn(f"could not pull {local.name}: {e}")
                    done_bytes += size  # advance past failed file so bar reaches 100%

        if bar and not self._aborted.is_set():
            bar.set(done_bytes)
        return pulled_ok

    def pull_dir(self, remote_dir, dest_dir, expected_files, bar=None,
                 fallback_workers=DEFAULT_PULL_WORKERS, on_done=None):
        """Pull an audited remote series directory in one continuous `adb pull` stream,
        move ONLY `expected_files` out of staging, verify byte sizes, and fall back to
        `pull_many` for any misses."""
        dest_dir = Path(dest_dir)
        remote_clean = remote_dir.rstrip("/")
        stage = dest_dir / "_adb_stage"
        shutil.rmtree(stage, ignore_errors=True)

        moved: set[str] = set()

        def _sweep():
            """Hand each finished file over to dest_dir right away, so an interrupted run
            keeps everything that completed (and the database can record it)."""
            # adb pull <remote_dir> <stage> either places files directly in <stage>
            # or nests <stage>/<basename> depending on platform-tools version.
            nested = stage / Path(remote_clean).name
            root = nested if nested.is_dir() else stage
            if not root.is_dir():
                return
            for item in list(root.iterdir()):
                want = expected_files.get(item.name)
                if not want or item.name in moved or not item.is_file():
                    continue
                try:
                    if item.stat().st_size != want:
                        continue  # still being written
                    target = dest_dir / item.name
                    target.unlink(missing_ok=True)
                    item.replace(target)  # can fail while adb still holds the file (Windows)
                except OSError:
                    continue  # try again on the next sweep
                moved.add(item.name)
                if on_done:
                    on_done(item.name)

        try:
            proc = self._spawn("pull", remote_clean, str(stage))
            while proc.poll() is None:
                _sweep()
                if bar:
                    pulled_bytes = sum(expected_files[n] for n in moved)
                    active_name = ""
                    if stage.is_dir():
                        for p in stage.rglob("*"):
                            if p.is_file() and p.name in expected_files and p.name not in moved:
                                try:
                                    sz = min(p.stat().st_size, expected_files[p.name])
                                except OSError:
                                    sz = 0
                                pulled_bytes += sz
                                if sz < expected_files[p.name]:
                                    active_name = p.name
                    bar.set(pulled_bytes, active_name or None)
                time.sleep(0.1)

            rc, stderr = self._reap(proc)
            _sweep()  # keep whatever finished, even if adb then failed
            if self._aborted.is_set():
                raise AdbError("cancelled")
            if rc != 0:
                raise AdbError(stderr.strip() or f"adb pull failed ({rc})")
        finally:
            shutil.rmtree(stage, ignore_errors=True)

        pulled_ok: set[str] = set()
        missing_items: list[tuple[str, Path, int]] = []
        for fname, size in expected_files.items():
            local = dest_dir / fname
            if local.is_file() and local.stat().st_size == size:
                pulled_ok.add(fname)
            else:
                local.unlink(missing_ok=True)
                missing_items.append((f"{remote_clean}/{fname}", local, size))

        if missing_items:
            pulled_ok |= self.pull_many(missing_items, bar=bar, workers=fallback_workers,
                                        on_done=on_done)
        elif bar:
            bar.set(sum(expected_files.values()))
        return pulled_ok
