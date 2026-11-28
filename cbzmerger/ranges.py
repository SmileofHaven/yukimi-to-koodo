"""Chapter range parsing ('7-20', '7-', '1-5,8')."""
from __future__ import annotations

import re


EPS = 1e-9


_NUM = r"(\d+(?:\.\d+)?)"


_PFX = r"(?:ch(?:apters?)?\.?\s*)?"


_RANGE_RE = re.compile(rf"^{_PFX}{_NUM}?\s*(?:-|–|—|to|\.\.|:)\s*{_PFX}{_NUM}?$", re.I)


_SINGLE_RE = re.compile(rf"^{_PFX}{_NUM}$", re.I)


def parse_ranges(spec):
    """'7-20', '7-', '-20', '3', '1-5,8,10-12' -> [(lo, hi)]; None means all."""
    spec = (spec or "").strip()
    if spec.lower() in ("", "all", "*"):
        return None
    ranges: list[tuple[float | None, float | None]] = []
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
