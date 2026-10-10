# User guide

How to use the desktop app. Translation engines and the glossary are in
[translation.md](translation.md); using HuaEPUB from a browser or a phone is in
[server-mode.md](server-mode.md).

HuaEPUB has four tabs: **Single**, **Multi**, **Library** and **Read**. The options under the
tabs (translate, clean, cache, workers, save folder) apply to downloads in every mode and are
remembered between sessions. The **SERVE** chip at the right of the tabs turns on
[server mode](server-mode.md).

## Quick start: one novel

1. Stay on **Single**.
2. Paste the novel's **table of contents** link: the main book page, not a single chapter.
3. Click **Fetch Chapters**.
4. Choose the chapters you want, or leave all selected. **Select All / None / Invert**, or a
   **Range** (for example from `200` to `450`).
5. Click **Download EPUB**. The status names the phase: fetching chapters, translating,
   polishing, writing the EPUB. The button stays disabled while a job runs, so a second run can't
   start on top of the first.
6. The EPUB is in your save folder (default `~/.huaepub/books`). **Read** after
   **Fetch Chapters** previews from the cache before you build anything; **Preview** on the
   completion dialog opens the new EPUB in the Read tab.

**Recent** reopens a link from an earlier download. If anything looks off (leftover Chinese, a
generic content guess, or polish stopped early), the dialog title is **Saved with warnings**
instead of **Success**.

## Options

