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

`--series` filters by name (case-insensitive substring, repeatable), `--serial` picks a
device when several are plugged in, `--temp` / `--keep-temp` control staging.
If `adb` isn't found (PATH, `ANDROID_HOME`, or a previous install), it asks whether to
download Google's platform-tools and unpack them to a private folder
(`%LOCALAPPDATA%\yukimi-to-koodo` on Windows, `~/.local/share/yukimi-to-koodo` elsewhere).
Your PATH is not changed, and later runs find it automatically. `--install-adb` skips the question. Also available as option 2 in the interactive menu.

Output is colored with progress bars. Disable with `--no-color` or the `NO_COLOR` env var.

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