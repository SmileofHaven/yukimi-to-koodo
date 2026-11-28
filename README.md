# yukimi-to-koodo

Merge a series of chapter-wise `.cbz` files (e.g. from Yukimi) into one `.cbz`
with a `ComicInfo.xml` and per-chapter bookmarks, so Koodo Reader shows a proper
chapter list. Pure Python 3.8+, no dependencies.

## Batch: pull straight from your phone (adb)

```
python cbz_merger.py --batch -o ./out [-r RANGE] [--series NAME] [-m MODE]
```

1. Runs `adb devices`. No device? It tells you to enable Developer options, turn on
   USB debugging, connect the phone, allow the prompt, and try again.
2. Lists the series in `/sdcard/Android/data/org.koitharu.kotatsu.nightly/files/manga`
   (change with `--remote-path`).
3. For each series it pulls `index.json` first, works out the chapters, and only
   pulls the chapters inside your range (plus the cover), merges them, and deletes
   the staged files.

Pulling and merging are pipelined: the next series downloads while the current one is merged.
Ranged pulls use parallel adb streams (`--workers N`, default 4). `--fast` instead streams a tar
over `adb exec-out` straight into the output CBZ with no staging files, and falls back to the
normal pull for any series where that fails.

### Progress database (skip finished work, resume interrupted runs)

Batch mode keeps a small SQLite file, `state.db`, next to the auto-installed adb
(`--db PATH` to move it).

- **Skip unchanged:** each series is fingerprinted from the chapter files it would use
  (name, size, modified time) plus the metadata written into `ComicInfo.xml`. If it matches a
  finished job and the output CBZ still exists at the recorded size and location, the series is
  skipped with no transfer (only the tiny `index.json` is read). A different range or output
  folder is a separate job.
- **Resume:** pulled files are staged in a persistent per-device folder and recorded with
  their size and modified time. After a crash, unplugged cable or Ctrl+C, the next run reuses
  every file that is still valid and pulls only the rest; a file that changed on the phone is
  pulled again. Staging is deleted only after a successful merge.
- **Incremental:** when a series changed, only the new or changed chapters are pulled. The
  database also records which chapter file became which pages of each output, so every chapter
  whose source file is unchanged (same name, size and modified time) is copied straight out of
  your existing CBZ on local disk. The new CBZ is assembled in chapter order, so a chapter
  inserted in the middle (say 20.5) works as well as one appended at the end, and bookmarks and
  page indices are regenerated. The same applies to removed or re-uploaded chapters and a
  changed cover, and it works with `--fast` (only the new chapters are streamed). If the old
  CBZ is missing, was modified, or fails its checksums, it falls back to a full rebuild
  automatically. When a range job's name changes (e.g. `(Ch 5-18)` becomes `(Ch 5-19)`), the
  superseded output in the same folder is removed.
- **Safe by design:** a series with a missing, unreadable or unpullable chapter is never
  recorded as done, so it is retried. If the database can't be opened it warns and carries on
  without it. `--force` ignores recorded progress; `--no-db` turns the feature off.
- `--fast` skips unchanged series and builds incrementally too, but an interrupted stream restarts
  that series (there is nothing on disk to resume from).

`--series` filters by name (case-insensitive substring, repeatable), `--serial` picks a
device when several are plugged in, `--temp` / `--keep-temp` control staging.
If `adb` isn't found (PATH, `ANDROID_HOME`, or a previous install), it asks whether to
download Google's platform-tools and unpack them to a private folder
(`%LOCALAPPDATA%\yukimi-to-koodo` on Windows, `~/.local/share/yukimi-to-koodo` elsewhere).
Your PATH is not changed, and later runs find it automatically. `--install-adb` skips the question. Also available as option 2 in the interactive menu.

Output is colored with progress bars. Disable with `--no-color` or the `NO_COLOR` env var.

## Project layout

`cbz_merger.py` is a thin launcher (so `python cbz_merger.py ...` keeps working);
`python -m cbzmerger ...` does the same. The code lives in the `cbzmerger/` package,
split so that each file has one job and imports only from the layers below it:

| Module | Contents |
|---|---|
| `constants`, `textutil`, `ranges` | shared constants, formatting helpers, chapter range parsing |
| `ui` | colors, status lines, thread-safe progress bar |
| `chapters` | finding chapters from `index.json` or filenames |
| `writer` | building the merged CBZ (full, streamed, incremental) and naming it |
| `adb`, `adb_setup` | adb wrapper (list/pull/stream); locating, installing and connecting to adb |
| `state` | SQLite progress database |
| `prompts`, `local` | interactive prompts; merging a local folder |
| `jobs` | per-series batch planning: discovery, skip / incremental decisions, bookkeeping |
| `transfer` | getting one series off the phone (staged pull, tar streaming, merge hand-off) |
| `batch` | the batch driver (pipelined pull + merge) |
| `cli` | argument parsing and the entry point |

Keep `cbz_merger.py` next to the `cbzmerger/` folder if you copy it elsewhere.

## Interactive

Run with no arguments and it asks for everything:

```
python cbz_merger.py
```

1. Input folder, output folder (type a path, or `?` for a folder picker)
2. If there's no `index.json`: pick one manually (file picker) or use filename detection
3. Lists the available chapters, then asks for a range (Enter = all)

## CLI

```
python cbz_merger.py INPUT [-o OUT] [-m auto|index|filename] [-r RANGE] [--index FILE]
```

| Option | Meaning |
|---|---|
| `INPUT` | folder containing the chapter CBZs (and `index.json`); default `.` |
| `-o, --output` | output folder, or a full path ending in `.cbz`; default: parent of `INPUT` |
| `-m, --mode` | `auto` (default): `index.json` if present, else filenames. `index`: require index metadata. `filename`: ignore the index and parse chapter numbers from filenames |
| `-r, --range` | chapters to include: `7-20`, `7-`, `-20`, `5`, `1-5,8,10-` (default: all) |
| `--index` | use this `index.json` instead of `INPUT/index.json` |

Missing chapter files, unreadable/corrupt CBZs and empty chapters are skipped
with a warning. A partial range adds a suffix to the output name, e.g.
`Title (Ch 7-20).cbz`.

Filename detection understands `Chapter 7`, `ch_007`, `Ch.7.5`, `#7`, `Vol1 - 007`, or a bare `007`.
