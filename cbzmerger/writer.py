"""Writing the merged CBZ (full, streamed and incremental) and naming it."""
from __future__ import annotations

import contextlib
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from .constants import IMG_EXTS, MARKER
from .ui import Bar, info, paint, UI_LOCK, warn
from .textutil import attr, fmt, nat, safe_filename
from .chapters import find_cover


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


class IncrementalError(RuntimeError):
    """The previous output can't be reused; the caller falls back to a full rebuild."""


@dataclass
class ReusePlan:
    """Chapters (and maybe the cover) that can be copied from the previous output."""
    old_path: Path
    chapters: dict[str, tuple[int, int, int]]  # chapter file -> (old group, pages, image bytes)
    cover_entry: str | None = None  # entry name inside the old CBZ when the cover is reused
    cover_bytes: int = 0

    @property
    def total_bytes(self):
        return sum(v[2] for v in self.chapters.values()) + (self.cover_bytes if self.cover_entry else 0)


_OLD_ENTRY_RE = re.compile(r"^(\d{4})_(\d{5})\.[A-Za-z0-9]+$")


def old_page_groups(old):
    """{chapter group number: [ZipInfo, ...]} for the page entries of a CBZ we wrote earlier
    (entries are named GGGG_PPPPP.ext; group 0 is the cover)."""
    groups: dict[int, list] = {}
    for i in old.infolist():
        m = _OLD_ENTRY_RE.match(i.filename)
        if m:
            groups.setdefault(int(m.group(1)), []).append(i)
    for infos in groups.values():
        infos.sort(key=lambda i: nat(i.filename))
    return groups


def _open_old(stack, reuse):
    try:
        old = stack.enter_context(zipfile.ZipFile(reuse.old_path))
        return old, old_page_groups(old)
    except (zipfile.BadZipFile, OSError) as e:
        raise IncrementalError(f"previous output unreadable ({e})") from e


def _read_reused(old, groups, fname, rc):
    grp, npages, _nbytes = rc
    infos = groups.get(grp, [])
    if len(infos) != npages:
        raise IncrementalError(
            f"{fname}: previous output holds {len(infos)} page(s) for it, expected {npages}")
    try:
        return [(Path(i.filename).suffix.lower(), old.read(i)) for i in infos]  # CRC-checked on read
    except (zipfile.BadZipFile, OSError, RuntimeError) as e:
        raise IncrementalError(f"{fname}: {e}") from e


def _read_old_cover(old, entry):
    try:
        return old.read(entry)
    except (zipfile.BadZipFile, OSError, KeyError, RuntimeError) as e:
        raise IncrementalError(f"cover: {e}") from e


