# Development

How to run, test, change and release HuaEPUB. For how the pieces fit together read
[architecture.md](architecture.md) first. The detailed rules for contributors (and for AI coding
assistants) are in [CLAUDE.md](../CLAUDE.md).

## Setup

```bash
git clone https://github.com/joelsnl/HuaEPUB.git
cd HuaEPUB
pip install -r requirements.txt -r requirements-gui.txt -r requirements-dev.txt
python app.py              # the desktop app
python app.py --headless   # server mode with no window (requirements.txt is enough)
```

Python 3.10 or newer. `requirements-nmt.txt` is optional (offline translation).

## Tests and lint

```bash
python -m pytest tests/
python -m ruff check .
```

The suite is fully **offline**: HTML fixtures, fake translators, a fake clock where timing
matters. No network is needed. Qt tests run with the offscreen platform and skip themselves if
the OS graphics libraries are missing (CI installs `libegl1` on Linux so they run).

Ruff catches bugs rather than style: unused or undefined names, bugbear's likely-bug rules, bare
`except`, and stale `# noqa` markers (see `[tool.ruff.lint]` in `pyproject.toml`). Formatting is not
enforced. Put a short reason on any `# noqa` you add.

CI (`.github/workflows/ci.yml`) runs lint, then the tests on Ubuntu, Windows and macOS with
Python 3.11 and 3.12. Changes go to `main` through a pull request that passes those checks.

Line endings are LF everywhere (`.gitattributes`, `.editorconfig`).

## Project layout

```
app.py                    Entry: desktop window, or --headless server
build.py                  PyInstaller build (regenerates HuaEPUB.spec; never commit a spec)
VERSION                   The release number. The only place it is written.
core/                     The engine. Qt-free. Never imports gui or web.
  parser.py               Base parser, Chapter/NovelInfo, the parser registry, HTTP session
  cleaner.py, ad_detect.py   Watermark/ad removal; learns repeating site junk
  epub_builder.py         EPUB creation (atomic write); read_aloud.py adds Play Books overlays
  download_runner.py      Pause/cancel, the chapter loop, translate_then_build
  translation_progress.py Status text, ETAs and retry-pass messages while translating
  tasks.py                Single / Multi / Library-update jobs shared by desktop and server
  session.py              Shared state: settings, cache, library, download control
  translator.py           GoogleTranslator: runs a translation job (throttle, retry, progress)
  translator_engines.py   One request method per engine (Google, Edge, LibreTranslate, Ollama)
  translator_cache.py     What may be cached; in-memory and SQLite helpers
  gtx_throttle.py         The per-IP in-flight throttle
  translation/            Glossary, offline NMT, NovelTranslator (the full pipeline)
  polish/, local_polish.py   Optional local copy-edit through llama.cpp
  cache.py                SQLite: chapters, translations, covers, chapter lists
  library.py, library_check.py   Tracked library, update checks, shelving
  reader.py, reading.py   Opening a book for the reader; reading positions and bookmarks
  download_job.py         The resume point (active_download.json)
  settings.py, atomic_io.py, logger.py, notify.py, branding.py, utils.py
  security.py             SSRF guards, safe archive extraction, secret files
  updater.py, update_scripts.py   Release check, verified install, relaunch helpers
parsers/                  sites.json + one parser that reads it, pagination, generic fallback
gui/                      PySide6 desktop window
  main_window.py          The window; its behaviour is split into mixins in window/
  window/                 Worker host, reader, library, server, look, glossary, resume, update
  pages/, widgets/, workers/, dialogs.py, theme.py + style.qss, help_content.py
web/                      Server mode. Never imports gui or Qt.
  server.py               Wires the FastAPI app together
  guard.py                Network, Host, CSRF, session checks and security headers
  *_api.py                Routers: auth, task, download, settings, library, reader
  tasks.py, context.py, auth.py, tls.py, host.py, books.py, options.py, storage.py, preview.py
  static/                 The browser app: plain HTML, CSS and JavaScript, no build step
tests/                    Offline pytest suite
tools/                    make_demo_home.py (demo library for screenshots), bench_google_gtx.py
docs/                     This documentation and the README screenshots
packaging/, snapcraft.yaml   Linux packaging descriptors (AppImage script, Flatpak manifest,
                          AppStream metainfo, desktop file, snap). CI does not build these.
```

## Adding a site

Add an object to `parsers/sites.json`. The first matching `domains` entry wins and the generic
parser stays last as a heuristic fallback. Don't add per-site Python modules.

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

Optional fields: `description`, `cover`, `chapter_title`, `remove`, `toc_link`, `reverse`, `delay`,
`encoding`, `language`, `book_id` (a regex), `chapter_list_url` (may include `{book_id}`),
`chapter_href_contains`, `referer`, `visit_toc_first`, `chapter_list_next` (CSS selector for the next
table-of-contents page) and `content_next` (CSS selector for the next *page of this chapter*, not
"next chapter").

Configured sites follow only the pagination keys you set; the generic parser follows `rel=next` on
tables of contents and 下一页 / "next page" on chapter bodies. If a configured `content` selector
misses, the density heuristic is used, `Chapter.used_heuristic` is set, and the completion dialog
warns the user. `tests/` includes schema checks for `sites.json`.

Parsers must **raise** when content extraction fails; they never return placeholder HTML.

## Conventions worth knowing

- Chapter downloads are **sequential** on purpose (a per-site delay avoids bans). Don't
  parallelise them. Translation is concurrent, behind the throttle.
- `core/` never imports `gui` or `web`; `web/` never imports `gui` or Qt. Tests enforce it.
- Worker threads must reach the UI through `@Slot` methods on the main window, never bare lambdas.
- Settings and other state files are written atomically (`core/atomic_io.py`).
- Fetches of pages, covers and LibreTranslate go through `safe_http_request` so redirects and
  private addresses are checked.
- Product strings (name, data folder, executable) live in `core/branding.py`.
- Keep the docs in step with the code. A test checks that every link and file path in the docs
  still exists.

## Releasing

1. Bump the number in `VERSION` (it is read by the updater; don't copy it anywhere else).
2. Commit, open a pull request, and merge when CI is green.
3. Tag the merge commit `vX.Y.Z` and push the tag.

Before tagging a change that touches the build (`build.py`, the requirements, the workflow), run the
**Build Release** workflow by hand on your branch (Actions, Build Release, Run workflow). It builds
all three platforms and publishes nothing, so a build failure shows up before a tag does. The pull
request checks do not build executables.

The tag starts `.github/workflows/release.yml`, which runs lint and the tests first and only then
builds the Windows, macOS and Linux binaries with the pinned PyInstaller. The release gets
`HuaEPUB-windows.zip`, `HuaEPUB-macos.zip`, `HuaEPUB-linux.zip`, `HuaEPUB-source.zip` and
`SHA256SUMS.txt`. The in-app updater verifies downloads against that checksum file and **refuses to
install without a match**, so all of those assets must be published.

## Screenshots

`python tools/make_demo_home.py <folder>` builds a demo `~/.huaepub` of invented novels (painted
covers, one readable book, reading positions). Point `HOME` / `USERPROFILE` at that folder to run
the app on it. The README screenshots come from there; never screenshot a real library.
