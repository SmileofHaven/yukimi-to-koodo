#!/usr/bin/env python3
"""Merge chapter-wise CBZ files into a single CBZ with ComicInfo.xml
(chapter bookmarks included) so Koodo Reader shows a proper chapter list.

Local folder
  cbz_merger.py                         # no arguments -> interactive menu
  cbz_merger.py INPUT [-o OUT] [-m auto|index|filename] [-r RANGE] [--index FILE]

Batch (pull straight from your phone over adb, Kotatsu layout)
  cbz_merger.py --batch -o OUT [-r RANGE] [--series NAME ...]

Examples
  cbz_merger.py ./Series -o ./out -r 7-20
  cbz_merger.py ./Series -m filename -r "1-5,8,10-"
  cbz_merger.py -b -o ./out --series "shadow slave" -r 1-10
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
CHAPTER_EXTS = {".cbz", ".zip"}
MARKER = "Merged by yukimi-to-koodo"  # lets filename mode skip our own outputs
EPS = 1e-9
ADB_URL = "https://dl.google.com/android/repository/platform-tools-latest-{os}.zip"
REMOTE_DEFAULT = "/sdcard/Android/data/org.koitharu.kotatsu.nightly/files/manga"


# =========================================================================== #
# console UI: colors + progress bar
# =========================================================================== #
USE_COLOR = False
FANCY = False  # live redraw (\r) allowed
FILL, EMPTY = "#", "-"
_STYLES = {"red": "31", "green": "32", "yellow": "33", "blue": "34",
           "magenta": "35", "cyan": "36", "gray": "90", "bold": "1"}


def init_ui(no_color=False):
    """Colors/progress need a real terminal. Git Bash (mintty) reports no tty,
    so MSYSTEM / FORCE_COLOR also count."""
    global USE_COLOR, FANCY, FILL, EMPTY
    forced = bool(os.environ.get("FORCE_COLOR"))
    term = forced or sys.stdout.isatty() or bool(os.environ.get("MSYSTEM"))
    FANCY = term
    USE_COLOR = term and not no_color and "NO_COLOR" not in os.environ
    if USE_COLOR and os.name == "nt":
        os.system("")  # switches on ANSI escape handling in the Windows console
    try:
        "█░".encode(sys.stdout.encoding or "ascii")
        FILL, EMPTY = "█", "░"
    except (UnicodeEncodeError, LookupError):
        pass


def paint(text, *styles):
    if not USE_COLOR or not styles:
        return str(text)
    return "\033[" + ";".join(_STYLES[s] for s in styles) + "m" + str(text) + "\033[0m"


def fmt_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


class Bar:
    """Single-line progress bar. Messages printed while it is active go through
    _emit(), which clears the bar line first and redraws it afterwards."""

    active = None

    def __init__(self, total, label="", as_bytes=True):
        self.total = total or 1
        self.label = label
        self.as_bytes = as_bytes
        self.value = 0
        self.status = ""
        self._last_draw = 0.0
        self._last_step = -1
        Bar.active = self
        self.draw(force=True)

    def set(self, value, status=None):
        self.value = min(value, self.total)
        if status is not None:
            self.status = status
        self.draw()

    def advance(self, n, status=None):
        self.set(self.value + n, status)

    def clear(self):
        if FANCY:
            cols = shutil.get_terminal_size((80, 20)).columns
            sys.stdout.write("\r" + " " * (cols - 1) + "\r")
            sys.stdout.flush()

    def draw(self, force=False):
        frac = self.value / self.total
        if not FANCY:  # piped output: one line per 10 %
            step = int(frac * 10)
            if step > self._last_step:
                self._last_step = step
                print(f"{self.label} {step * 10:3d}%", flush=True)
            return
        now = time.monotonic()
        if not force and frac < 1 and now - self._last_draw < 0.05:
            return
        self._last_draw = now
        cols = shutil.get_terminal_size((80, 20)).columns
        counts = (f"{fmt_size(self.value)}/{fmt_size(self.total)}" if self.as_bytes
                  else f"{int(self.value)}/{int(self.total)}")
        pct = f"{frac * 100:5.1f}%"
        base = f"{self.label} [] {pct} {counts} "
        width = max(10, min(30, cols - len(base) - 12))
        filled = int(width * frac)
        room = max(0, cols - len(base) - width - 1)
        status = self.status[:room]
        line = (f"{paint(self.label, 'bold')} "
                f"[{paint(FILL * filled, 'green')}{paint(EMPTY * (width - filled), 'gray')}] "
                f"{paint(pct, 'cyan')} {counts} {paint(status, 'gray')}")
        sys.stdout.write("\r" + line)
        sys.stdout.flush()

    def close(self):
        self.value = self.total
        self.draw(force=True)
        if FANCY:
            sys.stdout.write("\n")
            sys.stdout.flush()
        Bar.active = None

    def abort(self):
        self.clear()
        Bar.active = None


def _emit(msg, stream=None):
    stream = stream or sys.stdout
    bar = Bar.active
    if bar:
        bar.clear()
    print(msg, file=stream, flush=True)
    if bar:
        bar.draw(force=True)


def info(msg):
    _emit(f"{paint('::', 'cyan', 'bold')} {msg}")


def ok(msg):
    _emit(f"{paint('OK', 'green', 'bold')} {msg}")


def warn(msg):
    _emit(f"{paint('!', 'yellow', 'bold')} {paint(msg, 'yellow')}", sys.stderr)


def err(msg):
    _emit(f"{paint('error:', 'red', 'bold')} {msg}", sys.stderr)


def ask(prompt):
    return input(paint(prompt, "cyan") + " ")


# =========================================================================== #
# helpers
# =========================================================================== #
@dataclass
class Chapter:
    number: float
    name: str
    path: Path
    size: int = 0  # bytes of image data, filled in by probe()


def nat(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(s))]


def fmt(n):
    n = float(n)
    return str(int(n)) if n.is_integer() else str(n)


def attr(s):
    """Escape a string for use inside a double-quoted XML attribute."""
    return escape(str(s), {'"': "&quot;"})


def safe_filename(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name)).strip(" .")
    return name or "output"


def compress(numbers):
    """[1,2,3,5,6,8] -> '1-3, 5-6, 8' (integers collapse, fractions stay alone)."""
    nums = sorted(set(numbers))
    parts, run = [], []

    def flush():
        if run:
            parts.append(fmt(run[0]) if len(run) == 1 else f"{fmt(run[0])}-{fmt(run[-1])}")
            run.clear()

    for n in nums:
        if not float(n).is_integer():
            flush()
            parts.append(fmt(n))
        elif run and n == run[-1] + 1:
            run.append(n)
        else:
            flush()
            run.append(n)
    flush()
    return ", ".join(parts)


# =========================================================================== #
# range parsing
# =========================================================================== #
_NUM = r"(\d+(?:\.\d+)?)"
_PFX = r"(?:ch(?:apters?)?\.?\s*)?"
_RANGE_RE = re.compile(rf"^{_PFX}{_NUM}?\s*(?:-|–|—|to|\.\.|:)\s*{_PFX}{_NUM}?$", re.I)
_SINGLE_RE = re.compile(rf"^{_PFX}{_NUM}$", re.I)


def parse_ranges(spec):
    """'7-20', '7-', '-20', '3', '1-5,8,10-12' -> [(lo, hi)]; None means all."""
    spec = (spec or "").strip()
    if spec.lower() in ("", "all", "*"):
        return None
    ranges = []
    for part in re.split(r"[,;]", spec):
        part = part.strip()
        if not part:
            continue
        m = _SINGLE_RE.match(part)
        if m:
            n = float(m.group(1))
            ranges.append((n, n))
            continue
        m = _RANGE_RE.match(part)
        if not m or (m.group(1) is None and m.group(2) is None):
            raise ValueError(f"can't understand range '{part}' (try 7-20, 7-, -20, 5 or 1-5,8)")
        lo = float(m.group(1)) if m.group(1) else None
        hi = float(m.group(2)) if m.group(2) else None
        if lo is not None and hi is not None and lo > hi:
            raise ValueError(f"range '{part}' is backwards")
        ranges.append((lo, hi))
    return ranges or None


def in_ranges(n, ranges):
    if ranges is None:
        return True
    return any(
        (lo is None or n >= lo - EPS) and (hi is None or n <= hi + EPS)
        for lo, hi in ranges
    )


def requested_but_absent(ranges, available):
    """Whole chapter numbers the user explicitly asked for that don't exist."""
    if ranges is None or not available:
        return []
    have = {round(n, 6) for n in available}
    lo_bound, hi_bound = min(available), max(available)
    missing = set()
    for lo, hi in ranges:
        lo_i = int(-(-(lo if lo is not None else lo_bound) // 1))  # ceil
        hi_i = int((hi if hi is not None else hi_bound) // 1)  # floor
        for i in range(lo_i, hi_i + 1):
            if float(i) not in have:
                missing.add(float(i))
    return sorted(missing)


# =========================================================================== #
# chapter discovery
# =========================================================================== #
_N = r"(\d+(?:[._]\d{1,2}(?!\d))?)"  # 20, 20.5, 20_5 (underscore stands in for the dot)
_KEYWORD_NUM = re.compile(rf"(?<![a-z])(?:chapter|chap|ch|episode|ep|c)[\s._\-#]*{_N}", re.I)
_HASH_NUM = re.compile(rf"#\s*{_N}")
_ANY_NUM = re.compile(r"\d+(?:\.\d+)?")


def _to_float(txt):
    return float(txt.replace("_", "."))


def number_from_name(name):
    """Real chapter number from a title/filename ('Chapter 20.5', 'ch_20_5'), else None."""
    m = _KEYWORD_NUM.search(name) or _HASH_NUM.search(name)
    return _to_float(m.group(1)) if m else None


def number_from_filename(stem):
    n = number_from_name(stem)
    if n is not None:
        return n
    nums = _ANY_NUM.findall(stem)
    return float(nums[-1]) if nums else None


def load_index(index_path):
    return json.loads(Path(index_path).read_text(encoding="utf-8"))


def chapters_from_index(idx, folder, available=None):
    """Chapters listed in the index. `available` (set of filenames) replaces the
    on-disk check, so a phone's file list can be used before anything is pulled."""
    raw = idx.get("chapters", {})
    entries = list(raw.values()) if isinstance(raw, dict) else list(raw)
    found, absent = [], []
    for c in entries:
        # Kotatsu's "number" is a running position (Chapter 20.5 -> 21), so prefer
        # the real number from the chapter name and only fall back to the counter.
        num = number_from_name(str(c.get("name") or ""))
        if num is None:
            try:
                num = float(c["number"])
            except (KeyError, TypeError, ValueError):
                warn(f"index entry without a usable chapter number, skipped: {c!r}")
                continue
        fname = c.get("file")
        if not fname:
            absent.append(num)
            continue
        path = folder / fname
        exists = (fname in available) if available is not None else path.is_file()
        if not exists:
            absent.append(num)
            continue
        found.append(Chapter(num, str(c.get("name") or f"Chapter {fmt(num)}"), path))
    found.sort(key=lambda c: c.number)
    for a, b in zip(found, found[1:]):
        if a.number == b.number:
            warn(f"two index entries share chapter number {fmt(a.number)}: {a.name!r}, {b.name!r}")
    return found, absent


def chapters_from_names(names, folder):
    """Detect chapters from filenames alone (files need not exist yet)."""
    by_num = {}
    for name in sorted(names, key=nat):
        p = Path(name)
        if p.suffix.lower() not in CHAPTER_EXTS:
            continue
        n = number_from_filename(p.stem)
        if n is None:
            warn(f"no chapter number in filename, skipped: {name}")
            continue
        if n in by_num:
            warn(f"duplicate chapter {fmt(n)}: keeping {by_num[n].path.name}, skipping {name}")
            continue
        label = p.stem if re.search(r"[A-Za-z]", p.stem) else f"Chapter {fmt(n)}"
        by_num[n] = Chapter(n, label, folder / name)
    return sorted(by_num.values(), key=lambda c: c.number)


def _is_our_output(path):
    try:
        with zipfile.ZipFile(path) as z:
            return "ComicInfo.xml" in z.namelist() and MARKER.encode() in z.read("ComicInfo.xml")
    except (zipfile.BadZipFile, OSError, KeyError):
        return False


def chapters_from_filenames(folder, exclude=()):
    exclude = {Path(e).resolve() for e in exclude}
    names = [
        p.name for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in CHAPTER_EXTS
        and p.resolve() not in exclude and not _is_our_output(p)
    ]
    return chapters_from_names(names, folder)


def find_cover(folder, idx):
    candidates = []
    if idx and idx.get("cover_entry"):
        candidates.append(folder / idx["cover_entry"])
    candidates += [folder / f"cover{e}" for e in (".jpg", ".jpeg", ".png", ".webp")]
    return next((c for c in candidates if c.is_file()), None)


# =========================================================================== #
# building the merged CBZ
# =========================================================================== #
def _is_image(name):
    return not name.endswith("/") and "__MACOSX" not in name and Path(name).suffix.lower() in IMG_EXTS


def build_comicinfo(idx, series, pages, bookmarks):
    page_xml = "\n".join(
        f'    <Page Image="{i}" Type="{t}"'
        + (f' Bookmark="{attr(bookmarks[i])}"' if i in bookmarks else "")
        + " />"
        for i, t in enumerate(pages)
    )
    lines = ['<?xml version="1.0" encoding="utf-8"?>', "<ComicInfo>",
             f"  <Series>{escape(series)}</Series>"]
    if idx:
        authors = ", ".join(a for a in idx.get("authors", []) if a)
        genres = ", ".join(t.get("title", "") for t in idx.get("tags", []) if t.get("title"))
        alt = next((a for a in idx.get("alt_titles", []) if a), "")
        age = "Adults Only 18+" if idx.get("nsfw") else "Everyone"
        lines += [
            f"  <LocalizedSeries>{escape(alt)}</LocalizedSeries>",
            f"  <Summary>{escape(idx.get('description') or '')}</Summary>",
            f"  <Writer>{escape(authors)}</Writer>",
            f"  <Genre>{escape(genres)}</Genre>",
            f"  <Web>{escape(idx.get('public_url') or '')}</Web>",
            f"  <AgeRating>{age}</AgeRating>",
        ]
    lines += [f"  <Notes>{MARKER}</Notes>", f"  <PageCount>{len(pages)}</PageCount>",
              "  <Manga>Yes</Manga>", "  <Pages>", page_xml, "  </Pages>", "</ComicInfo>", ""]
    return "\n".join(lines)


def probe(chapters):
    """Drop chapters that can't be read or hold no images; record image bytes."""
    good = []
    for ch in chapters:
        try:
            with zipfile.ZipFile(ch.path) as z:
                imgs = [i for i in z.infolist() if _is_image(i.filename)]
        except (zipfile.BadZipFile, OSError) as e:
            warn(f"chapter {fmt(ch.number)} ({ch.path.name}) unreadable ({e}), skipped")
            continue
        if not imgs:
            warn(f"chapter {fmt(ch.number)} ({ch.path.name}) has no images, skipped")
            continue
        ch.size = sum(i.file_size for i in imgs)
        good.append(ch)
    return good


def merge(chapters, idx, series, cover, out_path):
    """Write the merged CBZ. Returns (pages, chapters_written, skipped_names)."""
    pages, bookmarks, skipped = [], {}, []
    tmp = out_path.with_name(out_path.name + ".part")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    total = sum(ch.size for ch in chapters) + (cover.stat().st_size if cover else 0)
    bar = Bar(total, "Merging")
    finished = False
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as dst:
            if cover:
                dst.write(cover, "0000_00000" + cover.suffix.lower())
                pages.append("FrontCover")
                bar.advance(cover.stat().st_size, "cover")

            written = 0
            for ch in chapters:
                bar.set(bar.value, ch.name)
                try:
                    with zipfile.ZipFile(ch.path) as src:
                        infos = sorted((i for i in src.infolist() if _is_image(i.filename)),
                                       key=lambda i: nat(i.filename))
                        data = []  # read the whole chapter first so a bad one leaves no half-chapter
                        for i in infos:
                            data.append((Path(i.filename).suffix.lower(), src.read(i)))
                            bar.advance(i.file_size)
                except (zipfile.BadZipFile, OSError) as e:
                    warn(f"chapter {fmt(ch.number)} ({ch.path.name}) unreadable ({e}), skipped")
                    skipped.append(ch.path.name)
                    continue
                written += 1
                bookmarks[len(pages)] = ch.name
                for pi, (ext, blob) in enumerate(data, start=1):
                    dst.writestr(f"{written:04d}_{pi:05d}{ext}", blob)
                    pages.append("Story")

            if written == 0:
                raise RuntimeError("none of the selected chapters could be read")
            dst.writestr("ComicInfo.xml", build_comicinfo(idx, series, pages, bookmarks))
        tmp.replace(out_path)
        finished = True
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        bar.close() if finished else bar.abort()
    return len(pages), written, skipped


def resolve_output(out_arg, default_dir, filename):
    if out_arg:
        p = Path(out_arg).expanduser()
        return p.resolve() if p.suffix.lower() == ".cbz" else (p / filename).resolve()
    return (default_dir / filename).resolve()


def finish(selected, all_count, idx, series, cover_dir, out_arg, default_dir, taken=None, tag=""):
    """probe -> name the output -> merge. Raises RuntimeError if nothing usable."""
    selected = probe(selected)
    if not selected:
        raise RuntimeError("none of the selected chapters could be read")
    if len(selected) == all_count:
        suffix = ""
    elif len(selected) == 1:
        suffix = f" (Ch {fmt(selected[0].number)})"
    else:
        suffix = f" (Ch {fmt(selected[0].number)}-{fmt(selected[-1].number)})"
    out_path = resolve_output(out_arg, default_dir, safe_filename(series + suffix) + ".cbz")
    if taken is not None:
        if out_path in taken and tag:  # same title as a series already written in this run
            out_path = resolve_output(out_arg, default_dir, safe_filename(f"{series}{suffix} [{tag}]") + ".cbz")
            warn(f"another series already produced that filename; using {out_path.name}")
        taken.add(out_path)
    selected = [c for c in selected if c.path.resolve() != out_path]  # never merge the output into itself
    if not selected:
        raise RuntimeError("nothing left to merge")
    cover = find_cover(cover_dir, idx)
    info(f"Merging {len(selected)} chapter(s) -> {paint(out_path, 'bold')}")
    pages, written, skipped = merge(selected, idx, series, cover, out_path)
    return out_path, pages, written, skipped


# =========================================================================== #
# adb (batch mode)
# =========================================================================== #
class AdbError(Exception):
    pass


@dataclass
class RemoteEntry:
    name: str
    is_dir: bool
    size: int


_LS_RE = re.compile(
    r"^([\-dlcbps])\S*\s+\d+\s+\S+\s+\S+\s+(\d+)\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+(.+?)(?:\s+->\s+.*)?$"
)

NO_DEVICE_HELP = [
    "No device found. On your phone:",
    "  1. Enable Developer options (Settings > About phone > tap 'Build number' 7 times)",
    "  2. Turn on USB debugging (Settings > Developer options)",
    "  3. Connect the phone to this PC with a USB cable",
    "  4. Unlock the phone and tap 'Allow' on the 'Allow USB debugging?' prompt",
    "  5. Run this command again",
]


def _adb_name():
    return "adb.exe" if os.name == "nt" else "adb"


def tools_dir():
    """Private folder for the auto-installed platform-tools (PATH is never touched)."""
    env = os.environ.get("CBZ_TOOLS_DIR")
    if env:
        return Path(env)
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "yukimi-to-koodo"


def find_adb():
    exe = shutil.which("adb")
    if exe:
        return exe
    local = tools_dir() / "platform-tools" / _adb_name()
    if local.is_file():
        return str(local)
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(var)
        if root:
            cand = Path(root) / "platform-tools" / _adb_name()
            if cand.is_file():
                return str(cand)
    return None


def install_adb():
    """Download Google's platform-tools and unpack them into tools_dir().
    Returns the adb path, or None on failure."""
    osname = "windows" if sys.platform == "win32" else "darwin" if sys.platform == "darwin" else "linux"
    url = os.environ.get("CBZ_ADB_URL") or ADB_URL.format(os=osname)
    dest = tools_dir()
    zpath = dest / "platform-tools.zip"
    part = zpath.with_name(zpath.name + ".part")
    try:
        dest.mkdir(parents=True, exist_ok=True)
        info(f"Downloading platform-tools from {paint(url, 'bold')}")
        with urllib.request.urlopen(url, timeout=30) as resp, open(part, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            bar = Bar(total, "Downloading")
            finished = False
            try:
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    out.write(chunk)
                    bar.advance(len(chunk))
                finished = True
            finally:
                bar.close() if finished else bar.abort()
        part.replace(zpath)

        with zipfile.ZipFile(zpath) as z:
            bad = z.testzip()
            if bad:
                raise zipfile.BadZipFile(f"corrupt download ({bad})")
            if not any(n.replace("\\", "/").startswith("platform-tools/") for n in z.namelist()):
                raise zipfile.BadZipFile("unexpected archive layout")
            target = dest / "platform-tools"
            if target.exists():
                shutil.rmtree(target)
            info("Unpacking...")
            z.extractall(dest)  # zipfile strips absolute paths and '..' components
        adb_path = dest / "platform-tools" / _adb_name()
        if not adb_path.is_file():
            raise RuntimeError("adb was not found inside the downloaded archive")
        if os.name != "nt":
            adb_path.chmod(0o755)
        r = subprocess.run([str(adb_path), "version"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        if r.returncode != 0:
            raise RuntimeError("the downloaded adb would not start: " + (r.stderr or r.stdout).strip())
        ok(f"adb installed to {paint(adb_path.parent, 'bold')} (your PATH was not changed)")
        return str(adb_path)
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, subprocess.SubprocessError) as e:
        err(f"could not install adb automatically: {e}")
        return None
    finally:
        part.unlink(missing_ok=True)
        zpath.unlink(missing_ok=True)


def offer_adb_install(auto):
    """adb is missing: ask (or just do it with --install-adb). Returns adb path or None."""
    warn("adb (Android platform-tools) was not found on this PC.")
    if not auto:
        try:
            ans = ask("Download and install it now from Google (~10 MB, no admin rights needed)? [Y/n]:")
        except EOFError:
            print()
            ans = "n"
        if ans.strip().lower() not in ("", "y", "yes"):
            warn("Install platform-tools yourself (https://developer.android.com/tools/releases/platform-tools) "
                 "and add it to PATH, or re-run and answer Y. Use --install-adb to skip this question.")
            return None
    return install_adb()


def parse_devices(text):
    devices, seen_header = [], False
    for line in text.replace("\r", "").splitlines():
        line = line.strip()
        if line.lower().startswith("list of devices"):
            seen_header = True
            continue
        if not seen_header or not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            devices.append((parts[0], parts[1]))
    return devices


class Adb:
    def __init__(self, exe, serial=None):
        self.exe = exe
        self.serial = serial

    def _cmd(self, *args):
        return [self.exe] + (["-s", self.serial] if self.serial else []) + list(args)

    def run(self, *args, timeout=60):
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
            if not m or m.group(3) in (".", ".."):
                continue
            kind, size, name = m.group(1), int(m.group(2)), m.group(3)
            if kind in ("-", "d"):
                entries.append(RemoteEntry(name, kind == "d", size))
        return entries

    def pull(self, remote, local, size, bar=None):
        """Pull one file, advancing `bar` smoothly by watching the growing local file."""
        local = Path(local)
        if local.is_file() and local.stat().st_size == size:  # left over from an earlier run
            if bar:
                bar.advance(size, local.name)
            return
        base = bar.value if bar else 0
        proc = subprocess.Popen(self._cmd("pull", remote, str(local)),
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        while proc.poll() is None:
            if bar:
                try:
                    cur = local.stat().st_size
                except OSError:
                    cur = 0
                bar.set(base + min(cur, size), local.name)
            time.sleep(0.1)
        _, stderr = proc.communicate()
        if proc.returncode != 0:
            raise AdbError(stderr.strip() or f"adb pull failed ({proc.returncode})")
        got = local.stat().st_size if local.is_file() else -1
        if got != size:
            raise AdbError(f"{local.name}: pulled {got} bytes, expected {size}")
        if bar:
            bar.set(base + size, local.name)


def connect_adb(serial=None, interactive=False, auto_install=False):
    exe = find_adb() or offer_adb_install(auto_install)
    if not exe:
        return None
    info(f"Checking for devices: {paint('adb devices', 'bold')}")
    try:
        r = subprocess.run([exe, "devices"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
    except subprocess.TimeoutExpired:
        err("`adb devices` timed out. Try `adb kill-server` and run this again.")
        return None
    devices = parse_devices(r.stdout)
    ready = [s for s, st in devices if st == "device"]

    if serial:
        if serial not in ready:
            err(f"device '{serial}' is not connected/ready. Seen: "
                + (", ".join(f"{s} ({st})" for s, st in devices) or "none"))
            return None
        chosen = serial
    elif len(ready) == 1:
        chosen = ready[0]
    elif len(ready) > 1:
        if not interactive:
            err("more than one device connected; pick one with --serial: " + ", ".join(ready))
            return None
        print(paint("Several devices are connected:", "bold"))
        for i, s in enumerate(ready, 1):
            print(f"  {paint(str(i), 'cyan')}. {s}")
        while True:
            c = ask("Which one?").strip()
            if c.isdigit() and 1 <= int(c) <= len(ready):
                chosen = ready[int(c) - 1]
                break
    else:
        states = {st for _, st in devices}
        if "unauthorized" in states:
            err("A device is connected but not authorized. Unlock the phone, tap 'Allow' on the "
                "'Allow USB debugging?' prompt (tick 'Always allow'), then try again.")
        elif "offline" in states:
            err("The device shows as offline. Unplug and reconnect the cable, re-allow the USB "
                "debugging prompt, or run `adb kill-server`, then try again.")
        else:
            _emit(paint(NO_DEVICE_HELP[0], "red", "bold"), sys.stderr)
            for line in NO_DEVICE_HELP[1:]:
                warn_plain(line)
        return None
    ok(f"Device connected: {paint(chosen, 'bold')}")
    return Adb(exe, chosen)


def warn_plain(line):
    _emit(paint(line, "yellow"), sys.stderr)


# =========================================================================== #
# interactive prompts
# =========================================================================== #
def _clean_path(s):
    return Path(s.strip().strip("\"'")).expanduser()


def _pick_with_dialog(kind, title):
    """Open a native picker. Returns a Path, or None if cancelled/unavailable."""
    try:
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            if kind == "file":
                chosen = filedialog.askopenfilename(
                    title=title, filetypes=[("JSON", "*.json"), ("All files", "*.*")])
            else:
                chosen = filedialog.askdirectory(title=title)
        finally:
            root.destroy()
        return Path(chosen) if chosen else None
    except Exception:
        return None


def ask_folder(label, default=None, must_exist=True):
    hint = f" [Enter = {default}]" if default else ""
    while True:
        raw = ask(f"{label}{hint} (type a path, or ? to browse):").strip()
        if raw == "?":
            picked = _pick_with_dialog("dir", label)
            if picked is None:
                print("  (no file picker available or cancelled; type the path instead)")
                continue
            p = picked
        elif not raw and default:
            p = Path(default)
        elif not raw:
            continue
        else:
            p = _clean_path(raw)
        if must_exist and not p.is_dir():
            print(f"  '{p}' is not a folder, try again.")
            continue
        return p.resolve()


def ask_index_file():
    """File picker for index.json; falls back to typing a path."""
    print("  Opening file picker...")
    picked = _pick_with_dialog("file", "Select index.json")
    if picked:
        return picked
    print("  (file picker unavailable or cancelled)")
    raw = ask("  Type the path to index.json, or press Enter to cancel:").strip()
    if not raw:
        return None
    p = _clean_path(raw)
    if not p.is_file():
        print(f"  '{p}' is not a file.")
        return None
    return p


def show_available(chapters):
    nums = [c.number for c in chapters]
    print(f"\n{paint('Available chapters', 'bold')} ({len(chapters)}): {paint(compress(nums), 'green')}")
    ints = {int(n) for n in nums if float(n).is_integer()}
    if ints:
        gaps = [float(i) for i in range(min(ints), max(ints) + 1) if i not in ints]
        if gaps:
            print(f"{paint('Missing / no file', 'bold')}: {paint(compress(gaps), 'yellow')}")


def ask_range(chapters=None):
    if chapters is not None:
        show_available(chapters)
    while True:
        raw = ask("\nChapter range, e.g. 7-20, 7-, -20, 1-5,8 [Enter = all]:")
        try:
            return parse_ranges(raw)
        except ValueError as e:
            print(f"  {paint(str(e), 'red')}")


def ask_menu():
    print(paint("=== CBZ merger (Yukimi -> Koodo) ===", "bold", "cyan"))
    print(f"  {paint('1', 'cyan')}) Merge a local folder")
    print(f"  {paint('2', 'cyan')}) Batch: auto-scan your phone over adb (Kotatsu)")
    while True:
        c = ask("Choose [1/2, Enter = 1]:").strip() or "1"
        if c in ("1", "2"):
            return "local" if c == "1" else "batch"


def interactive_local():
    folder = ask_folder("Input folder (chapter CBZs + index.json)")
    out = ask_folder("Output folder", default=folder.parent, must_exist=False)
    idx_path = folder / "index.json"
    mode = "index"
    if not idx_path.is_file():
        print(f"\n{paint('No index.json found in', 'yellow')} {folder}.")
        while True:
            c = ask("  [1] Select an index.json manually   [2] Use filename detection  >").strip()
            if c == "1":
                picked = ask_index_file()
                if picked:
                    idx_path = picked
                    break
                print("  No index.json selected.")
            elif c == "2":
                mode = "filename"
                break
    return folder, out, mode, idx_path


# =========================================================================== #
# local run
# =========================================================================== #
def run_local(args, interactive):
    if interactive:
        folder, out_arg, mode, idx_path = interactive_local()
        out_arg = str(out_arg)
        ranges = None  # asked once the chapters are known
    else:
        folder = _clean_path(args.input_opt or args.input or ".").resolve()
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


# =========================================================================== #
# batch run (phone over adb)
# =========================================================================== #
def process_remote_series(adb, remote_root, name, args, ranges, out_arg, default_dir, tmp_root, taken):
    rdir = f"{remote_root.rstrip('/')}/{name}"
    files = {e.name: e.size for e in adb.ls(rdir) if not e.is_dir}
    if not files:
        raise RuntimeError("folder is empty")
    tmp = tmp_root / safe_filename(name)
    tmp.mkdir(parents=True, exist_ok=True)

    idx = None
    mode = args.mode
    if mode == "index" and "index.json" not in files:
        raise RuntimeError("--mode index but this series has no index.json")
    if mode != "filename" and "index.json" in files:
        adb.pull(f"{rdir}/index.json", tmp / "index.json", files["index.json"])
        idx = load_index(tmp / "index.json")
        chapters, absent = chapters_from_index(idx, tmp, available=set(files))
        info(f"Using index metadata ({len(chapters)} chapters listed on the phone)")
        if absent:
            warn(f"listed in index but file missing: {compress(absent)}")
        series = str(idx.get("title") or name.replace("_", " "))
    else:
        info("No index.json on the phone; using filename detection."
             if mode == "auto" else "Using filename detection.")
        chapters = chapters_from_names(list(files), tmp)
        series = name.replace("_", " ")
    if not chapters:
        raise RuntimeError("no chapter files found")

    selected = [c for c in chapters if in_ranges(c.number, ranges)]
    gone = requested_but_absent(ranges, [c.number for c in chapters])
    if gone:
        warn(f"requested but not available, skipping: {compress(gone)}")
    if not selected:
        show_available(chapters)
        raise RuntimeError("no available chapters fall in the requested range")

    # pull only what the range needs (+ cover)
    wanted = [c.path.name for c in selected]
    cover_name = None
    if idx and idx.get("cover_entry") in files:
        cover_name = idx["cover_entry"]
    else:
        cover_name = next((f"cover{e}" for e in (".jpg", ".jpeg", ".png", ".webp")
                           if f"cover{e}" in files), None)
    if cover_name:
        wanted.append(cover_name)
    total = sum(files[w] for w in wanted)
    bar = Bar(total, "Pulling")
    pulled_ok = set()
    finished = False
    try:
        for w in wanted:
            try:
                adb.pull(f"{rdir}/{w}", tmp / w, files[w], bar)
                pulled_ok.add(w)
            except AdbError as e:
                warn(f"could not pull {w}: {e}")
                bar.advance(files[w])
        finished = True
    finally:
        bar.close() if finished else bar.abort()
    selected = [c for c in selected if c.path.name in pulled_ok]
    if not selected:
        raise RuntimeError("no chapters could be pulled")

    return finish(selected, len(chapters), idx, series, tmp, out_arg, default_dir, taken, name)


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
        entries = adb.ls(remote_root)
    except AdbError as e:
        err(f"could not list {remote_root}: {e}")
        warn("Is Kotatsu installed, and has it downloaded something? Use --remote-path for another location.")
        return 1
    series = sorted((e.name for e in entries if e.is_dir), key=nat)
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

    tmp_root = Path(tempfile.mkdtemp(prefix="cbzmerge_", dir=args.temp))
    results = []
    taken = set()  # output paths already written in this run
    try:
        for n, name in enumerate(chosen, 1):
            print()
            info(f"[{n}/{len(chosen)}] {paint(name, 'bold', 'magenta')}")
            try:
                res = process_remote_series(adb, remote_root, name, args, ranges,
                                            out_arg, default_dir, tmp_root, taken)
                results.append((name, res, None))
                out_path, pages, written, skipped = res
                extra = f", {len(skipped)} skipped" if skipped else ""
                ok(f"Wrote {paint(out_path, 'bold')} ({pages} pages, {written} chapters{extra})")
            except (AdbError, RuntimeError, OSError, ValueError) as e:
                err(f"{name}: {e}")
                results.append((name, None, str(e)))
            finally:
                if not args.keep_temp:
                    shutil.rmtree(tmp_root / safe_filename(name), ignore_errors=True)
    finally:
        if args.keep_temp:
            info(f"Temporary files kept in {tmp_root}")
        else:
            shutil.rmtree(tmp_root, ignore_errors=True)

    print(f"\n{paint('Summary', 'bold')}")
    for name, res, why in results:
        if res:
            print(f"  {paint('OK  ', 'green', 'bold')} {name} -> {res[0].name}")
        else:
            print(f"  {paint('FAIL', 'red', 'bold')} {name}: {why}")
    good = sum(1 for _, r, _ in results if r)
    return 0 if good == len(results) else 1


# =========================================================================== #
# CLI
# =========================================================================== #
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
    b.add_argument("--remote-path", default=REMOTE_DEFAULT, metavar="PATH",
                   help="manga folder on the phone (default: Kotatsu nightly)")
    b.add_argument("--series", action="append", metavar="NAME",
                   help="only these series (case-insensitive substring; repeatable)")
    b.add_argument("--serial", metavar="SERIAL", help="adb device serial when several are connected")
    b.add_argument("--temp", metavar="DIR", help="where to stage files pulled from the phone")
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


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        if Bar.active:
            Bar.active.abort()
        print("\nCancelled.")
        sys.exit(130)