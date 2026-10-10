# CLAUDE.md

Guidance for Claude Code (claude.ai/code) and other contributors working in this repository.
User-facing documentation is in `README.md` and `docs/`; this file holds the rules and the
non-obvious facts that are easy to break.

## Overview

HuaEPUB (formerly novelDownloader) is a Python application (PySide6 / Qt) for downloading Chinese
web novels, optionally translating them to English (Google, Microsoft Edge, LibreTranslate, local
Ollama, or optional offline NMT via CTranslate2), optionally polishing machine English with local
llama.cpp, and packaging them as EPUB files.

Modes: **Single**, **Multi** (block-paste links), **Library** (track novels, check and update for new
chapters) and **Read** (in-app reader). **Server mode** serves the same app to browsers (`web/`):
*This network* (HTTP, private/loopback clients, access code + QR) or *Anywhere* (HTTPS + password).
While serving, the desktop tabs are swapped for a server screen and the browser is the app.

User data lives under `~/.huaepub/` (migrated from `~/.noveldownloader/` when present).

The release number lives only in the repo-root `VERSION` file. `core/updater.py` reads it. Do not copy
it into the README, pyproject, Snap or metainfo.

## Commands

- Run: `python3 app.py` (desktop) or `python3 app.py --headless` (server, no window).
  `python3 app.py --install-service` writes a user systemd unit only when none exists, then exits.
- Test: `python3 -m pytest tests/` (offline). Lint: `python3 -m ruff check .`
- Build the executable: `python3 build.py`
- Dependencies: `pip install -r requirements.txt -r requirements-gui.txt` (headless needs only
  `requirements.txt`); dev tools: `pip install -r requirements-dev.txt`
- Release: bump `VERSION`, commit, merge, tag `vX.Y.Z`, push the tag. The test job must pass before
  the build matrix starts.

## Documentation

- `README.md` is the front page; `docs/` has the user guide, translation, server mode,
  troubleshooting, architecture and development pages. Update the page that owns a topic instead of
  repeating it elsewhere.
- `tests/test_docs.py` fails when a link, an anchor or a repository path named in any Markdown file no
  longer exists. Renaming or splitting a module means fixing the docs in the same change.
- Screenshots in `docs/screenshots/` come from `tools/make_demo_home.py` (invented books). Never
  screenshot a real library.

## Repository map

All application code is at the repository root. There used to be a duplicate in `novel_downloader/`;
do not recreate it.

- `app.py`: thin entry to `gui.app.run()`.
- `core/`: the engine. Qt-free; never imports `gui` or `web`. `session.py` (`AppSession`), `tasks.py`
  (job bodies), `parser.py`, `cleaner.py`, `ad_detect.py`, `translator*.py`, `translation_progress.py`,
  `translation/`, `polish/`, `local_polish.py`, `ollama_setup.py`, `epub_builder.py`,
  `download_runner.py`, `download_job.py`, `cache.py`, `library.py`, `library_check.py`, `reader.py`,
  `reading.py`, `settings.py`, `atomic_io.py`, `security.py`, `updater.py`, `update_scripts.py`,
  `logger.py`, `notify.py`, `branding.py`, `utils.py`.
- `parsers/`: `sites.json` (host + CSS selectors) and the one parser that reads it.
- `gui/`: the PySide6 window (`main_window.py`, mixins in `window/`, `pages/`, `widgets/`,
  `workers/`, `dialogs.py`, `theme.py` + `style.qss`, `help_content.py`).
- `web/`: server mode: the FastAPI backend and the browser app in `web/static/`. Never imports Qt or
  `gui`.
- `tests/`: offline pytest suite. `tools/`: `make_demo_home.py`, `bench_google_gtx.py`.
- `build.py`: PyInstaller packaging. `packaging/` and `snapcraft.yaml`: Linux packaging descriptors
  (not built by CI). `.github/workflows/`: `ci.yml` and `release.yml`.

Layer rules, enforced by `tests/test_server_mode.py`: `core/` and `parsers/` never import `web` or
`gui`; `web/` never imports `gui` or Qt. `gui/` may import `web.host` and `web.auth` to start and
describe the server.

## Download pipeline

