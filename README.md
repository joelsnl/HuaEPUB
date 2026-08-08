# HuaEPUB

**Current version: 2.6.2**

Download Chinese web novels and build English EPUBs. Run from source on **Windows, macOS, or Linux** (Python 3.10+). Prebuilt executables are published for **Windows, macOS, and Linux**.

GUI is **PySide6 (Qt)** (CustomTkinter was replaced in 2.6.0 for smoother window move/resize and stabler threading). Formerly *Novel Downloader & Translator*. Based on WebToEpub extension (by dteviot) and fixTranslate.py.

<img width="898" height="729" alt="image" src="https://github.com/user-attachments/assets/1a40bb3c-a92b-4c7b-a210-7fd50562a887" />

## Features

- **Download novels** from supported sites (currently: twkan.com, 69shuba.com, uukanshu.cc)
- **Generic fallback parser** (experimental) — tries a best-effort download for any other novel site
- **Multi-download mode** — paste a block of novel URLs and download them sequentially with one click
- **Library mode** — cover-grid or list shelf, track novels, pull only new chapters, rebuild full EPUBs (local cover/TOC caches; Drive syncs library.json + EPUBs only)
- **Pause / Resume** — pause a long download, or close the app / shut down the PC and resume later from a banner on startup (local only; not synced to Drive)
- **Optional Google Drive sync** — sync library metadata and/or EPUBs across devices (offline-first; off by default)
- **Remove watermarks** and ads automatically
- **Translate to English** using Google Translate (free, concurrent) or a LibreTranslate server
- **Chapter cache** — downloaded chapters are stored locally so re-runs and resumes skip network fetches
- **Translation cache** — previously translated text is reused across runs, costing zero API requests
- **Create EPUB** files ready for e-readers, with volume-grouped table of contents when chapter titles carry volume prefixes
- **Select specific chapters** to download, including quick range selection (e.g. 200-450)
- **Progress tracking** with ETA, Pause, and Cancel; failed chapters are retried at the end of the run
- **Custom output folder** and persistent settings in `~/.huaepub/` (migrates from `~/.noveldownloader/` if present)
- **Auto-updater** — downloads prebuilt, checksum-verified release builds when available
- **Log file** (`~/.huaepub/logs/huaepub.log`) for diagnosing issues

## Installation

### Option 1: Run from Source (any OS)

Works on Windows, macOS, and Linux.

1. Install Python 3.10 or newer
2. Clone/download this folder
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Run the app:
   ```bash
   python app.py
   ```

### Option 2: Prebuilt Executables (Windows, macOS & Linux)