def merge(chapters, idx, series, cover, out_path, show_progress=True, reuse=None, layout=None):
    """Single-pass read and write of the merged CBZ. Validates each chapter inline so files are
    opened only once. With `reuse`, chapters already present in the previous output are copied
    from it instead of being read from disk. `layout` (a dict) is filled with where each chapter
    ended up, for the progress database. Returns (pages, chapters_written, skipped_names)."""
    pages: list[str] = []
    bookmarks: dict[int, str] = {}
    skipped: list[str] = []
    tmp = out_path.with_name(out_path.name + ".part")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    total = (sum(ch.size for ch in chapters)
             + (cover.stat().st_size if cover else 0)
             + (reuse.cover_bytes if reuse and reuse.cover_entry else 0))
    with UI_LOCK:
        bar = Bar(total, "Merging") if show_progress else None
    finished = False
    try:
        # the ExitStack closes the previous output before it is replaced below (matters on Windows)
        with contextlib.ExitStack() as stack:
            dst = stack.enter_context(zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED))
            old, old_groups = _open_old(stack, reuse) if reuse else (None, {})

            if reuse and reuse.cover_entry:
                blob = _read_old_cover(old, reuse.cover_entry)
                dst.writestr("0000_00000" + (Path(reuse.cover_entry).suffix.lower() or ".jpg"), blob)
                pages.append("FrontCover")
                if layout is not None:
                    layout["cover"] = reuse.cover_bytes
                if bar:
                    bar.advance(reuse.cover_bytes, "cover")
            elif cover:
                dst.write(cover, "0000_00000" + (cover.suffix.lower() or ".jpg"))
                pages.append("FrontCover")
                csz = cover.stat().st_size
                if layout is not None:
                    layout["cover"] = csz
                if bar:
                    bar.advance(csz, "cover")

            written = 0
            for ch in chapters:
                ch_start = bar.value if bar else 0
                if bar:
                    bar.set(ch_start, ch.name)
                rc = reuse.chapters.get(ch.path.name) if reuse else None
                if rc is not None:
                    data = _read_reused(old, old_groups, ch.path.name, rc)
                else:
                    try:
                        with zipfile.ZipFile(ch.path) as src:
                            infos = sorted((i for i in src.infolist() if _is_image(i.filename)),
                                           key=lambda i: nat(i.filename))
                            if not infos:
                                warn(f"chapter {fmt(ch.number)} ({ch.path.name}) has no images, skipped")
                                skipped.append(ch.path.name)
                                if bar:
                                    bar.set(ch_start + ch.size)
                                continue
                            img_total = sum(i.file_size for i in infos) or 1
                            data = []  # read whole chapter first so a bad one leaves no half-chapter
                            read_bytes = 0
                            for i in infos:
                                data.append((Path(i.filename).suffix.lower(), src.read(i)))
                                read_bytes += i.file_size
                                if bar:
                                    scaled = int(ch.size * (read_bytes / img_total))
                                    bar.set(ch_start + min(scaled, ch.size))
                    except (zipfile.BadZipFile, OSError) as e:
                        warn(f"chapter {fmt(ch.number)} ({ch.path.name}) unreadable ({e}), skipped")
                        skipped.append(ch.path.name)
                        if bar:
                            bar.set(ch_start + ch.size)
                        continue
                if bar:
                    bar.set(ch_start + ch.size)
                written += 1
                bookmarks[len(pages)] = ch.name
                for pi, (ext, blob) in enumerate(data, start=1):
                    dst.writestr(f"{written:04d}_{pi:05d}{ext}", blob)
                    pages.append("Story")
                if layout is not None:
                    layout.setdefault("groups", {})[ch.path.name] = (
                        written, len(data), sum(len(b) for _, b in data))

            if written == 0:
                raise RuntimeError("none of the selected chapters could be read")
            dst.writestr("ComicInfo.xml", build_comicinfo(idx, series, pages, bookmarks))
        tmp.replace(out_path)
        finished = True
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        if bar:
            bar.close() if finished else bar.abort()
    return len(pages), written, skipped


def merge_from_stream(stream_iter, chapters, idx, series, cover_name, out_path, total_bytes,
                      label="Streaming", reuse=None, layout=None):
    """Consume `(filename, raw_bytes)` items yielded from an `adb exec-out tar` stream
    and write the merged `.cbz` directly to `out_path` without touching temp disk storage.
    With `reuse`, chapters already in the previous output are copied from it and slotted into
    chapter order between the streamed ones (the stream carries only new/changed files)."""
    pages: list[str] = []
    bookmarks: dict[int, str] = {}
    skipped: list[str] = []
    tmp = out_path.with_name(out_path.name + ".part")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    by_name = {ch.path.name: ch for ch in chapters}
    seen_names: set[str] = set()
    bar = Bar(total_bytes + (reuse.total_bytes if reuse else 0), label)
    finished = False
    try:
        with contextlib.ExitStack() as stack:
            dst = stack.enter_context(zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED))
            old, old_groups = _open_old(stack, reuse) if reuse else (None, {})
            written = 0
            pos = 0  # next chapter (in order) not yet written

            def emit(ch, data):
                nonlocal written
                written += 1
                bookmarks[len(pages)] = ch.name
                for pi, (ext, img_blob) in enumerate(data, start=1):
                    dst.writestr(f"{written:04d}_{pi:05d}{ext}", img_blob)
                    pages.append("Story")
                if layout is not None:
                    layout.setdefault("groups", {})[ch.path.name] = (
                        written, len(data), sum(len(b) for _, b in data))

            def flush_reused(upto):
                """Write reused chapters that come before `upto` (None = all remaining)."""
                nonlocal pos
                while pos < len(chapters):
                    cur = chapters[pos]
                    if upto is not None and cur.path.name == upto:
                        return
                    pos += 1
                    rc = reuse.chapters.get(cur.path.name) if reuse else None
                    if rc is not None:
                        emit(cur, _read_reused(old, old_groups, cur.path.name, rc))
                        bar.advance(rc[2], cur.name)

            if reuse and reuse.cover_entry:
                blob = _read_old_cover(old, reuse.cover_entry)
                dst.writestr("0000_00000" + (Path(reuse.cover_entry).suffix.lower() or ".jpg"), blob)
                pages.append("FrontCover")
                if layout is not None:
                    layout["cover"] = reuse.cover_bytes
                bar.advance(reuse.cover_bytes, "cover")

            for fname, blob in stream_iter:
                seen_names.add(fname)
                if cover_name and fname == cover_name and not (reuse and reuse.cover_entry):
                    bar.advance(len(blob), "cover")
                    ext = Path(cover_name).suffix.lower() or ".jpg"
                    dst.writestr("0000_00000" + ext, blob)
                    pages.append("FrontCover")
                    if layout is not None:
                        layout["cover"] = len(blob)
                    continue

                ch = by_name.get(fname)
                if ch is None:
                    bar.advance(len(blob))
                    continue

                flush_reused(fname)
                pos += 1  # this chapter is handled now, whether or not it turns out readable
                bar.advance(len(blob), ch.name)
                try:
                    with zipfile.ZipFile(io.BytesIO(blob)) as src:
                        infos = sorted((i for i in src.infolist() if _is_image(i.filename)),
                                       key=lambda i: nat(i.filename))
                        if not infos:
                            warn(f"chapter {fmt(ch.number)} ({fname}) has no images, skipped")
                            skipped.append(fname)
                            continue
                        data = [(Path(i.filename).suffix.lower(), src.read(i)) for i in infos]
                except (zipfile.BadZipFile, OSError) as e:
                    warn(f"chapter {fmt(ch.number)} ({fname}) unreadable ({e}), skipped")
                    skipped.append(fname)
                    continue
                emit(ch, data)

            flush_reused(None)
            missing = [ch.path.name for ch in chapters
                       if ch.path.name not in seen_names
                       and not (reuse and ch.path.name in reuse.chapters)]
            if missing:
                raise RuntimeError(f"tar stream missing {len(missing)} chapter(s), e.g. {missing[0]}")
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


