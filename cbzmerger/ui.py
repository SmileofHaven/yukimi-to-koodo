"""Console UI: colors, status lines and a thread-safe progress bar."""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time


USE_COLOR = False


FANCY = False  # live redraw (\r) allowed


FILL, EMPTY = "#", "-"


_STYLES = {"red": "31", "green": "32", "yellow": "33", "blue": "34",
           "magenta": "35", "cyan": "36", "gray": "90", "bold": "1"}


UI_LOCK = threading.RLock()


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
    return f"{n:.1f} GB"


class Bar:
    """Single-line progress bar. Messages printed while it is active go through
    emit_line(), which clears the bar line first and redraws it afterwards."""

    active: "Bar | None" = None  # the bar currently allowed to draw (newest live one)
    _live: "list[Bar]" = []

    def __init__(self, total, label="", as_bytes=True):
        self.total = total or 1
        self.label = label
        self.as_bytes = as_bytes
        self.value = 0
        self.status = ""
        self._last_draw = 0.0
        self._last_step = -1
        with UI_LOCK:
            Bar._live.append(self)
            Bar.active = self
            self.draw(force=True)

    def set(self, value, status=None):
        with UI_LOCK:
            self.value = min(value, self.total)
            if status is not None:
                self.status = status
            self.draw()

    def advance(self, n, status=None):
        with UI_LOCK:
            self.set(self.value + n, status)

    def clear(self):
        with UI_LOCK:
            if FANCY:
                cols = shutil.get_terminal_size((80, 20)).columns
                sys.stdout.write("\r" + " " * (cols - 1) + "\r")
                sys.stdout.flush()

    def draw(self, force=False):
        with UI_LOCK:
            if FANCY and Bar.active is not self:
                return  # another bar owns the line (e.g. next series pulling while this one merges)
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
            plain = f"{self.label} [{FILL * filled}{EMPTY * (width - filled)}] {pct} {counts} {status}"
            pad = " " * max(0, cols - 1 - len(plain))
            line = (f"{paint(self.label, 'bold')} "
                    f"[{paint(FILL * filled, 'green')}{paint(EMPTY * (width - filled), 'gray')}] "
                    f"{paint(pct, 'cyan')} {counts} {paint(status, 'gray')}{pad}")
            sys.stdout.write("\r" + line)
            sys.stdout.flush()

    def close(self):
        with UI_LOCK:
            self.value = self.total
            self.draw(force=True)
            if FANCY and Bar.active is self:
                sys.stdout.write("\n")
                sys.stdout.flush()
            self._release()

    def abort(self):
        with UI_LOCK:
            if Bar.active is self:
                self.clear()
            self._release()

    def _release(self):
        """Drop this bar; the newest remaining bar takes the line back."""
        if self in Bar._live:
            Bar._live.remove(self)
        Bar.active = Bar._live[-1] if Bar._live else None
        if Bar.active is not None:
            Bar.active.draw(force=True)


def emit_line(msg, stream=None):
    stream = stream or sys.stdout
    with UI_LOCK:
        bar = Bar.active
        if bar:
            bar.clear()
        print(msg, file=stream, flush=True)
        if bar:
            bar.draw(force=True)


def info(msg):
    emit_line(f"{paint('::', 'cyan', 'bold')} {msg}")


def ok(msg):
    emit_line(f"{paint('OK', 'green', 'bold')} {msg}")


def warn(msg):
    emit_line(f"{paint('!', 'yellow', 'bold')} {paint(msg, 'yellow')}", sys.stderr)


def err(msg):
    emit_line(f"{paint('error:', 'red', 'bold')} {msg}", sys.stderr)


def ask(prompt):
    return input(paint(prompt, "cyan") + " ")


def warn_plain(line):
    emit_line(paint(line, "yellow"), sys.stderr)
