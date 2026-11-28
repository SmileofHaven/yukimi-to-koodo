"""Interactive prompts for the no-argument mode."""
from __future__ import annotations

from pathlib import Path

from .ui import ask, paint
from .textutil import compress
from .ranges import parse_ranges


def clean_path(s):
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
            p = clean_path(raw)
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
    p = clean_path(raw)
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
