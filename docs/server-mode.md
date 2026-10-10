# Server mode

Server mode turns HuaEPUB into a small web app you open in a browser: on your phone, on another
computer, or on this PC. It's the same app, library, cache and reading position, with a layout
built for touch. It can run beside the desktop window or **headless** on a machine with no
screen, such as a Raspberry Pi.

Click **SERVE** at the right end of the tab bar (or **File → Server mode…**) and choose where to
serve:

| | This network | Anywhere |
|---|---|---|
| Who can reach it | Devices on your home or office network only | Any device on the internet (your router must forward the port to this PC) |
| Connection | HTTP | HTTPS only |
| Sign-in | An 8-character access code, or scan the QR code | A password you set (at least 10 characters) |
| Session length | 30 days | 7 days |

While serving, the desktop window shows the addresses, the code or password status, a QR code,
what the browser is doing right now, **Open in browser** (signs this PC's browser in with a
one-time link) and **Stop serving**. The desktop tabs are paused so the browser and the window
never run jobs on the same library at once; on this PC you use the browser too.

HuaEPUB remembers that server mode was on and starts serving again the next time it opens. If the
port is taken it says so and opens the desktop app instead.

## The browser app

### Library

The home page. Each book is a catalogue card: a call number from its source link, the English and
Chinese titles, the author, the chapter count, the chapter you're on, and a red **+N new** stamp
after **Check for updates**.

- **Continue reading** at the top reopens the book you read last, where you left off.
- **Search** by title (English or Chinese), author or site; press **/** to jump to the box.
- **Sort** by Recent, Title, Author, Most left to read or Longest, and **show** All, Reading,
  Not started or New chapters. Your sort and list are remembered in that browser.
- Tap a card for its synopsis, chapter list (opened at your chapter, with a finder for long books)
  and actions: Read, Update, Download EPUB, Open link, Shelve, Remove. Tick cards to update, download
  or remove several at once.
- **Shelve** is for a book you gave up on. It stays in the Library under **Shelved books** with its
  files kept, and is never checked for updates. Looking it up again on **Add a book** or
  **Add several** says you shelved it, either by its link or by the same Chinese title on another
  site; Add several leaves it out of the build. **Put back** returns it.

### Add a book and Add several

The Single and Multi downloads. Paste a link, look it up, pick the chapter range, build.

### Read

The same position (`reading.json`) as the desktop reader.

- **Pages** mode turns pages like Play Books: the page follows your finger and slides on, and taps
  on the edges, the arrow keys and the mouse wheel turn pages too. **Scroll** mode is one long
  column.
- **Display** sets the theme, text size, typeface, line spacing, alignment, flow, **Pages on
  screen** (Auto, One page, Two pages) and margins. They're saved per device, so a phone and a
  monitor can differ. **Pages on screen** only applies in Pages mode, so it's greyed out in Scroll;
  **Two pages** on a screen too narrow for two shows one page and says why.
- **Contents** opens at the current chapter and lists your bookmarks; a jump shows **Back to N**
  until you use it. **Bookmark**, **Full screen**, and the screen stays awake while you read (on
  HTTPS or this PC).
- While a download runs, a progress button in the top bar ("Translating 38%") opens a card with the
  whole job (steps, counts, notes, files) and **Pause / Cancel / Hide**. It never covers the text on
  its own.

### Settings

Translate, clean, cache, translator, glossary, workers, Polish and the books folder, plus
installing Polish, Offline NMT or an Ollama model (Ollama itself must already be installed).

### Header

Next to the version, the header shows how much **disk space** is left on the tightest disk the
server writes to, and how much **memory** is available, so a Pi doesn't fill up unnoticed. It turns
red when space or memory runs low. Only sizes are shown, never paths.

Finished EPUBs are saved in the books folder on the computer that's serving and added to the
Library; the browser can also save a copy to the device's Downloads folder. You can keep reading a
cached chapter while a download runs. A missing chapter from the same site waits until that download
finishes. One download runs at a time, and an unfinished download shows a **Resume** banner just
like on the desktop.

## Running without a window

On a machine with no desktop (a Raspberry Pi over SSH, for instance):

```bash
pip install -r requirements.txt
python3 app.py --headless
```

It prints the address and the access code. Under a service the code is not printed; it's in
`~/.huaepub/server/secret.json`.

### Staying up to date

About 20 seconds after it starts, and then every six hours, a headless server checks GitHub for a
newer release. A verified update is installed and the process restarts, unless someone has a book
open or a download is running, in which case it waits and tries again. The desktop app still asks
first. In the desktop app, **Help → Auto-check updates on startup** turns the automatic check off.

### As a service

The browser's Settings can install a systemd **user** service when none is installed, and
`python3 app.py --install-service` does the same and exits. An existing `huaepub.service` or
`noveldownloader.service` is left as it is. Under the service (`Restart=on-failure`) an update makes
the process exit with a failure status on purpose, and systemd starts it again on the new version, so
`--headless` stays.

## Security

Everything is built in; there's no account and no outside service.

- **This network:** only loopback and private-network addresses are answered, and the `Host` header
  must be an IP address or `localhost` (which stops DNS-rebinding tricks). **New code** signs every
  device out.
- **Anywhere:** HTTPS with a certificate HuaEPUB makes for you (browsers warn the first time;
  compare the SHA-256 fingerprint shown on the PC) or your own certificate and key. The password is
  stored as a salted scrypt hash, and changing it signs every device out.
- **Sessions** are signed cookies (HttpOnly, SameSite=Strict, Secure over HTTPS). Every change needs
  a same-origin request with a custom header. After five wrong codes or passwords one address waits
  up to 15 minutes, and 30 failures from anywhere within 10 minutes pause all sign-ins for 10 minutes.
- **Files:** the server hands out EPUBs only from your books folders.
- **Find my public address** in the dialog asks `api.ipify.org` once, and only when you click it.
- The access code, password, cookies and one-time links are never written to the log.
