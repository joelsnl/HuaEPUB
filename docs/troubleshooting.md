# Troubleshooting

The log is the first place to look: **File → Open log file**
(`~/.huaepub/logs/huaepub.log`). Crashes are dumped to `huaepub.fault.log` next to it.

## Downloading

### "Could not extract book ID"
Use the book's main page (its table of contents), not a chapter, and check that the site is
supported ([supported sites](../README.md#supported-sites)). Unlisted sites use a best-effort
generic parser.

### A chapter looks wrong or is full of site text
If a configured site's content selector misses, the generic heuristic is used and the completion
dialog warns you (**Saved with warnings**). That site's entry in `parsers/sites.json` probably
needs a new selector ([development.md](development.md#adding-a-site)).

### The EPUB won't open
Try another reader (Calibre is a good test). Check whether the title has unusual characters.

## Translation

### "Translation failed" or lots of 429s
Unofficial Google and Microsoft rate-limit by IP. HuaEPUB already starts slow and backs off, but
if it still struggles, lower **Translation Workers** to 16 to 32. Switching **Translator** to
Google (New) usually helps, since Google (Old) is walled for many IPs. See
[translation.md](translation.md#workers-and-rate-limits).

### Part of a finished book is still in Chinese
Run the translation again. Failed Chinese is not kept as a cache hit, so only the missing
segments are retried. **Help → Cache…** can clear translations if you want a clean slate. After
a few retry passes HuaEPUB stops and builds the EPUB with the best text it has rather than hanging.

### Polish English is greyed out
Turn on **Translate to English** and set **Translator** to Google or LibreTranslate. With Ollama
the model already translates locally, so there is nothing to polish.

### Polish never starts
The log will say why. If it mentions the GPU being busy, quit **Ollama** from the tray and retry.
Polish needs a download on first use (llama.cpp and a Qwen model, 2 to 9 GB into
`~/.huaepub/polish`); it does not need Ollama. See [translation.md](translation.md#polish-english).

### Offline NMT stays on the CPU
It needs the CUDA 12 libraries. See [translation.md](translation.md#nvidia-gpu).

## Pausing, cancelling, resuming

### No Resume banner after closing mid-download
Check that the download had really started (chapters were being fetched) and that
`~/.huaepub/active_download.json` exists. If you clicked **Cancel**, the resume point was cleared on
purpose. Cached chapters still help if you fetch the same book again with the cache on.

### Cancel wrote no EPUB, or wrote one
That's by design. Cancelling during translation writes nothing, so a half-translated book is never
saved. Cancelling during polish saves the EPUB with the machine translation. Cached chapter text is
kept either way.

## Cache and disk

### The cache is huge, or old chapters disappeared
The default cap is 2 GB. Over the cap the oldest stored chapter HTML goes first; translations are
kept unless the file is still over the limit. Nothing is cleared on a timer. **Help → Cache…**
raises the cap, sets it to unlimited, or clears chapters or everything.

### A Raspberry Pi or small server runs out of space or memory
The browser app's header shows free disk and memory and turns red when either runs low. Free space
by removing books from the Library or clearing the cache. On very small boards the translation
worker count is capped automatically.

## Updates

### The app does not reopen after an update
- After **Restart now**, let the app quit and don't force-quit the helper; it reopens HuaEPUB itself.
  If it doesn't, start `HuaEPUB` from the folder you installed into.
- Builds before 2.10.1 could fail to reopen after updating. Install a current release by hand once to
  get past that.
- If it still fails, download the zip for your OS from
  [Releases](https://github.com/joelsnl/HuaEPUB/releases) and replace the binary yourself. On macOS,
  clear quarantine if Gatekeeper blocks it: `xattr -cr /path/to/HuaEPUB`.

### Update refused, or a checksum error
The release may be incomplete or the download corrupted. Try again later, or install the zip from
Releases by hand after checking it against `SHA256SUMS.txt`. HuaEPUB never installs an update it
can't verify.

## Server mode

### The browser says the server only answers its own network
In **This network** mode only devices on private addresses are served, and the address in the
browser must be an IP address or `localhost`. Use the address shown on the desktop's server screen.

### I forgot the access code or password
In **This network** mode click **New code** on the desktop's server screen; in **Anywhere** mode set a
new password in the server dialog (**File → Server mode…**). Either signs every device out. On a
headless machine the code is in `~/.huaepub/server/secret.json`.

### The port is already in use
HuaEPUB says so and opens the desktop app instead. Pick another port in the server dialog, or stop
whatever else is using it.