def plan_output_path(chapters, all_count, series, out_arg, default_dir, taken=None, tag=""):
    """Compute final output path (handling range suffix and duplicate title tags)."""
    if len(chapters) == all_count:
        suffix = ""
    elif len(chapters) == 1:
        suffix = f" (Ch {fmt(chapters[0].number)})"
    else:
        suffix = f" (Ch {fmt(chapters[0].number)}-{fmt(chapters[-1].number)})"
    out_path = resolve_output(out_arg, default_dir, safe_filename(series + suffix) + ".cbz")
    if taken is not None:
        if out_path in taken and tag:  # same title as a series already written in this run
            out_path = resolve_output(out_arg, default_dir, safe_filename(f"{series}{suffix} [{tag}]") + ".cbz")
            warn(f"another series already produced that filename; using {out_path.name}")
        taken.add(out_path)
    return out_path


def rename_for_skipped(out_path, chapters, skipped, all_count, series, out_arg, default_dir, taken, tag):
    """Chapters that turned out unreadable/empty are only found while merging, after the
    output was named. Re-derive the range suffix from what was really written."""
    if not skipped:
        return out_path
    gone = set(skipped)
    kept = [c for c in chapters if c.path.name not in gone]
    if not kept:
        return out_path
    new_path = plan_output_path(kept, all_count, series, out_arg, default_dir, None, tag)
    if new_path == out_path or (taken is not None and new_path in taken):
        return out_path
    out_path.replace(new_path)
    if taken is not None:
        taken.discard(out_path)
        taken.add(new_path)
    return new_path


def finish(selected, all_count, idx, series, cover_dir, out_arg, default_dir,
           taken=None, tag="", show_progress=True, plan=None, layout_out=None):
    """stat sizes -> name the output -> single-pass merge. Raises RuntimeError if nothing usable.
    With `plan`, chapters listed in it are copied from the previous output (no local file needed)."""
    usable = []
    for ch in selected:
        rc = plan.chapters.get(ch.path.name) if plan else None
        if rc is not None:
            ch.size = rc[2]  # image bytes, for the progress bar
            usable.append(ch)
            continue
        try:
            ch.size = ch.path.stat().st_size
            usable.append(ch)
        except OSError as e:
            warn(f"chapter {fmt(ch.number)} ({ch.path.name}) unreadable ({e}), skipped")
    if not usable:
        raise RuntimeError("none of the selected chapters could be read")

    out_path = plan_output_path(usable, all_count, series, out_arg, default_dir, taken, tag)
    usable = [c for c in usable if c.path.resolve() != out_path]  # never merge the output into itself
    if not usable:
        raise RuntimeError("nothing left to merge")
    cover = find_cover(cover_dir, idx)
    info(f"Merging {len(usable)} chapter(s) -> {paint(out_path, 'bold')}")
    pages, written, skipped = merge(usable, idx, series, cover, out_path,
                                    show_progress=show_progress, reuse=plan, layout=layout_out)
    out_path = rename_for_skipped(out_path, usable, skipped, all_count, series, out_arg,
                                  default_dir, taken, tag)
    return out_path, pages, written, skipped