| Option | What it does |
|--------|--------------|
| Remove watermarks & ads | Strips site junk, and learns repeating ads from the first chapters (independent of Polish). |
| Translate to English | Machine-translates the text while the EPUB is built. |
| Use chapter cache (resume) | Reuses chapters already saved on this PC. Keep it on unless you want a full re-download. |
| Watch clipboard for URLs | Copied novel links are queued into Multi (and fill Single if it's empty). |
| Translator | Google (New, the default), Google (HTML), Google (Old), Microsoft Edge, LibreTranslate, Ollama or Offline NMT. See [translation.md](translation.md). |
| Polish English | A local copy-edit after Google or LibreTranslate. Greyed out when Translate is off or the Translator is Ollama. |
| Translation Workers | The ceiling for Google requests in flight (default 200). It starts at 8 and climbs on success, and the machine's own limit applies on small devices. |
| Ollama model / URL | Shown only when the Translator is Ollama. The URL must be localhost. |
| Save to | Where EPUB files are written. |
| Cache size | **Help → Cache…** (default 2 GB; `0` keeps everything). |

## Multi mode

1. Open **Multi**.
2. Paste several links, one per line or as a block of text containing links.
3. **Fetch All**, then **Download All**.

The novels run one after another, and you can pause, cancel and resume the queue exactly as in
Single. **Preview** on the completion dialog opens a finished EPUB (pick which one if several
succeeded).

## Library

After you download a novel it appears in **Library** so you can update it later.

1. Choose **Grid** (covers) or **List** (compact table).
2. **Check updates**: each book shows `Checking…`, `N new`, `Up to date` or an error under its
   title, and thumbnails are refreshed from the site.
3. Filter **All** or **Updates** (books with new chapters).
4. Select books with **Select All / None / Invert**, or Ctrl/Shift-click.
   - **Read** (or double-click / Enter) opens the current book in the Read tab.
   - **Update** rebuilds a full EPUB for every selected book. Old chapters come from the cache, so
     only new chapters are fetched. The ETA counts only chapters that need the network, so a
     500-chapter book with 3 new chapters never shows "ETA 0s".
   - **Update All** updates every book the last check flagged, regardless of the selection.
   - **Open URL** uses the current book. **Download EPUB** and **Remove** apply to the whole
     selection. **Remove** deletes the local EPUB, that book's chapter/cover/TOC cache and its
     reading position; the shared translation cache is kept.

The browser app's Library has more: search, sorting, **Continue reading** and **Shelve** for
books you gave up on. See [server-mode.md](server-mode.md#library).

## Pause, cancel and resume

Long downloads can take hours. You don't have to leave the PC on the whole time.

- **Pause** stops between chapters; **Resume** continues. It's safe to close the app while paused.
- **Closing the app** or shutting down mid-download keeps the progress locally
  (`cache.db` and `active_download.json`). On the next launch a banner offers **Resume** or
  **Discard**; resuming continues from the cached chapters.
- **Cancel** clears the resume point (cached chapter text stays on disk):
  - during fetching or translation the run **aborts and writes no EPUB**, because a
    half-translated book is never saved;
  - during polish the EPUB **is** written with the machine translation (sentences already polished
    are kept), and the completion dialog says so.

Resume data is local only.

## The reader

**Read** opens the local EPUB when it is in your books folder; otherwise it uses the cached table
of contents and chapter HTML (usually the original site text, not the translated EPUB). The badge
at the top says **EPUB** or **Cached** so that isn't a surprise.

A chapter missing from the cache is fetched on demand, one at a time with the site delay, and saved
to the cache. That never rebuilds an EPUB or runs translation or polish. If a download is already
running, the fetch waits and says so. With Translate on, a cached Chinese chapter is translated as
you open it.

Use **Prev / Next**, **A- / A+** (or the slider). The font size is remembered. Your place (chapter
and scroll) is stored in `~/.huaepub/reading.json` on this PC. When a Library update rebuilds a
book, the reader reopens at the same chapter and page even if the site inserted chapters before
it. Removing a book also clears its position.

## Where files live

| Path | Contents |
|------|----------|
| `~/.huaepub/books/` | Default EPUB output |
| `~/.huaepub/library.json` | Tracked library and recent history |
| `~/.huaepub/cache.db` | Chapter HTML, translations (including polished spans), covers and chapter-list snapshots. Default 2 GB cap |
| `~/.huaepub/settings.json` | App options |
| `~/.huaepub/active_download.json` | The resume point of an unfinished download (if any) |
| `~/.huaepub/reading.json` | Reader positions and bookmarks |
| `~/.huaepub/glossary.json` | Your own glossary terms |
| `~/.huaepub/glossaries/` | Per-book terms, including names harvested from that book |
| `~/.huaepub/glossary-qwen.json` | Older extra terms (read only for cultivation books) |
| `~/.huaepub/polish/` | llama.cpp and the Qwen model for Polish English |
| `~/.huaepub/nmt/` | The optional offline translation model |
| `~/.huaepub/server/` | Server mode: signing key, access code, password hash, generated HTTPS certificate (owner-only) |
| `~/.huaepub/logs/huaepub.log` | Diagnostics (rotates at 1 MB, keeps `.log.1`) |
| `~/.huaepub/logs/huaepub.fault.log` | Native crash dumps |

On Windows `~` is your user folder (for example `C:\Users\YourName`). Older installs migrate
automatically from `~/.noveldownloader/`. Nothing in this table leaves your PC, except the text a
translation engine is sent when Translate is on.

## The cache

The cache is not cleared on a timer. It's capped (default 2 GB); over the cap, the oldest stored
chapter HTML goes first, then covers, chapter lists and, last, translations. **Help → Cache…**
raises the cap, sets it to unlimited, or clears chapters, translations or everything.

## Menus and keys

- **File**: Open books folder, Open data folder, Open log file, **Server mode…**, Exit.
- **View → Look**: Auto follows your system (Catalogue at night when dark, Catalogue when light), or
  pick Indigo & Jade, Ink & Gold, Cinnabar Night, Blue Mist or Surprise me. The browser app uses the
  same palettes.
- **Library**: Check for updates, Reset library…
- **Help**: Check for updates, Auto-check updates on startup, How translation works…, Polish glossaries
  with Qwen…, **Cache…**, About.
- **Y** / **N** answer Yes/No dialogs (the underlined letters aren't Alt-only). There are no other
  app shortcuts. The window size and position are remembered.

## Updating the app

**Help → Check for updates** (or the automatic check at startup) offers a new release. An update is
installed only if the downloaded file matches the release's `SHA256SUMS.txt`; a missing or wrong
checksum stops it. Accepting hides the window behind one progress dialog with the real download
size, then **Restart now** closes the app so a helper can replace the files and reopen it, on every
OS. If it doesn't reopen, see [troubleshooting.md](troubleshooting.md#the-app-does-not-reopen-after-an-update).

## Tips

- Prefer the **main book page** link from a supported site.
- For overnight runs: start the download, then **Pause** or just close the app when you need the PC
  off, and **Resume** later.
- If translation is rate-limited, lower **Translation Workers** (for example 30 to 50) and keep the
  cache on.