Download the latest release from the [Releases](https://github.com/joelsnl/novelDownloader/releases) page:
- `HuaEPUB-windows.zip`
- `HuaEPUB-macos.zip`
- `HuaEPUB-linux.zip`

Each zip includes `HuaEPUB` plus a legacy `NovelDownloader` binary (same build) so older in-app updaters still work. Each release also includes `SHA256SUMS.txt`.

### Option 3: Build Standalone Executable Yourself

1. Install dependencies + PyInstaller:
   ```bash
   pip install -r requirements.txt
   pip install pyinstaller
   ```
2. Build:
   ```bash
   python build.py
   ```
3. Find the executable in `dist/`:
   - **Windows:** `HuaEPUB.exe`
   - **macOS / Linux:** `HuaEPUB`

## User Manual

HuaEPUB has three tabs at the top: **Single**, **Multi**, and **Library**. Options below the main area (translate, clean, cache, workers, save folder) apply to all modes and are remembered between sessions.

### Quick start (one novel)

1. Stay on **Single**.
2. Paste the novel’s **table of contents** URL (the main book page, not a single chapter).
3. Click **Fetch Chapters**.
4. Select the chapters you want (or leave all selected).
5. Click **Download EPUB**.
6. When it finishes, the EPUB is in your Save folder (default `~/.huaepub/books`).

### Options (what they mean)

| Option | What it does |
|--------|----------------|
| Remove watermarks & ads | Cleans site junk from chapter HTML |
| Translate to English | Machine-translates text while building the EPUB |
| Use chapter cache (resume) | Reuses chapters already saved on this PC |
| Watch clipboard for URLs | When on, copied novel URLs are queued into Multi (and fill Single if empty) |
| Translator | Google (default) or LibreTranslate |
| Translation Workers | Concurrent translate requests (default 200); use the **−** / **+** buttons |
| Save to | Where EPUB files are written |

Keep **Use chapter cache** on unless you intentionally want a full re-download.

### Pause, cancel, and resume after shutdown

Long downloads can take hours. You do not have to leave the PC on the whole time.

- **Pause** — stops between chapters. Click **Resume** to continue in the same session.
- **Close the app** or shut down while a download is running / paused — progress is kept locally (`cache.db` + `active_download.json` under `~/.huaepub/`).
- **Next launch** — a banner appears: **Resume** or **Discard**. Resume continues from cached chapters.
- **Cancel** — aborts the current run and clears the resume point (cached chapter text stays on disk for a later re-download).

Resume data is **local only**. Google Drive sync never uploads the chapter cache or the resume file.

### Single mode (details)

1. Paste the book URL → **Fetch Chapters**.
2. Select chapters with Select All / None / Invert, or a **Range** (e.g. from `200` to `450`).
3. **Download EPUB** — progress shows chapter fetch, then EPUB build/translate.
4. Use **Recent** to reopen a URL from earlier downloads.

### Multi mode

1. Open the **Multi** tab.
2. Paste several book URLs (one per line, or a block of text containing URLs).
3. **Fetch All**, then **Download All**.
4. Novels run one after another. You can Pause / Cancel / resume the queue the same way as Single.

### Library mode

After you download a novel, it appears in **Library** so you can update it later.

1. Open the **Library** tab.
2. Choose **Grid** (covers) or **List** (compact table).
3. Click **Check updates** — each cover shows status under the title (`Checking…`, `N new`, `Up to date`, or an error).
4. Filter **All** or **Updates** (novels that have new chapters).
5. Select a novel → **Update** (rebuilds a full EPUB; old chapters come from cache).
6. Or use **Update All** when several books have new chapters.
7. **Open URL** / **Download EPUB** / **Remove** as needed (**Remove** drops library tracking only; files/cache stay unless you delete them yourself).

The cover grid reflows when you resize the window and scrolls when there are more novels than fit on screen.

### Google Drive sync (optional)

Use this only if you want the same library list (and optionally EPUBs) on more than one PC.

**Always local:** `~/.huaepub/` (`settings.json`, `library.json`, `cache.db`, covers, resume job, logs).

**Drive can sync:** `library.json` and/or EPUB files — not the chapter cache, not pause/resume state.

1. Create a Google Cloud project, enable **Google Drive API**, create an OAuth client (**Desktop app**).
2. Save the client JSON as:
   - Windows: `C:\Users\<you>\.huaepub\google_oauth_client.json`
   - macOS / Linux: `~/.huaepub/google_oauth_client.json`
3. In the app: **Library** → Google Drive panel → enable sync → **Connect** (browser login).
4. Choose **Sync library** and/or **Sync EPUBs**.
5. Files go to a visible Drive folder (default **My Drive → HuaEPUB**). Use **Change folder** / **Open folder** / **Sync Now** as needed. Progress appears in the status bar while syncing.

If Drive is offline, downloads and the local library still work.

**Second device shows an empty library after Connect:**

1. Use the **same** `google_oauth_client.json` (same Google Cloud Desktop client) on every device — Drive’s `drive.file` scope only lets HuaEPUB manage folders **this app created**. A folder you made by hand (or with a different OAuth client) can look selectable but Sync will not read/write `library.json` / `books/` inside it.
2. On the PC that already has novels: **Library → Open folder** and confirm `library.json` + `books/` are inside that Drive folder.
3. On the new device: **Change folder** → paste that folder’s URL → the app checks list access and then syncs. You should see `library.json novels: N` in the confirmation.
4. Status should look like `Synced “HuaEPUB”: library (N novel(s))`. If N is 0 or you get an access error, fix the OAuth client / folder — Sync will no longer silently invent a second empty HuaEPUB folder.
5. EPUB sync uploads local files that are missing on Drive; it does not wipe or re-upload every book on every sync. On a new Mac, use **Download EPUB** per novel (or copy `books/` once) after the library list appears.

### Where files live

| Path | Contents |
|------|----------|
| `~/.huaepub/books/` | Default EPUB output |
| `~/.huaepub/library.json` | Tracked library + recent history |
| `~/.huaepub/cache.db` | Chapter HTML, translations, covers, TOCs |
| `~/.huaepub/active_download.json` | Incomplete download resume point (if any) |
| `~/.huaepub/settings.json` | App options |
| `~/.huaepub/logs/huaepub.log` | Diagnostics |

On Windows, `~` is your user folder (e.g. `C:\Users\YourName`).

### Tips

- Prefer the **main book page** URL from a supported site.
- For overnight runs: start the download, **Pause** or just close the app when you need the PC off, reopen later → **Resume**.
- If translation is rate-limited, lower **Translation Workers** (e.g. 30–50) and keep cache on.
- Menu **File** → Open books / data / log folder is handy when hunting files.

## Supported Sites

| Site | URL Pattern | Status |
|------|-------------|--------|
| twkan.com | `https://twkan.com/book/{id}.html` | ✅ Working |
| 69shuba.com | `https://69shuba.com/book/{id}/` | ✅ Working |
| uukanshu.cc | `https://uukanshu.cc/book/{id}/` | ✅ Working |
| Other sites | any novel table-of-contents URL | 🧪 Experimental (generic parser) |

## Adding New Sites

To add support for a new site, create a new parser in `parsers/`:

```python
# parsers/newsite.py
from core.parser import BaseParser, Chapter, NovelInfo, register_parser

@register_parser
class NewSiteParser(BaseParser):
    SITE_NAME = "newsite.com"
    SITE_DOMAINS = ["newsite.com", "www.newsite.com"]
    
    def get_novel_info(self, url: str) -> NovelInfo:
        # Extract title, author, cover, etc.
        pass
    
    def get_chapter_list(self, url: str) -> List[Chapter]:
        # Return list of chapters
        pass
    
    def get_chapter_content(self, chapter: Chapter) -> str:
        # Fetch and return chapter HTML content
        pass
```

Then import it in `parsers/__init__.py` **before** the generic parser (registration order matters — the generic parser matches any URL and must stay last):
```python
from parsers.newsite import NewSiteParser
```

## Running Tests

The test suite is fully offline (HTML fixtures, no network needed):

```bash
pip install pytest
python -m pytest tests/
```

## Project Structure

```
.
├── app.py              # Entry → gui.app.run()
├── gui/                # PySide6 UI (main window, pages, workers)
├── requirements.txt    # Python dependencies
├── build.py            # PyInstaller build script
├── core/
│   ├── branding.py     # Product name + legacy aliases
│   ├── parser.py       # Base parser class + registry
│   ├── cleaner.py      # Watermark/ad removal
│   ├── translator.py   # Translation (Google / LibreTranslate)
│   ├── epub_builder.py # EPUB creation
│   ├── download_runner.py  # Pause/cancel/chapter download (UI-agnostic)
│   ├── settings.py     # Persistent app settings
│   ├── cache.py        # Chapter + translation + cover caches (SQLite)
│   ├── download_job.py # Local incomplete-download resume (not Drive)
│   ├── library.py      # Library + history store
│   ├── drive_sync.py   # Optional Google Drive sync
│   ├── logger.py       # Log-to-file setup
│   └── updater.py      # Auto-updater
├── parsers/
│   ├── twkan.py        # twkan.com parser
│   ├── shuba69.py      # 69shuba.com parser
│   ├── uukanshu.py     # uukanshu.cc parser
│   └── generic.py      # Fallback parser for other sites
└── tests/              # Offline pytest suite
```

## Troubleshooting

### "Translation failed" errors
- Reduce workers (try 20-30 instead of 50)
- Google may rate-limit; the app will retry with backoff
- If some segments persistently fail to translate, the app gives up after a
  few retry passes, keeps the best available text, and builds the EPUB anyway
  instead of getting stuck

### "Could not extract book ID"
- Make sure you're using the main novel page URL
- Check that the site is supported

### EPUB won't open
- Try a different reader (Calibre recommended)
- Check if the novel has special characters in the title

### Resume banner does not appear after closing mid-download
- Confirm the download had started (chapters were being fetched)
- Check that `~/.huaepub/active_download.json` exists; if you clicked **Cancel**, the resume point was cleared on purpose
- Cached chapters still help if you fetch the same book again with cache enabled

## Credits

- Based on [WebToEpub](https://github.com/dteviot/WebToEpub) browser extension
- Translation logic from fixTranslate.py
- Uses [ebooklib](https://github.com/aerkalov/ebooklib) for EPUB creation
- GUI built with [PySide6](https://doc.qt.io/qtforpython/) (Qt)

## Disclaimer

HuaEPUB is a personal utility for fetching and packaging web novel pages that are already publicly reachable in a browser. It does **not** grant you any rights to the novels themselves.

- Many novels on aggregator / mirror sites are uploaded **without the copyright holder's permission**. Downloading, copying, translating, or redistributing that material may violate copyright law in your country.
- You are solely responsible for how you use this tool and for complying with applicable laws, site terms of service, and the rights of authors, publishers, and platforms.
- Automatic translation does **not** create a legal license to keep or share the work. Machine-translated EPUBs are still derived from the original copyrighted text.
- This project is **not affiliated with**, endorsed by, or connected to twkan, 69shuba, uukanshu, Google, Google Play Books, or any novel publisher.
- The software is provided **as is**, without warranty. The authors are not liable for misuse, account bans, takedown notices, or legal claims arising from your use of it.

If you enjoy a novel, support the author through official channels whenever possible.

## License

MIT License - Feel free to modify and distribute.
