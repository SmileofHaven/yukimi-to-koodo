"""Finding chapters, from index.json metadata or from filenames."""
from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .constants import CHAPTER_EXTS, MARKER
from .ui import warn
from .textutil import fmt, nat


@dataclass
class Chapter:
    number: float
    name: str
    path: Path
    size: int = 0  # file size on disk or remote, filled in before merge()


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
    by_num: dict[float, Chapter] = {}
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


def is_our_output(path):
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
        and p.resolve() not in exclude and not is_our_output(p)
    ]
    return chapters_from_names(names, folder)


def find_cover(folder, idx):
    candidates = []
    if idx and idx.get("cover_entry"):
        candidates.append(folder / idx["cover_entry"])
    candidates += [folder / f"cover{e}" for e in (".jpg", ".jpeg", ".png", ".webp")]
    return next((c for c in candidates if c.is_file()), None)
