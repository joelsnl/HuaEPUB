# HuaEPUB

**Current version: 2.10.1**

Download Chinese web novels and build English EPUBs. Run from source on **Windows, macOS, or Linux** (Python 3.10+). Prebuilt executables are published for **Windows, macOS, and Linux**.

GUI is **PySide6 (Qt)**. Formerly *Novel Downloader & Translator* (CustomTkinter through 2.5).

<img width="1838" height="1124" alt="Screenshot 2026-08-08 at 16 30 03" src="https://github.com/user-attachments/assets/6ffa080a-5ba0-4bde-a506-e52de3a90fd5" />


## Features

- **Download novels** from hosts listed in `parsers/sites.json` (twkan, 69shuba, uukanshu, and hundreds of others)
- **Generic fallback parser** (experimental) — tries a best-effort download for any other novel site; if a configured site’s content selector misses, the same heuristic is used and the completion dialog warns you
- **Multi-download mode** — paste a block of novel URLs and download them sequentially with one click
- **Library mode** — cover-grid or list shelf, track novels, pull only new chapters, rebuild full EPUBs (local cover/TOC caches; Drive syncs library.json + EPUBs only)
- **Pause / Resume** — pause a long download, or close the app / shut down the PC and resume later from a banner on startup (local only; not synced to Drive)
- **Optional Google Drive sync** — sync library metadata and/or EPUBs across devices (offline-first; off by default). After a successful download or library update, a silent sync is queued if Drive is enabled (it does not switch you to the Library tab)
- **Remove watermarks** and ads automatically
- **Translate to English** using Google Translate (free, concurrent), a LibreTranslate server, or local **Ollama**
- **Polish English** — after Google or LibreTranslate, a fast local copy-edit (llama.cpp + Qwen). Only awkward MTL spans are rewritten; fluent sentences are copied. Ollama is not required.
- **Chapter + translation cache** — stored in `~/.huaepub/cache.db` so re-runs and resumes skip network fetches. Default size cap is **2 GB** (Help → **Cache…**); oldest stored chapter HTML is deleted first. Nothing is cleared on a timer.
- **Create EPUB** files ready for e-readers, with volume-grouped table of contents when chapter titles carry volume prefixes. EPUBs are written to a sibling `.tmp` then replaced so a crash cannot leave a half-written file.
- **Select specific chapters** to download, including quick range selection (e.g. 200-450)
- **Progress tracking** with ETA (network/uncached work only — cached chapters do not fake “ETA 0s”), Pause, and Cancel. Failed chapters are retried at the end of the run. **Download EPUB** is disabled while a job is already running.
- **Completion notes** — leftover Chinese, heuristic chapter guesses, or a cancelled polish pass show as “Saved with warnings”
- **Custom output folder** and persistent settings in `~/.huaepub/` (migrates from `~/.noveldownloader/` if present). `settings.json` is written atomically.
- **Auto-updater** — downloads prebuilt release builds, verifies `SHA256SUMS.txt`, then a helper replaces the binary and **reopens the app on every OS** (Windows, macOS, Linux, and source installs)
- **Log file** (`~/.huaepub/logs/huaepub.log`) — File → **Open log file**

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

