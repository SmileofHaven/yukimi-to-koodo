import json, re, sys, zipfile
from pathlib import Path
from xml.sax.saxutils import escape

folder = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
idx = json.loads((folder / "index.json").read_text(encoding="utf-8"))
out = folder.parent / f"{idx['title']}.cbz"
exts = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

def nat(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(s))]

chapters = sorted(idx["chapters"].values(), key=lambda c: c["number"])
pages, bookmarks = [], {}  # bookmarks: page index -> chapter name

with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as dst:
    cover = folder / idx.get("cover_entry", "cover.jpg")
    if cover.exists():
        dst.write(cover, "0000_00000" + cover.suffix.lower())
        pages.append("FrontCover")

    for ci, ch in enumerate(chapters, start=1):
        with zipfile.ZipFile(folder / ch["file"]) as src:
            names = sorted(
                (n for n in src.namelist() if Path(n).suffix.lower() in exts),
                key=nat,
            )
            bookmarks[len(pages)] = ch["name"]
            for pi, n in enumerate(names, start=1):
                dst.writestr(f"{ci:04d}_{pi:05d}{Path(n).suffix.lower()}", src.read(n))
                pages.append("Story")

    page_xml = "\n".join(
        f'    <Page Image="{i}" Type="{t}"'
        + (f' Bookmark="{escape(bookmarks[i])}"' if i in bookmarks else "")
        + " />"
        for i, t in enumerate(pages)
    )
    authors = ", ".join(a for a in idx.get("authors", []) if a)
    genres = ", ".join(t["title"] for t in idx.get("tags", []))
    age = "Adults Only 18+" if idx.get("nsfw") else "Everyone"
    alt = next((a for a in idx.get("alt_titles", []) if a), "")
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<ComicInfo>
  <Series>{escape(idx['title'])}</Series>
  <LocalizedSeries>{escape(alt)}</LocalizedSeries>
  <Summary>{escape(idx.get('description', ''))}</Summary>
  <Writer>{escape(authors)}</Writer>
  <Genre>{escape(genres)}</Genre>
  <Web>{escape(idx.get('public_url', ''))}</Web>
  <PageCount>{len(pages)}</PageCount>
  <AgeRating>{age}</AgeRating>
  <Manga>Yes</Manga>
  <Pages>
{page_xml}
  </Pages>
</ComicInfo>
"""
    dst.writestr("ComicInfo.xml", xml)

print("wrote", out, f"({len(pages)} pages, {len(chapters)} chapters)")