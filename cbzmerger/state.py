"""SQLite progress database: skip finished work, resume pulls, reuse previous output."""
from __future__ import annotations

import datetime
import hashlib
import json
import sqlite3
import threading
from pathlib import Path

from .ui import warn
from .textutil import fmt


SCHEMA_VERSION = 2


FINGERPRINT_VERSION = "1"  # bump when the merged output format changes, to force rebuilds


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    device TEXT NOT NULL, remote_dir TEXT NOT NULL, range_sig TEXT NOT NULL, mode TEXT NOT NULL,
    fingerprint TEXT NOT NULL, output_path TEXT NOT NULL, output_size INTEGER NOT NULL,
    pages INTEGER NOT NULL, chapters INTEGER NOT NULL, finished_at TEXT NOT NULL,
    PRIMARY KEY (device, remote_dir, range_sig, mode)
);
CREATE TABLE IF NOT EXISTS job_files (
    device TEXT NOT NULL, remote_dir TEXT NOT NULL, range_sig TEXT NOT NULL, mode TEXT NOT NULL,
    file TEXT NOT NULL, kind TEXT NOT NULL, size INTEGER NOT NULL, mtime TEXT NOT NULL,
    grp INTEGER NOT NULL, pages INTEGER NOT NULL, bytes INTEGER NOT NULL,
    PRIMARY KEY (device, remote_dir, range_sig, mode, file)
);
CREATE TABLE IF NOT EXISTS pulled (
    device TEXT NOT NULL, remote_path TEXT NOT NULL, size INTEGER NOT NULL, mtime TEXT NOT NULL,
    local_path TEXT NOT NULL, pulled_at TEXT NOT NULL,
    PRIMARY KEY (device, remote_path)
);
"""


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def range_signature(ranges):
    """Stable text form of a chapter range so each range is its own job."""
    if ranges is None:
        return "all"
    return ",".join(sorted(
        f"{'' if lo is None else fmt(lo)}-{'' if hi is None else fmt(hi)}" for lo, hi in ranges))


def job_fingerprint(idx, series, selected, cover_name, sizes, mtimes):
    """Hash of everything that determines the merged output: the chosen chapter files
    (name, size, modified time), the cover, and the metadata that lands in ComicInfo.xml."""
    meta = None
    if idx:
        meta = {k: idx.get(k) for k in
                ("title", "alt_titles", "description", "authors", "tags", "nsfw", "public_url")}
    h = hashlib.sha256()
    h.update(json.dumps([FINGERPRINT_VERSION, series, meta], sort_keys=True,
                        ensure_ascii=False).encode("utf-8"))
    for c in selected:
        n = c.path.name
        h.update(f"\n{n}|{fmt(c.number)}|{c.name}|{sizes[n]}|{mtimes.get(n, '')}".encode("utf-8"))
    if cover_name:
        h.update(f"\ncover|{cover_name}|{sizes[cover_name]}|{mtimes.get(cover_name, '')}".encode("utf-8"))
    return h.hexdigest()


class State:
    """Small SQLite store. If the database misbehaves it warns once and turns every call
    into a no-op, so a bad state file can never stop a transfer."""

    def __init__(self, conn, path):
        self._conn = conn
        self.path = path
        self._lock = threading.Lock()
        self._broken = False

    @classmethod
    def open(cls, path):
        try:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
            if ver > SCHEMA_VERSION:
                conn.close()
                warn(f"state database {path} is from a newer version; continuing without it")
                return None
            conn.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
            return cls(conn, path)
        except (sqlite3.Error, OSError) as e:
            warn(f"could not open the state database ({e}); continuing without it")
            return None

    def _exec(self, sql, params=(), fetch=False):
        with self._lock:
            if self._broken:
                return None
            try:
                cur = self._conn.execute(sql, params)
                rows = cur.fetchall() if fetch else None
                self._conn.commit()
                return rows
            except sqlite3.Error as e:
                self._broken = True
                warn(f"state database problem ({e}); continuing without it")
                return None

    def job_hit(self, device, remote_dir, range_sig, mode, fingerprint):
        rows = self._exec(
            "SELECT output_path, output_size, pages, chapters, finished_at FROM jobs "
            "WHERE device=? AND remote_dir=? AND range_sig=? AND mode=? AND fingerprint=?",
            (device, remote_dir, range_sig, mode, fingerprint), fetch=True)
        return rows[0] if rows else None

    def last_job(self, device, remote_dir, range_sig, mode):
        """The recorded job for this key whatever its fingerprint: (fingerprint, output_path,
        output_size, pages, chapters, finished_at), or None."""
        rows = self._exec(
            "SELECT fingerprint, output_path, output_size, pages, chapters, finished_at FROM jobs "
            "WHERE device=? AND remote_dir=? AND range_sig=? AND mode=?",
            (device, remote_dir, range_sig, mode), fetch=True)
        return rows[0] if rows else None

    def job_files(self, device, remote_dir, range_sig, mode):
        """Where each source file ended up in that job's output:
        [(file, kind, size, mtime, group, pages, image_bytes), ...]"""
        return self._exec(
            "SELECT file, kind, size, mtime, grp, pages, bytes FROM job_files "
            "WHERE device=? AND remote_dir=? AND range_sig=? AND mode=?",
            (device, remote_dir, range_sig, mode), fetch=True) or []

    def output_in_use(self, output_path, device, remote_dir, range_sig, mode):
        """Is this output file the recorded output of some *other* job?"""
        rows = self._exec(
            "SELECT 1 FROM jobs WHERE output_path=? "
            "AND NOT (device=? AND remote_dir=? AND range_sig=? AND mode=?)",
            (output_path, device, remote_dir, range_sig, mode), fetch=True)
        return bool(rows)

    def record_job(self, device, remote_dir, range_sig, mode, fingerprint,
                   output_path, output_size, pages, chapters, files=()):
        """Store a finished job and its layout in one transaction."""
        with self._lock:
            if self._broken:
                return
            key = (device, remote_dir, range_sig, mode)
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)",
                    key + (fingerprint, output_path, output_size, pages, chapters, _now()))
                self._conn.execute(
                    "DELETE FROM job_files WHERE device=? AND remote_dir=? AND range_sig=? AND mode=?", key)
                self._conn.executemany(
                    "INSERT INTO job_files VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [key + tuple(r) for r in files])
                self._conn.commit()
            except sqlite3.Error as e:
                self._broken = True
                warn(f"state database problem ({e}); continuing without it")

    def mark_pulled(self, device, remote_path, size, mtime, local_path):
        self._exec("INSERT OR REPLACE INTO pulled VALUES (?,?,?,?,?,?)",
                   (device, remote_path, size, mtime, local_path, _now()))

    def pull_valid(self, device, remote_path, size, mtime):
        """True only if this exact remote file (same size + modified time) was fully pulled."""
        rows = self._exec(
            "SELECT 1 FROM pulled WHERE device=? AND remote_path=? AND size=? AND mtime=?",
            (device, remote_path, size, mtime), fetch=True)
        return bool(rows)

    def forget_pulled(self, device, remote_dir):
        esc = remote_dir.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        self._exec("DELETE FROM pulled WHERE device=? AND remote_path LIKE ? ESCAPE '\\'",
                   (device, esc + "/%"))

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
