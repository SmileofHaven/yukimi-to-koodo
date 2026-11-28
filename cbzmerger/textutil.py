"""Small text and number formatting helpers."""
from __future__ import annotations

import re
from xml.sax.saxutils import escape


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
    parts: list[str] = []
    run: list[float] = []

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
