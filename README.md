# HuaEPUB

**Current version: 2.3.0**

Download Chinese web novels and build English EPUBs. Run from source on **Windows, macOS, or Linux** (Python 3.10+). Prebuilt executables are published for **Windows, macOS, and Linux**.

Formerly *Novel Downloader & Translator*. Based on WebToEpub extension (by dteviot) and fixTranslate.py.

<img width="898" height="729" alt="image" src="https://github.com/user-attachments/assets/1a40bb3c-a92b-4c7b-a210-7fd50562a887" />

## Features

- **Download novels** from supported sites (currently: twkan.com, 69shuba.com, uukanshu.cc)
- **Generic fallback parser** (experimental) — tries a best-effort download for any other novel site
- **Multi-download mode** — paste a block of novel URLs and download them sequentially with one click
- **Library mode** — track novels, pull only new chapters, rebuild full EPUBs (cache reuses old chapters)
- **Optional Google Drive sync** — sync library metadata and/or EPUBs across devices (offline-first; off by default)
- **Remove watermarks** and ads automatically
- **Translate to English** using Google Translate (free, concurrent) or a LibreTranslate server
- **Resume support** — downloaded chapters are cached, so re-runs and interrupted downloads skip what's already fetched
- **Translation cache** — previously translated text is reused across runs, costing zero API requests
- **Create EPUB** files ready for e-readers, with volume-grouped table of contents when chapter titles carry volume prefixes
- **Select specific chapters** to download, including quick range selection (e.g. 200-450)
- **Progress tracking** with ETA and cancel support; failed chapters are retried at the end of the run
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

## Usage

### Single Mode (default)

1. **Enter URL**: Paste the URL of the novel's main page
   - Example: `https://twkan.com/book/76222.html`

2. **Fetch Chapters**: Click "Fetch Chapters" to load the chapter list

3. **Select Chapters**: Check/uncheck chapters you want to download
   - Use "Select All", "Select None", or "Invert" for bulk selection
   - Or enter a range (e.g. 200 - 450) and click "Select Range"

4. **Options** (remembered between sessions):
   - ✅ Remove watermarks & ads - Cleans the content
   - ✅ Translate to English - Translates Chinese text
   - ✅ Use chapter cache (resume) - Reuses chapters downloaded in previous runs
   - Translator - Google (default) or LibreTranslate (server URL configurable in `settings.json`)
   - Translation Workers - Number of concurrent translation requests (default: 200)
   - Save to - Output folder (defaults to `~/.huaepub/books`)

5. **Download**: Click "Download EPUB" — saved automatically to the chosen folder

### Multi Mode

1. **Switch mode**: Click the "Multi" toggle at the top left
2. **Paste URLs**: Drop one or more novel URLs into the text box (one per line)
3. **Fetch All**: Click "Fetch All" to load info for all novels at once
4. **Download All**: Click "Download All" — each novel is processed sequentially (download → clean → translate → EPUB)
5. **Summary**: A single popup shows all results when everything is done

### Library Mode & Google Drive Sync (optional)

Library mode tracks novels you've downloaded so you can **Update** them for new chapters on any machine that shares the same library.

**Local data** (always used): `~/.huaepub/` (`settings.json`, `library.json`, `cache.db`, logs).

**Optional Drive sync** (Library tab):

1. Create a Google Cloud project, enable the **Google Drive API**, and create an OAuth client ID of type **Desktop app**
2. Download the client JSON and save it as:
   - Windows: `C:\Users\<you>\.huaepub\google_oauth_client.json`
   - macOS / Linux: `~/.huaepub/google_oauth_client.json`
3. In the app: open **Library** → enable **Sync with Google Drive** → **Connect** (browser login)
4. Choose what to sync (any combo):
   - **Sync library** — `library.json` (tracked novels + last-downloaded chapter cursors)
   - **Sync EPUBs** — files under Drive `books/`
Files are stored in a visible Drive folder (default **My Drive → HuaEPUB**, with `library.json` + `books/`). Use **Change folder** to pick another folder name or paste a Drive folder link, and **Open folder** to jump there.

Sync is offline-first: if Drive is unreachable, downloads and the local library still work. Use **Sync Now** to pull/merge/push on demand. After a download, the app pushes in the background when sync is enabled.

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
├── app.py              # Main GUI application
├── requirements.txt    # Python dependencies
├── build.py            # PyInstaller build script
├── core/
│   ├── branding.py     # Product name + legacy aliases
│   ├── parser.py       # Base parser class + registry
│   ├── cleaner.py      # Watermark/ad removal
│   ├── translator.py   # Translation (Google / LibreTranslate)
│   ├── epub_builder.py # EPUB creation
│   ├── settings.py     # Persistent app settings
│   ├── cache.py        # Chapter + translation caches (SQLite)
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

## Credits

- Based on [WebToEpub](https://github.com/dteviot/WebToEpub) browser extension
- Translation logic from fixTranslate.py
- Uses [ebooklib](https://github.com/aerkalov/ebooklib) for EPUB creation
- GUI built with [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter)

## License

MIT License - Feel free to modify and distribute.
