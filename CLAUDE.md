# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview
HuaEPUB (formerly novelDownloader) is a Python GUI application (PySide6 / Qt) for downloading Chinese web novels, optionally translating them to English (Google Translate, LibreTranslate, or local Ollama), optionally polishing Google/LibreTranslate English with local llama.cpp, and packaging them as EPUB files.

Modes: **Single**, **Multi** (block-paste URLs), and **Library** (track novels, check/update for new chapters). Optional Google Drive sync for library metadata and/or EPUBs. User data lives under `~/.huaepub/` (migrates from `~/.noveldownloader/` when present).

Current release version lives in `core/updater.py` (`__version__`) and the README header — keep those two in sync on every release.

## Layout
All application code lives at the repository root (there used to be a duplicate copy in `novel_downloader/`; it was removed - do not recreate it).

- `app.py` - thin entry → `gui.app.run()`
- `gui/` - PySide6 UI (`main_window`, pages, workers, dark `style.qss`). Worker→UI must use `@Slot` methods on the main window (never bare lambdas — they can run on the worker thread and crash Qt). Menus: File (books / data / log), Library (check / Drive sync / reset), Help (updates, How translation works, **Cache…**, About, Drive OAuth). Shortcuts: Ctrl+Enter fetch/download on Single/Multi; Esc cancels an active job. Window geometry is restored from settings.
- `gui/dialogs.py` - QMessageBox helpers. Fusion underlines `&Yes`/`&No` but those are Alt+Y/N; bind **Y**/**N** (and other first letters) so the keys work. Use these instead of `QMessageBox.question/information/warning/critical`.
- `core/branding.py` - product name, data-dir name, exe basename, and legacy aliases
- `core/parser.py` - `BaseParser`, `Chapter`/`NovelInfo` dataclasses (`Chapter.used_heuristic` when sites.json content selectors miss), parser registry, `create_http_session()` (curl_cffi Chrome impersonation with requests fallback)
- `core/cleaner.py` - `ContentCleaner`: watermark/ad removal, XHTML structure fixing, br-to-p conversion
- `core/translator.py` - Google / LibreTranslate / Ollama: concurrent translation with bounded multi-pass retry (hard cap on passes; partial improvements are accepted so it never loops forever). Translation cache writes are batched (`put_translation(..., commit=False)` + `flush()`).
- `core/local_polish.py` - after Google/LibreTranslate, KEEP/REPLACE polish via `core/polish/` (auto-installs llama.cpp + Qwen GGUF under `~/.huaepub/polish`; Ollama not required). In-memory on the same EPUB; logs via print() into `huaepub.log`.
- `core/polish/` - llama.cpp download/serve, hardware caps (3B/7B/14B), span KEEP/REPLACE. Never Drive-sync this cache. Do not make polish depend on Ollama.
- `core/epub_builder.py` - `EPUBBuilder` and `TranslatedEPUBBuilder` (ebooklib); translations are applied at the text-node level, never with raw string replacement. Atomic write (sibling `.tmp` then replace). Translated builds skip a second `clean_html`. Cancel during translation raises `DownloadCancelled` (no EPUB). Cancel during polish still writes the EPUB (`polish_cancelled`).
- `core/download_runner.py` - UI-agnostic pause/cancel/chapter-cache/EPUB orchestration. ETA uses uncached/network samples only. Completion notes cover leftover Chinese, heuristic chapters, and polish-cancelled.
- `core/updater.py` - auto-updater against GitHub releases (`__version__` lives here — bump on each release). Frozen onefile relaunch must strip `_PYI_*` / stale `_MEI` env and set `PYINSTALLER_RESET_ENVIRONMENT=1`. Windows helper: **ShellExecute** a hidden `powershell.exe -File` (not a Popen child — the onefile bootloader kills those on exit). POSIX: `/bin/sh` or `/bin/bash` — **never** `sys.executable` when frozen. Relaunch the bare binary with double-fork + exec — **never** `/usr/bin/open` (that opens Terminal.app).
- `core/settings.py` - persistent settings JSON; atomic tmp+replace under a lock around the full read-modify-write. `get_data_dir()`, `get_default_books_dir()`. `cache_max_mb` (default 2048; `0` = unlimited). `polish_notice_shown` (first-run Polish dialog). `window_x/y/w/h` (0 width/height = default size).
- `core/cache.py` - SQLite caches: chapters, translations (including polished spans), **covers**, **chapter-list snapshots**. Local-only; never Drive-synced. Default 2 GB cap; LRU by oldest stored chapter HTML first, then covers, TOCs, translations last; VACUUM after purge. Help → Cache… is the UI.
- `core/download_job.py` - local-only `active_download.json` so Pause/close/reboot can resume (never Drive-synced)
- `core/library.py` - history + tracked library (`library.json`), chapter-diff helpers for updates
- `core/drive_sync.py` - optional Google Drive sync (`drive.file` scope only; visible folder; **library.json + EPUBs only**). GUI queues a silent auto-sync after successful Single / Multi / Library Update / Update All (does not switch to the Library tab).
- `core/notify.py` - OS done/update notifications (Windows toast payloads via base64 — never interpolate novel titles into expandable PowerShell)
- `core/logger.py` - stdlib `logging` + `RotatingFileHandler` to `logs/huaepub.log` (1 MB, keep `.log.1`, rotates during a long session). Outside pytest, tees stdout/stderr into the logger (do not add a StreamHandler back to stdout). `sys.excepthook` / thread hook + faulthandler → `logs/huaepub.fault.log`. Existing `print()` calls stay; they are captured via the tee.
- `core/utils.py` - shared helpers (`safe_filename`, URL extraction, ETA formatting)
- `core/security.py` - SSRF URL validation, `safe_http_request` (re-checks redirect hops), `safe_extract_zip` (zip-slip + size caps), `write_secret_file` / update-helper JSON
- `parsers/` - `sites.json` (host + CSS selectors) read by a single `SiteConfigParser`; `generic.py` is the heuristic fallback for unknown hosts. Registration order matters: `parsers/__init__.py` must register the JSON parser first and `generic` LAST. A content-selector miss sets `Chapter.used_heuristic` and is shown in the completion dialog.
- `tests/` - offline pytest suite with HTML fixtures (no network access needed). Includes `sites.json` schema checks and a cache→EPUB pipeline test. `tests/test_dialogs.py` imports PySide6 and skips collection if OS GL/EGL libs are missing; Linux CI/release installs `libegl1` so those tests run.
- `build.py` - PyInstaller packaging; **regenerates** `HuaEPUB.spec` each build. Do not commit a leftover spec (`*.spec` is gitignored). Pin is `pyinstaller==6.22.2` (keep in sync with `requirements-dev.txt` and release.yml).
- `requirements-dev.txt` - pytest, ruff, pinned PyInstaller
- `.github/workflows/release.yml` - pytest + ruff gate, then Win/macOS/Linux zips on `v*` tag push
- `.github/workflows/ci.yml` - ruff, then offline pytest on Ubuntu/Windows/macOS × Python 3.11/3.12

## Workflow
Source Input → Parse → Clean → Translate (optional) → Polish English (optional, local llama.cpp) → Build EPUB.

Chapter downloads are sequential on purpose (per-site `request_delay` avoids bans); do NOT parallelize them.

Library updates rebuild a full EPUB from cache + new chapters. Drive sync is offline-first and opt-in (Library tab). After a successful download/update, the GUI queues a silent Drive sync if Drive is enabled.

## Commands
- Run the app: `python3 app.py`
- Run tests: `python3 -m pytest tests/`
- Lint: `python3 -m ruff check .`
- Build executable: `python3 build.py`
- Dependencies: `pip install -r requirements.txt`
- Dev tools: `pip install -r requirements-dev.txt`
- Release: bump `__version__` in `core/updater.py` + README, commit, tag `vX.Y.Z`, push tag (test job must pass before the build matrix starts)

## Conventions
- To support a new site: add an object to `parsers/sites.json` (domains + CSS selectors). Keep `generic` registered last. Do not add per-site Python modules or a WebToEpub extractor.
- Parsers must RAISE on failed content extraction (never return placeholder HTML); the app handles failures and retries them at the end of a run. If `sites.json` content selectors miss, GenericParser may still extract via density heuristic — log it and set `used_heuristic`.
- Text cleaning rules live in `core/cleaner.py`; output format changes in `core/epub_builder.py`. Do not run `clean_html` a second time on already-cleaned translated chapters.
- Keep the default of 200 translation workers; the retry/backoff logic in `translator.py` handles rate limiting. Polish does not use that worker count.
- Cancel during chapter fetch or Chinese→English translation: abort and write no EPUB (resume point is cleared). Cancel during polish: still write the EPUB with machine translation (`polish_cancelled`). Download EPUB stays disabled while a job is running.
- Cache is not timer-cleared. Default cap is 2 GB (`cache_max_mb`); oldest stored chapter HTML is evicted first. Translations are kept unless the file is still over the cap.
- Drive OAuth uses `drive.file` only; client JSON at `~/.huaepub/google_oauth_client.json`. Do not reintroduce hidden `appDataFolder` sync. Never Drive-sync `cache.db`, resume files, or `polish/`.
- Auto-updates must fail closed without a matching `SHA256SUMS` entry; never fall back to unsigned `main.zip`. Release CI must publish `HuaEPUB-*.zip` (with legacy `NovelDownloader` binary inside), `HuaEPUB-source.zip`, legacy `novelDownloader-source.zip` alias, and `SHA256SUMS.txt`.
- Frozen updates (all OSes): GUI must quit after a successful download so the helper can swap/relaunch. Strip `_PYI_*` and set `PYINSTALLER_RESET_ENVIRONMENT=1` on every relaunch path. Frozen POSIX: launch `_update_helper.sh` via `/bin/sh` or `/bin/bash` — **never** `sys.executable` when frozen (that reopens the GUI). Prefer python3 inside the helper for replace; re-hash staged `_new_*` before `mv`. Relaunch with double-fork + exec — never `/usr/bin/open` on a bare binary. Windows: ShellExecute a hidden `powershell.exe -File` (not a Popen child of the onefile process).
- Product fetches (pages, covers, LibreTranslate) must go through `safe_http_request` / `validate_fetch_url`, not raw `session.get` with automatic redirects.
- Product strings live in `core/branding.py` — prefer importing those over hardcoding.
- Settings writes are atomic (tmp+replace) under one lock for the full read-modify-write.
- Yes/No (and other) popups go through `gui/dialogs.py` so Y/N work without Alt. Do not use the static `QMessageBox.question` helpers.
- `closeEvent` during a download: persist the resume job, `request_cancel` (do not clear the resume point), wait up to 15s, `terminate` only as a last resort. Update-install close stays a short wait.
- Status copy should name the phase (Fetching chapters / Translating / Polishing / Writing EPUB). Completion dialogs with leftover Chinese, placeholders, heuristic chapters, or a cancelled polish pass use the title **Saved with warnings** — never a Success title with the warning only in the body.