Download the latest release from the [Releases](https://github.com/joelsnl/HuaEPUB/releases) page:
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
| Translator | Google (default), LibreTranslate, or local **Ollama** |
| Polish English | After Google/LibreTranslate, local copy-edit on this PC. First run downloads llama.cpp + a Qwen GGUF that fits this GPU (`~/.huaepub/polish`). Greyed out if Translate is off or Translator is already Ollama. |
| Translation Workers | Concurrent Google/LibreTranslate requests (default 200). Ollama auto-drops to 2. Polish does not use this number. |
| Ollama model / URL | Shown only when Translator is Ollama (full local translation). If you have no models, HuaEPUB asks to download `qwen2.5:3b` (~2 GB). URL must be localhost. |
| Save to | Where EPUB files are written |
| Cache size | Help → **Cache…** — default 2 GB; `0` / Unlimited keeps everything. Oldest chapter HTML is deleted first; translations are kept unless the file is still over the cap. |

Keep **Use chapter cache** on unless you intentionally want a full re-download.

### Polish English (local copy-edit)

Keep **Translator** on **Google** (or LibreTranslate) and tick **Polish English**. Google still does the translation; a local LLM then copy-edits awkward English in the same EPUB.

- **Ollama is not required.** The first polish run downloads [llama.cpp](https://github.com/ggml-org/llama.cpp) (`llama-server`) and a **Qwen2.5 Instruct** GGUF into `~/.huaepub/polish`. Later runs reuse that.
- Model size follows this PC: roughly **3B** on CPU / low VRAM, **7B** on mid-range GPUs, **14B** when there is enough VRAM (NVIDIA CUDA, AMD Vulkan, or Apple Silicon). First download is a few GB.
- Only dirty MTL **spans** go to the GPU. Fluent Google sentences, titles, and Chinese leftovers are copied as-is. Polished spans are reused from the translations table in `cache.db`.
- If llama.cpp is already listening on `:8080` (or vLLM on `:8000`), that server is used instead of starting a new one.
- Progress and errors go to `~/.huaepub/logs/huaepub.log` (File → **Open log file**). Help → **How translation works** summarizes this in the app. Cancel during polish still **saves** the EPUB with Google/LibreTranslate English (already-polished sentences are kept).
- If Ollama is occupying the GPU so llama.cpp cannot start, polish can fall back to Ollama. Quit Ollama from the tray icon if you want the llama.cpp path.

### Local translation with Ollama

Ollama is only for **full** on-PC translation (no Google). It is much slower than Google + Polish. Install from [ollama.com](https://ollama.com). The suggested model is **`qwen2.5:3b`**: Apache-2.0, strong Chinese→English, about 2 GB, usable on CPU. Untagged `qwen2.5` can pull a much larger 7B+ build.

```bash
ollama list            # what you already have
# or let HuaEPUB ask to download qwen2.5:3b when you pick Ollama
```

In HuaEPUB set **Translator** to **Ollama**. Use 1–4 workers. Polish is greyed out in this mode (the model is already rewriting the whole book). Translations are cached.

### Pause, cancel, and resume after shutdown

Long downloads can take hours. You do not have to leave the PC on the whole time.

- **Pause** — stops between chapters. Click **Resume** to continue in the same session. Safe to close the app while paused.
- **Close the app** or shut down while a download is running / paused — progress is kept locally (`cache.db` + `active_download.json` under `~/.huaepub/`).
- **Next launch** — a banner appears: **Resume** or **Discard**. Resume continues from cached chapters.
- **Cancel** — clears the resume point (cached chapter text stays on disk). What gets written depends on the phase:
  - During chapter fetch or Chinese→English translation: the run **aborts** and **no EPUB** is written (a half-translated book is never saved).
  - During polish: the EPUB **is** saved with machine translation (already-polished sentences are kept). The completion dialog says so.

**Download EPUB** stays disabled while a job is running so a second run cannot start on top of the first.

Resume data is **local only**. Google Drive sync never uploads the chapter cache or the resume file.

### Single mode (details)

1. Paste the book URL → **Fetch Chapters**.
2. Select chapters with Select All / None / Invert, or a **Range** (e.g. from `200` to `450`).
3. **Download EPUB** — progress shows chapter fetch, then EPUB build/translate. The button is disabled while a job is running.
4. Use **Recent** to reopen a URL from earlier downloads.
5. If anything looks off (leftover Chinese, a generic content guess, or polish stopped early), the dialog title is **Saved with warnings**.

### Multi mode

1. Open the **Multi** tab.
2. Paste several book URLs (one per line, or a block of text containing URLs).
3. **Fetch All**, then **Download All**.
4. Novels run one after another. You can Pause / Cancel / resume the queue the same way as Single.

### Library mode

After you download a novel, it appears in **Library** so you can update it later.

1. Open the **Library** tab.
2. Choose **Grid** (covers) or **List** (compact table).
3. Click **Check updates** — each cover shows status under the title (`Checking…`, `N new`, `Up to date`, or an error), and thumbnails are refreshed from the site.
4. Filter **All** or **Updates** (novels that have new chapters).
5. Select a novel → **Update** (rebuilds a full EPUB; old chapters come from cache). ETA is based only on chapters that still need a network fetch, so a 500-chapter update with 3 new chapters will not show “ETA 0s”.
6. Or use **Update All** when several books have new chapters.
7. **Open URL** / **Download EPUB** / **Remove** as needed. **Remove** deletes the local EPUB, this novel’s chapter/cover/TOC cache, and the Drive copy (`library.json` + EPUB) so sync cannot restore it. Shared translation cache is kept.

The cover grid reflows when you resize the window and scrolls when there are more novels than fit on screen.

### Google Drive sync (optional)

Use this only if you want the same library list (and optionally EPUBs) on more than one PC.

**Always local:** `~/.huaepub/` (`settings.json`, `library.json`, `cache.db`, covers, resume job, logs, polish models).

**Drive can sync:** `library.json` and/or EPUB files — not the chapter cache, not pause/resume state, not `polish/`.

1. Create a Google Cloud project, enable **Google Drive API**, create an OAuth client (**Desktop app**).
2. Save the client JSON as:
   - Windows: `C:\Users\<you>\.huaepub\google_oauth_client.json`
   - macOS / Linux: `~/.huaepub/google_oauth_client.json`
3. In the app: **Library** → Google Drive panel → enable sync → **Connect** (browser login).
4. Choose **Sync library** and/or **Sync EPUBs**.
5. Files go to a visible Drive folder (default **My Drive → HuaEPUB**). Use **Change folder** / **Open folder** / **Sync Now** (or Library → **Sync Drive now**) as needed. Progress appears in the status bar while syncing.
6. After a successful Single / Multi / Library Update / Update All, HuaEPUB queues a **silent** Drive sync if sync is enabled. It does not switch you to the Library tab. Startup also runs a silent sync when Drive is already connected.

If Drive is offline, downloads and the local library still work.

**Second device shows an empty library after Connect:**

1. Use the **same** `google_oauth_client.json` (same Google Cloud Desktop client) on every device — Drive’s `drive.file` scope only lets HuaEPUB manage folders **this app created**. A folder you made by hand (or with a different OAuth client) can look selectable but Sync will not read/write `library.json` / `books/` inside it.
2. On the PC that already has novels: **Library → Open folder** and confirm `library.json` + `books/` are inside that Drive folder.
3. On the new device: **Change folder** → paste that folder’s URL → the app checks list access and then syncs. You should see `library.json novels: N` in the confirmation.
4. Status should look like `Synced “HuaEPUB”: library (N novel(s))`. If N is 0 or you get an access error, fix the OAuth client / folder — Sync will no longer silently invent a second empty HuaEPUB folder.
5. EPUB sync uploads missing books and **overwrites Drive copies when your local EPUB is newer or a different size** (e.g. after a library update adds chapters). It will not overwrite a Drive file that is clearly newer than your local copy. On a new Mac, use **Download EPUB** per novel (or copy `books/` once) after the library list appears if the files are not on that machine yet.

### Where files live

| Path | Contents |
|------|----------|
| `~/.huaepub/books/` | Default EPUB output |
| `~/.huaepub/library.json` | Tracked library + recent history |
| `~/.huaepub/cache.db` | Chapter HTML, translations (including polished spans), covers, TOC snapshots. Local only; default 2 GB cap via Help → Cache… |
| `~/.huaepub/polish/` | llama.cpp + Qwen GGUF for Polish English (first-run download; never Drive-synced) |
| `~/.huaepub/active_download.json` | Incomplete download resume point (if any; never Drive-synced) |
| `~/.huaepub/settings.json` | App options (atomic tmp+replace writes) |
| `~/.huaepub/google_oauth_client.json` | Desktop OAuth client (you copy this in; keep private) |
| `~/.huaepub/google_token.json` | Drive refresh token (owner-only when the OS allows) |
| `~/.huaepub/logs/huaepub.log` | Diagnostics |

On Windows, `~` is your user folder (e.g. `C:\Users\YourName`).

### Auto-update & trust

- Updates install only when the downloaded zip matches the release’s `SHA256SUMS.txt` (fail closed if the sum is missing or wrong). There is no fallback to unsigned `main.zip`.
- Checksums and zips come from the **same** GitHub release — treat the [joelsnl/HuaEPUB](https://github.com/joelsnl/HuaEPUB) publisher account as your trust root (enable 2FA on that account).
- On **every OS** (Windows, macOS, Linux, and source installs), confirm the “update ready” dialog. The app **quits** so a small helper can replace the files and **reopen** HuaEPUB. Frozen Windows may swap the on-disk `.exe` first, then a hidden PowerShell helper deletes the backup and relaunches after exit. Frozen macOS/Linux use a shell helper (`/bin/sh` or `/bin/bash`), never the app binary itself.
- Need **2.10.1+** for a correct reopen of the **built** app. 2.9.2–2.10.0 could relaunch via Terminal/`python` on macOS (closing that session quit the app) and often failed to reopen on Windows. Older 2.6–2.9.1 builds often left the window closed after an update.
- Novel page / cover / LibreTranslate fetches block private/loopback hosts and re-check redirect targets. Translation still sends chapter text to Google or your LibreTranslate URL when enabled. Polish English stays on this PC.

### Tips

- Prefer the **main book page** URL from a supported site.
- For overnight runs: start the download, **Pause** or just close the app when you need the PC off, reopen later → **Resume**.
- If translation is rate-limited, lower **Translation Workers** (e.g. 30–50) and keep cache on.
- Menus:
  - **File** — Open books folder, Open data folder, Open log file
  - **Library** — Check for updates, Sync Drive now, Reset library…
  - **Help** — Check for updates, Auto-check on startup, How translation works…, **Cache…**, About, Drive OAuth setup…

## Supported Sites

| Site | URL Pattern | Status |
|------|-------------|--------|
| twkan.com | `https://twkan.com/book/{id}.html` | ✅ Working |
| 69shuba.com | `https://69shuba.com/book/{id}/` | ✅ Working |
| uukanshu.cc | `https://uukanshu.cc/book/{id}/` | ✅ Working |
| Other configured hosts | see `parsers/sites.json` | ✅ Best-effort (CSS selectors) |
| Unlisted sites | any novel table-of-contents URL | 🧪 Experimental (generic parser) |

## Adding New Sites

Add an object to `parsers/sites.json`. First matching `domains` entry wins. `generic.py` stays last as a heuristic fallback. If a configured site’s `content` selector misses, the same density heuristic is used, `Chapter.used_heuristic` is set, and the completion dialog warns you.

```json
{
  "name": "example.com",
  "domains": ["example.com"],
  "title": "h1",
  "author": ".author",
  "content": ["#chapter"],
  "chapter_list": "ul.toc a"
}
```

Optional fields: `description`, `cover`, `chapter_title`, `remove`, `toc_link`, `reverse`, `delay`, `encoding`, `language`, `book_id` (regex), `chapter_list_url` (may include `{book_id}`), `chapter_href_contains`, `referer`, `visit_toc_first`.

## Running Tests

The test suite is fully offline (HTML fixtures, no network needed):

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/
python -m ruff check .
```

CI runs this suite on Ubuntu, Windows, and macOS (Python 3.11 and 3.12). A `v*` tag build waits for the same tests before PyInstaller runs (pinned `pyinstaller==6.22.2`).

## Project Structure

```
.
├── app.py              # Entry → gui.app.run()
├── gui/                # PySide6 UI (main window, pages, workers)
├── requirements.txt    # Python dependencies
├── requirements-dev.txt # pytest, ruff, pinned PyInstaller
├── build.py            # PyInstaller build (regenerates HuaEPUB.spec; do not commit a stale spec)
├── core/
│   ├── branding.py     # Product name + legacy aliases
│   ├── parser.py       # Base parser class + registry
│   ├── cleaner.py      # Watermark/ad removal
│   ├── translator.py   # Translation (Google / LibreTranslate / Ollama)
│   ├── local_polish.py # Polish English entry (KEEP/REPLACE)
│   ├── polish/         # llama.cpp serve + span copy-edit
│   ├── epub_builder.py # EPUB creation (atomic write; skip second clean after translate)
│   ├── download_runner.py  # Pause/cancel/chapter download (UI-agnostic)
│   ├── settings.py     # Persistent app settings (atomic tmp+replace)
│   ├── cache.py        # Chapter + translation + cover + TOC caches (SQLite, 2 GB LRU)
│   ├── download_job.py # Local incomplete-download resume (not Drive)
│   ├── library.py      # Library + history store
│   ├── drive_sync.py   # Optional Google Drive sync (library.json + EPUBs only)
│   ├── security.py     # SSRF guards, safe zip extract, secret file perms
│   ├── notify.py       # Desktop notifications
│   ├── logger.py       # Log-to-file setup
│   └── updater.py      # Auto-updater (version + GitHub releases + relaunch helper)
├── parsers/
│   ├── sites.json      # Host + CSS selector configs
│   ├── config.py       # Single parser that reads sites.json
│   └── generic.py      # Fallback parser for other sites
└── tests/              # Offline pytest suite
```

## Troubleshooting

### Polish English is greyed out
- Turn on **Translate to English**
- Set **Translator** to Google or LibreTranslate (not Ollama — that already translates locally)

### Polish English
- Does **not** need Ollama
- First run downloads llama.cpp + a Qwen2.5 Instruct GGUF that fits this GPU (into `~/.huaepub/polish`). Later runs skip the download.
- NVIDIA / AMD / Apple Silicon use CUDA / Vulkan / Metal; otherwise CPU (3B, slower)
- If llama.cpp is already on `:8080` (or vLLM on `:8000`), that server is used
- Only awkward MTL spans are sent to the GPU; fluent Google English is copied
- Writes the same EPUB as a normal translated download (no extra polished copy)
- Progress and errors go to `~/.huaepub/logs/huaepub.log` (File → **Open log file**)
- If polish never starts and the log mentions GPU busy: quit **Ollama** from the tray (Quit Ollama), then retry
- Cancel during polish still writes the EPUB with Google/LibreTranslate English

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

### Cancel wrote no EPUB / still wrote an EPUB
- Cancel **during translation** is supposed to write nothing (avoids a half-translated book).
- Cancel **during polish** is supposed to save the EPUB with machine translation.
- Cached chapter HTML is kept either way.

### Cache grew huge / old chapters disappeared
- Default cap is 2 GB. Help → **Cache…** to raise it, set Unlimited, or clear chapters vs everything.
- Over the cap, oldest stored chapter HTML is deleted first. Translations are kept unless the file is still over the limit. Nothing is cleared on a timer.

### Update installed but the app does not reopen (any OS)
- Confirm you are on **2.10.1+**. 2.9.2–2.10.0 could reopen a Terminal/`python` session on macOS instead of the `dist` binary, and on Windows the helper was often killed with the old process so nothing reopened. Older onefile builds could also fail to relaunch (PyInstaller treated the new process as a worker of the dying extract). Polish English in **2.9+** uses llama.cpp (Ollama is not required); 2.7.x used Ollama for polish.
- After “Update ready”, allow the app to quit; do not force-quit the helper. It should reopen itself. If it does not, start `HuaEPUB` from the same folder you installed into.
- If it still fails, download the OS zip from [Releases](https://github.com/joelsnl/HuaEPUB/releases) and replace the binary manually. On macOS, clear quarantine if Gatekeeper blocks it: `xattr -cr /path/to/HuaEPUB`.

### Update refused / checksum error
- The release may be incomplete, or the download was corrupted — try again later, or install the zip from Releases manually after checking `SHA256SUMS.txt`.

## Credits

- Inspired by [WebToEpub](https://github.com/dteviot/WebToEpub) (dteviot, Apache-2.0); some site CSS selectors in `parsers/sites.json` are adapted from it
- Translation logic from fixTranslate.py
- Polish English uses [llama.cpp](https://github.com/ggml-org/llama.cpp) and [Qwen2.5](https://huggingface.co/Qwen) Instruct GGUFs
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