Source input, parse, clean, translate (optional), polish (optional), build EPUB. Chapter downloads
are **sequential on purpose** (a per-site `request_delay` avoids bans): do not parallelise them.

Library updates rebuild a full EPUB from the cache plus the new chapters. The library and EPUBs stay on
this PC.

- `core/tasks.py`: Qt-free job bodies shared by the desktop workers and server mode (`run_single`,
  `run_multi`, `run_library_update`, `run_library_update_all`). Every book goes through
  `download_one_novel` (fetch, build, record). The Qt workers in `gui/workers/` are thin wrappers; do
  not fork the logic back into them.
- `core/download_runner.py`: pause/cancel, the chapter cache loop, EPUB orchestration and
  `translate_then_build` (clean, translate, polish, apply, write), which is a short sequence of named
  stages. `core/translation_progress.py` owns the status line, ETAs and retry-pass messages
  (`TranslationProgress` is the translator's progress callback). Fetch ETA uses uncached/network
  samples only; the translate status names the engine, segment counts, in-flight requests, current
  chapter and ETA. Prefetch warms the translation cache and may fill `Chapter.translated_content` for
  preview (LibreTranslate, or Offline NMT only when the model is already on disk; Google does not hit
  gtx during fetch, and NMT does not download ~320 MB during a scrape). The final pass always runs
  `translate_texts_with_retry`, which counts only usable cache hits (echoed Chinese is not done).
  `library_epub_path` picks a book's EPUB path (its Library file name when tracked). When translate
  is on, `prepare_translation` builds `NovelTranslator` before the sequential chapter loop so
  LibreTranslate prefetch can overlap `request_delay`.
- `core/epub_builder.py`: `EPUBBuilder` and `TranslatedEPUBBuilder` (ebooklib). `apply_translations`
  writes at the text-node level, never by raw string replacement. Atomic write (sibling `.tmp`, then
  replace). Translated builds skip a second `clean_html`. `build_with_translation` delegates the
  sequencing to `translate_then_build`. Each EPUB carries `huaepub/chapters.json` (chapter file to
  source URL) so a reading place survives a rebuild.
- `core/cleaner.py`: `ContentCleaner` (watermark/ad removal in Simplified and Traditional, ad divs
  with content, XHTML structure fixing, br-to-p). Repeating site junk is learned from the first
  chapters by `core/ad_detect.py` when Clean is on, independent of Polish and llama.cpp. Do not start
  llama.cpp or download a GGUF for this. Do not run `clean_html` a second time on already-cleaned
  translated chapters.
- `core/parser.py`: `BaseParser`, `Chapter` / `NovelInfo` (`Chapter.used_heuristic` when `sites.json`
  content selectors miss), the parser registry, `CHROME_UA`, `create_http_session(*, ipv4=False,
  pool_size=None)` (curl_cffi Chrome impersonation with a requests fallback) and
  `fetch_info_and_chapters`.
- `parsers/`: `sites.json` is read by a single `SiteConfigParser`; `generic.py` is the heuristic
  fallback for unknown hosts; `pagination.py` walks `chapter_list_next` / `content_next` (and generic
  `rel=next` / 下一页 only) and converts TOC `<a>` nodes via `links_to_chapters`. Registration order
  matters: `parsers/__init__.py` registers the JSON parser first and `generic` **last**. A content
  selector miss sets `Chapter.used_heuristic` and is shown in the completion dialog. Do not add
  per-site Python modules, and do not fill `sites.json` next selectors unless the host is known to
  paginate. Configured sites must not follow `rel=next` unless those keys are set, and content-next
  must not follow 下一章 / next chapter. Parsers must **raise** on failed content extraction (never
  return placeholder HTML); the app handles failures and retries them at the end of a run. If the
  selectors miss, `GenericParser` may still extract by density: log it and set `used_heuristic`.
- Cancel during chapter fetch or Chinese-to-English translation: abort and write no EPUB (the resume
  point is cleared). Cancel during polish: still write the EPUB with the machine translation
  (`polish_cancelled`). Download EPUB stays disabled while a job runs.
- Completion notes cover leftover Chinese, heuristic chapters and a cancelled polish pass. Dialogs
  with any of those use the title **Saved with warnings**, never a Success title with the warning only
  in the body. Status copy names the phase (Fetching chapters / Translating / Polishing / Writing
  EPUB).
- `core/download_job.py`: local-only `active_download.json` so Pause, closing or a reboot can resume.
  Builds the job dicts (`book_job`, `multi_job`, `library_update_all_job`) and decodes them for resume
  (`single_from_job`, `novels_from_job`, `entries_from_job`) for both the desktop and server mode.
  `closeEvent` during a download persists the job, calls `request_cancel` (it does not clear the resume
  point), waits up to 15 s and `terminate`s only as a last resort; an update-install close waits only
  briefly.

## Translation

- `core/translator.py`: `GoogleTranslator` runs a translation job for **every** engine (the name is
  historical): a thread pool, a per-IP throttle, bounded multi-pass retry, progress. Engines are
  Google (New `translate-pa` / HTML / Old gtx), Microsoft Edge, LibreTranslate and Ollama, in
  `core/translator_engines.py` (`EngineRequestsMixin`). `core/translator_cache.py`
  (`TranslationCacheMixin`) holds `is_usable_translation` (empty or echoed Chinese is rejected) and the
  cache helpers. The class composes both, and `NovelTranslator` subclasses it. Re-exports for
  `from core.translator import …` are listed in `__all__`.
- Default Google is **New** (`translate-pa.googleapis.com/v1/translate`), the same as Calibre Ebook
  Translator 2.4+ *Google (Free) - New*. Old gtx is still selectable. HTTP sessions are thread-local,
  cache writes are batched, and LibreTranslate may pack several paragraphs per call.
- Throttle: unofficial engines use `GtxThrottle` (`core/gtx_throttle.py`: start 8 in flight, floor 2,
  cool new requests on 429, climb +1 per success). Do not restore "429 backs off that worker only;
  the other 199 keep going."
- Default Google workers is 200 (capped at 200) as the **ceiling**, further capped by
  `machine_worker_cap()` (24 threads under 1 GB RAM, 64 under 2 GB, at most a quarter of RLIMIT_NPROC):
  a Pi Zero 2 W failed with "can't start new thread" at 200. Polish does not use this worker count.
- `core/translation/`: glossary protect/restore (`§G0§` placeholders), the optional CTranslate2
  opus-mt-zh-en adapter, and `NovelTranslator` (the pipeline facade). Do not add `huaepub/translator/`;
  keep this under `core/`.
  - Built-in terms are `core/translation/data/novel_terms.json`, a curated cultivation/wuxia pack
    expanded in releases. It is not CEDICT and is not grown by swallowing a general dictionary.
  - Default glossary mode is **off** (names come straight from the translator; changing modes changes
    cache keys, so a book re-translates once). In **auto** the pack attaches only when the title,
    description or early chapter titles look like cultivation (strong markers such as 修仙 / 金丹 /
    灵根; 公子 or 凡人 alone do not count). Setting `translation_glossary`: `auto` | `xianxia` | `user`
    | `off` (UI: Auto, Cultivation pack, Names only, Off).
  - User terms: `~/.huaepub/glossary.json`; per-novel: `~/.huaepub/glossaries/<title>.json`. In the
    final translate pass `harvest.py` mines names, sects and techniques from the book's Chinese and
    romanises them with **pypinyin** (not Google).
  - Optional local Qwen (`qwen_glossary.py`, a 7B+ polish GGUF already on disk) classifies those
    candidates with evidence. It does not invent terms from titles and writes no global dump. The
    startup modal appears only if the GGUF is present; Help → Polish glossaries with Qwen shows Accept
    all / Discard. It never starts a GGUF download and does not rewrite the shipped `novel_terms.json`.
    Legacy `glossary-qwen.json` is read only for cultivation books.
  - Configure the glossary **before** prefetch (`prepare_translation` / `configure_glossary`) so cache
    keys include the pack fingerprint. Harvest/Qwen may change the fingerprint for the final pass
    (a prefetch warmup may miss; that is acceptable).
  - Offline NMT cache: `~/.huaepub/nmt/` (Hugging Face https only, no invented SHA256; redirects may
    go to `*.cdn.hf.co`, which is allowed, but do not allow all of `*.hf.co`). Prefetch never downloads
    the ~320 MB model; `ensure_nmt_model` runs on the translate pass, once per process failure. CUDA
    without cuBLAS falls back to CPU, never to Google. Register the CUDA 12 DLL directories with
    `os.add_dll_directory` and **keep the returned cookie alive** (garbage collection undoes the path),
    and also `ctypes.CDLL`-preload by absolute path. Use pip `nvidia-cublas-cu12` / the toolkit /
    Ollama; CUDA 13 is not sufficient. `ctranslate2` and `sentencepiece` are optional
    (`requirements-nmt.txt`); missing packages fall back to Google.
- `core/ollama_setup.py`: Ollama install/GPU/probe/pull (kept out of the translator retry/cache code).
- `core/local_polish.py`: after Google/LibreTranslate, KEEP/REPLACE polish via `core/polish/`
  (auto-installs llama.cpp and a Qwen GGUF under `~/.huaepub/polish`; Ollama not required). It runs in
  memory on the same EPUB and logs through `print()` into `huaepub.log`.
- `core/polish/`: llama.cpp download/serve, hardware caps (3B/7B/14B), span KEEP/REPLACE. Tagger
  training (`train_tagger`) lives in `tagger_train.py`; the runtime `get_tagger` / `SpanTagger` stay in
  `tagger.py`. Downloads go through `validate_polish_download_url` (GitHub and Hugging Face https only)
  and `safe_extract_zip` / `safe_extract_tar`. Verify SHA256 when GitHub publishes a `digest` or
  `HF_GGUF_SHA256` has an official file hash; do not invent SHA256s for unofficial or split GGUFs. This
  cache stays on this PC. Do not make polish depend on Ollama.

## Data, library and reading

- `core/settings.py`: persistent settings JSON; atomic tmp+replace under one lock around the full
  read-modify-write (`core/atomic_io.py`, `fsync=True`). `get_data_dir()`, `get_default_books_dir()`.
  Keys: `cache_max_mb` (default 2048; `0` = unlimited), `polish_notice_shown`, `nmt_notice_shown`,
  `window_x/y/w/h` (0 width/height = default), `server_enabled`, `server_mode` (`lan` | `remote`),
  `server_port` (8765), `server_hostname`, `server_cert_path`, `server_key_path`, `ui_look`,
  `reader_font_pt`.
- `core/atomic_io.py`: `atomic_write_text` / `atomic_write_json(*, fsync=…)`. Settings and reading
  position fsync; library, glossary and resume jobs do not. EPUB binary writes stay on
  `write_epub_atomic`.
- `core/cache.py`: SQLite caches: chapters, translations (including polished spans), covers and
  chapter-list snapshots. Local only. Bulk `get_translations_bulk` / `delete_translations`; cache keys
  collapse whitespace; chapter fingerprints store a translated node list. Default 2 GB cap, evicting
  oldest stored chapter HTML first, then covers, TOCs, and translations last, with VACUUM after a purge.
  Nothing is cleared on a timer. Help → Cache… is the UI.
- `core/library.py`: history and the tracked library (`library.json`), chapter-diff helpers for updates,
  `remove_and_purge`. `LibraryEntry.shelved_at` (0 = not shelved): shelving keeps the entry and its
  files, but `run_library_check` and `/api/library/check` skip it, `upsert_library` keeps the flag, and
  `find_shelved(url, title)` (the link, or the whitespace/case-insensitive original title for the same
  novel on another site) drives the "You shelved this" note on `/api/preview` and Multi lookup rows
  (status `Shelved`, left out of the build). Shelving exists in the browser only; the desktop Library
  has no shelve UI yet.
- `core/reading.py`: local-only `reading.json` (chapter index, scroll, bookmarks). A position stores the
  chapter's source URL, and EPUBs carry `huaepub/chapters.json` (read by `load_epub_chapters`), so
  `resume_index` finds the same chapter after a Library update rebuilds the book even when the site's
  list shifted. Older EPUBs and positions fall back to the chapter number.
- `core/reader.py`: resolves a book for the in-app reader (an EPUB under the books folder, else cached
  TOC/HTML) and sanitises HTML for QTextBrowser. `next_cache_prefetch_index` and
  `html_needs_live_translate` drive N+1 prefetch and live translation; `fetch_reader_chapter` and
  `live_translate_chapter` do the one-chapter fetch and translate for both the desktop Read tab and the
  browser reader.
- The **Read** tab prefers a local EPUB, else cached chapter HTML (an on-demand site fetch for a missing
  chapter, with the site `request_delay` and no translation or polish, then an N+1 prefetch). With
  Translate on, cache HTML can be live-translated. Reading position is local only.
- Local-only files: `cache.db`, resume files, `reading.json`, `polish/`, `nmt/`, `server/`,
  `glossary.json`, `glossary-qwen.json`, `glossaries/`.
- Google Drive sync was removed. Do not add it back.
- `core/utils.py`: shared helpers (`safe_filename`, URL extraction, ETA formatting, `in_pytest()`,
  `pipeline_phase` for the status-to-slips phase on both UIs). `core/branding.py`: product name, data
  dir name, exe basename and legacy aliases; prefer importing product strings from it.
- `core/logger.py`: stdlib `logging` with a `RotatingFileHandler` to `logs/huaepub.log` (1 MB, keeps
  `.log.1`). Outside pytest it tees stdout/stderr into the logger (do not add a `StreamHandler` back to
  stdout). `sys.excepthook`, the thread hook and faulthandler write `logs/huaepub.fault.log`. Existing
  `print()` calls stay; they are captured by the tee.
- `core/notify.py`: OS done/update notifications. Windows toast payloads go through base64; never
  interpolate novel titles into expandable PowerShell.

## Security

- `core/security.py`: SSRF URL validation, `safe_http_request` (re-checks redirect hops),
  `safe_extract_zip` / `safe_extract_tar` (zip-slip and size caps), the polish download host pin and
  SHA256 helpers, `write_secret_file` and the update-helper JSON.
- Product fetches (pages, covers, LibreTranslate) must go through `safe_http_request` /
  `validate_fetch_url`, not a raw `session.get` with automatic redirects.
- Server mode: never log the access code, password, cookies or open-link tokens. Remote mode is
  HTTPS-only (`ServerContext` refuses otherwise). Keep LAN mode's client-IP and Host checks. Browser
  jobs and desktop jobs must never run at the same time (the server screen replaces the tabs).

## Updater

- `core/updater.py` checks GitHub releases (`__version__` comes from the repo-root `VERSION`), downloads
  the platform asset and relaunches. `core/update_scripts.py` holds the helper scripts it writes (as
  plain text constants; paths and the hash reach them through the JSON config, never interpolation).
- Auto-updates must **fail closed** without a matching `SHA256SUMS` entry; never fall back to unsigned
  `main.zip`. Release CI must publish `HuaEPUB-*.zip` (the HuaEPUB binary only), `HuaEPUB-source.zip`
  and `SHA256SUMS.txt`. Do not ship a NovelDownloader binary or a `novelDownloader-source.zip` alias.
- Frozen updates (all OSes): the GUI must quit after a successful download so the helper can
  swap and relaunch. Strip `_PYI_*` and stale `_MEI` env and set `PYINSTALLER_RESET_ENVIRONMENT=1` on
  every relaunch path. Windows: **ShellExecute** a hidden `powershell.exe -File` (not a Popen child;
  the onefile bootloader kills those on exit). POSIX: run `_update_helper.sh` via `/bin/sh` or
  `/bin/bash`, **never** `sys.executable` when frozen (that reopens the GUI); prefer python3 inside the
  helper for the replace and re-hash the staged `_new_*` before `mv`. Relaunch the bare binary with a
  double-fork and exec, **never** `/usr/bin/open` (that opens Terminal.app).
- Accepting an app update hides the main window behind `UpdateProgressDialog` (`gui/dialogs.py`:
  app-modal, no close/Esc). The asset download streams (`_download_release_asset`, progress throttled to
  `DOWNLOAD_PROGRESS_INTERVAL`) so the bar shows real MB. On success the same dialog switches to
  `show_ready` with one **Restart now** button; never stack a second message box on it. A failed
  download restores the window.
- The startup update check runs at 5 s, after the GUI-thread startup jobs (the Library cover refresh at
  4 s and the Qwen glossary GPU probe at 3.5 s); fired inside the update prompt they froze its Yes
  button.

## Desktop GUI (`gui/`)

- Window behaviour is split into mixins in `gui/window/` (`worker_host`, `reader_actions`,
  `library_actions`, `server_actions`, `look_actions`, `glossary_actions`, `resume_actions`,
  `update_actions`). Slot handlers that Qt connects across threads keep a thin `@Slot` wrapper on
  `MainWindow` that delegates to the mixin, so every slot stays on the QObject class.
- Worker-to-UI traffic must use `@Slot` methods on the main window, never bare lambdas (they can run
  on the worker thread and crash Qt).
- Menus: File (books / data / log / **Server mode…**), View (**Look**), Library (check / reset), Help
  (updates, How translation works, Polish glossaries with Qwen, **Cache…**, About). The help text is in
  `gui/help_content.py`.
- No app keyboard shortcuts except **Y** / **N** on Yes/No dialogs. Fusion underlines `&Yes` / `&No`,
  but those are Alt+Y/N, so bind **Y**/**N** only on Yes/No (and Yes / Not now) and no other first
  letters. Use `gui/dialogs.py` helpers instead of `QMessageBox.question/information/warning/critical`.
  Single / Multi completion uses `show_info_with_preview`; the cache, recent-download and rich HTML
  About boxes live there too.
- Window geometry is restored from settings. The Library grid/list uses extended selection (Select All
  / None / Invert, or Ctrl/Shift-click): **Update**, **Remove** and **Download EPUB** apply to the
  selection, while **Read**, **Open URL** and double-click use the current book.
- `gui/theme.py` + `gui/style.qss` are the Slips look. `style.qss` is a `string.Template` (`${ground}`,
  `${accent}`, …) rendered per palette; never hardcode colours in widgets (use object names such as
  `mutedLabel` / `hintLabel` / `eyebrow`). Six palettes match `web/static/app.css` exactly (a test checks).
  `ui_look`: `auto` (follows `styleHints().colorScheme()` live: dark is Catalogue at night, light is
  Catalogue) | `dark` | `light` | `indigo` | `gold` | `cinnabar` | `mist` | `random` (opt-in only).
  Hanken Grotesk and JetBrains Mono (OFL) are bundled in `gui/assets/fonts`; the CJK serif is not
  bundled (system fallback). `gui/widgets/slips.py` (`SlipStrip`, `SealMark` 译) replaces the progress bar.
- `gui/window/server_actions.py`: the **SERVE** chip (tab-bar corner) and File → Server mode…
  (`gui/server_dialog.py`). It refuses to start while a desktop job or a library check runs. While
  serving, the central `QStackedWidget` shows `gui/widgets/server_screen.py`; Server mode, the Library
  menu, Qwen glossary and Cache… are disabled, and clipboard watch and the startup resume/glossary offers
  are skipped. Stopping applies `options.apply_snapshot` from settings (the browser may have changed
  them) before anything persists, then refreshes the library and checks for a resume point.
  `closeEvent` stops the server (the resume point is kept) but leaves `server_enabled` on so the next
  launch serves again; a failed launch start turns it off and says so.
- `core/session.py` (`AppSession`: settings, cache, library store, `DownloadControl`) is Qt-free so the
  desktop and server share one; `gui/session.py` re-exports it.

## Server mode (`web/`)

FastAPI and uvicorn on a daemon thread inside the desktop process (no `python3 -m web`). Never import
Qt or `gui`.

- `server.py` only wires the app (the Busy handler, the guard, the routers, `/` and `/static`).
  `guard.py`: LAN mode answers only private/loopback client IPs and an IP/localhost `Host`; body
  <= 256 KB; every non-GET needs the same Origin and `X-HuaEPUB: 1`; session check; CSP
  `script-src 'self'`; security headers; HSTS only with the user's own cert. Routers, each a
  `build_router(ctx)`: `auth_api` (session, login, logout, one-time open link), `task_api` (state, SSE
  events, storage, pause/cancel, files, resume), `download_api` (preview, single, multi),
  `settings_api` (settings, installs, service), `library_api`, `reader_api` (same `reading.json`).
- `host.py` (`ServerHost.start/stop`, `lan_address`, `port_is_free`, `find_public_address` via
  `safe_http_request` only on click), `auth.py` (`ServerSecrets` in `~/.huaepub/server/secret.json` via
  `write_secret_file`: HMAC key, 8-char code, scrypt password >= 10 chars, generation counters so
  **New code** or a new password signs everyone out; `LoginLimiter` 5 per IP / 15 min + 30 global / 10
  min -> a 10-min pause; one-time 60 s `OpenLinks` for the host browser), `tls.py` (self-signed EC P-256
  with LAN IP/hostname SANs, reused while it covers the names; an own cert/key must load together),
  `tasks.py` (`TaskManager`: one task slot, `exclusive()` for reader fetches, cancel clears the resume job
  like the desktop, shutdown keeps it; files are served only from book roots), `books.py` (Single /
  Multi lookup and build / Library update(s) / resume), `options.py` (the browser-editable settings
  whitelist; Polish and NMT only when already installed, never start those downloads from a browser),
  `preview.py`, and `storage.py`.
- `storage.py` (`GET /api/storage`): free/total/used bytes of the tightest disk among the books folders
  and the app data folder, a `level` of ok < 2 GiB or 10% free = low < 500 MiB or 3% = critical, and a
  `where` label; never a path; 503 when no disk can be read. The header shows that as a bar plus "N GB
  free" (`#storage`, `static/storage.js`): fetched on load, every minute while the tab is visible, when
  the tab returns, after a job ends and after Library Remove / Reset; red when low. The reader hides the
  whole header, so it is never shown while reading.
- The browser can set the books folder and install Polish, Offline NMT or an Ollama model (Ollama itself
  must already be installed). Headless checks GitHub and restarts into a verified app update (systemd
  source installs exit 1 so `Restart=on-failure` starts the unit again; otherwise the existing helper
  relaunches and keeps `--headless`); the desktop still asks first. `--install-service` writes
  `~/.config/systemd/user/huaepub.service` only when no HuaEPUB unit is already there.
- Pages and `/static/` are served `Cache-Control: no-cache` so an app update never leaves a phone on
  stale files.

## Browser app (`web/static/`)

No build step; scripts load in order: app, single, multi, library, read, settings, storage, boot.
Keep script-referenced IDs when editing `index.html`.

- **Look:** the "card catalogue". The Library is the home page and each book is a catalogue card (a
  call number from the source URL, one red rule under it, the title in Literata (Play Books' typeface),
  details in Hanken Grotesk, a "+N new" stamp, and the reading position from `reading.json` as
  `read_chapter`; no ruled lines). The detail dialog is the same card enlarged.
- **Library page:** **Continue reading** (the book with the newest `read_at`), search over title /
  Chinese title / author / call number / URL, sort (Recent = the newer of `read_at` and `updated_at`,
  Title, Author, Most left to read, Longest) and a filter (All / Reading / Not started / New chapters).
  Sort and filter persist in `localStorage` (`huaepub-library`); the search does not. Shelve / Put back
  are in the selection bar and the detail. Shelved books leave the main list; **Shelved (n)** is a fifth
  filter in the same bar, shown whenever something is shelved (it is not saved as the remembered
  filter, and putting back the last shelved book returns to All). Action messages go to `#lib-note` (or `#lib-detail-note` while the
  dialog is open), never `#lib-lede`, which every state tick rewrites. The detail's chapter list scrolls
  to the reading chapter and shows a finder past 30 chapters.
- **Rendering:** state ticks only call `drawControls()`; the shelf (`drawShelf()`) is rebuilt only when
  the books, filter or selection change, and shelf cover `<img>`s are reused, or every progress tick
  reloads every cover.
- **Reader, Pages mode:** a chapter is laid out in CSS columns (`--cols` 1 or 2) inside a padded
  `#read-leaf` (so every page has top and bottom margins) and `#read-spread` moves with a transform
  (never `scrollLeft`: the last half-filled spread would clamp). Pages on screen `auto` | `one` | `two`
  (auto = two pages when the leaf is landscape, >= 800 px, and each page still fits ~22 em; `two` falls
  back to one on a narrow phone and `#read-spread-note` says so) and Margins `narrow` | `normal` |
  `wide` (side margin plus a capped line length, `--measure`, so a monitor never gets very long lines)
  are browser-only. The Pages-on-screen chips are disabled in Scroll, and the keys hint matches the flow
  (`only-pages` / `only-scroll`).
- **Display settings** (text size, theme, face, leading, align, flow, pages on screen, margins) are **per
  device** in `localStorage` (`huaepub-reader`). A device without its own starts from the PC's
  `reader_*` settings, and the browser never writes those back (the desktop Read tab keeps its own).
- **Turning pages:** the page follows a horizontal touch drag and eases on (past 20% of the width or a
  quick flick) or springs back (a rubber band at chapter edges); taps, side arrows (fine pointers only),
  keys and the mouse wheel use the same ease (`slideTo`, skipped under prefers-reduced-motion). The
  wheel/touchpad turns one page per notch or swipe (the inertia tail is ignored; ctrl+wheel is left to the
  browser). Keys: ←/→, ↑/↓ (Pages), PgUp/PgDn, Space/Shift+Space, Home/End (chapter start/end), Esc. Scroll
  mode keeps native wheel scrolling, swipe-to-turn and a Next chapter button at the end.
- A resize, rotation or web-font load keeps the place as a ratio in the chapter. The bottom margin says
  how many pages are left in the chapter. Contents and Display are a bottom sheet on phones and a side
  panel from 900 px; Contents opens at the current chapter, has a finder past 30 chapters and lists
  bookmarks. A jump (Contents, bookmark, slider) shows **Back to N** until used. Full screen where the
  browser allows it; the screen wake lock is requested while reading (HTTPS/localhost only) and
  released after 5 idle minutes. Shown chapters stay in memory; an EPUB's next chapter is read ahead
  (cached books are not, so the read-ahead never holds the reader slot).
- **Job dock:** while reading, the dock never covers the text by itself. The reader's top bar shows a
  progress button (`#read-job`, e.g. "Translating 38%") that toggles it as a card under the bar
  (`body.dock-open`). Because it only appears on request it shows the whole job (percent, title, status,
  slips, Fetch/Translate/Write/Save steps, chapter and book counts, full completion notes, file links) in
  the app's own palette, with one button set: Pause, Cancel (greyed out once finished) and Hide. Hide
  and the progress button both close the card; closing a finished job clears it (no Dismiss in the
  reader). Those detail parts are hidden on other pages, and leaving the reader closes it.

## Conventions

- To support a new site, add an object to `parsers/sites.json` (domains + CSS selectors), with optional
  `chapter_list_next` / `content_next`. Keep `generic` registered last. Do not add per-site Python modules
  or a WebToEpub extractor.
- Text cleaning rules live in `core/cleaner.py`; output format changes in `core/epub_builder.py`.
- Status copy and dialogs: see the Download pipeline section. Yes/No popups go through
  `gui/dialogs.py` so Y/N work without Alt.
- Settings writes are atomic (tmp+replace) under one lock for the full read-modify-write.
- Lint is `ruff` with bug-finding rules (pyflakes, bugbear, bare except, stale noqa); keep it clean and put
  a reason on any `# noqa`. Line endings are LF (`.gitattributes`).
- Build: `build.py` bundles `web/` as data plus `web.*` hidden imports and `--collect-submodules=uvicorn`,
  and **regenerates** `HuaEPUB.spec` each build. Do not commit a leftover spec (`*.spec` is gitignored).
  The pin `pyinstaller==6.22.2` must match `requirements-dev.txt` and `release.yml`.
- CI: `ci.yml` runs ruff, then the offline pytest suite on Ubuntu/Windows/macOS x Python 3.11/3.12.
  `release.yml` runs pytest + ruff, then builds the Windows/macOS/Linux zips on a `v*` tag push. It can
  also be run by hand on a branch (`workflow_dispatch`): that builds all three and publishes nothing,
  and the PR checks do not build executables, so use it for any change to the build. Pillow is a
  **build-only** dependency (PyInstaller needs it to make the macOS `.icns` icon); the app does not
  import it, so it lives in `requirements-dev.txt` and the release build step, not `requirements.txt`.
- `tests/` is offline pytest with HTML fixtures (no network), including `sites.json` schema checks and a
  cache-to-EPUB pipeline test. `tests/test_dialogs.py` imports PySide6 and skips collection if the OS
  GL/EGL libraries are missing; Linux CI installs `libegl1` so those tests run.
