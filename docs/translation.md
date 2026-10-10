# Translation

Tick **Translate to English** and pick a **Translator**. Everything below is also summarised
in the app under **Help → How translation works**.

## Engines

| Translator | What it is | Notes |
|------------|------------|-------|
| **Google (New)** *(default)* | The `translate-pa` engine, the same one as the Calibre plugin's "Google (Free) - New" | Use this first. Free and unofficial. |
| Google (HTML) | The widget HTML API | |
| Google (Old) | `translate_a/single?client=gtx` | Walled for many IPs since 2026; still selectable. |
| Microsoft Edge | Another free unofficial engine (same path as the Calibre plugin) | No API key. |
| LibreTranslate | A server you run (or a public one) | More private, usually slower. May pack several paragraphs per call. |
| Ollama | Full translation by a local model | Slow (hours for a long novel). Needs [Ollama](https://ollama.com) running. |
| Offline NMT | A local CTranslate2 model (opus-mt-zh-en) | Free and offline. Optional install, see below. |

Google and Microsoft are unofficial endpoints, so chapter text is sent to those services while
you translate. LibreTranslate sends it to the server you configured. Ollama, Offline NMT and
Polish English never leave your PC.

### Workers and rate limits

**Translation Workers** is a *ceiling*, not a promise. Unofficial Google and Microsoft rate-limit
by IP address, so HuaEPUB starts at 8 requests in flight and climbs by one for each success. When
a request is answered with HTTP 429 it **pauses all new requests** so the IP can recover (floor of 2
in flight), instead of letting the other workers keep hammering. If you still see 429s, lower the
ceiling (try 16 to 32).

Small machines are capped by what they can run: 24 threads under 1 GB of RAM, 64 under 2 GB, and
never more than a quarter of the process limit. A Raspberry Pi Zero 2 W ran out of threads at the
default 200 before this cap existed.

If some segments keep failing, HuaEPUB gives up after a few retry passes, keeps the best text it
has, and still builds the EPUB. Failed or echoed Chinese is never stored as a cache hit, so a
second run retries it. **Help → Cache…** can clear translations for a clean slate.

## Glossary

The glossary protects names and terms so they aren't translated literally. It is **Off by default**:
names come straight from the translator.

- **Auto** attaches the built-in cultivation pack only when the title or early chapter titles look
  like xianxia or wuxia (strong markers such as 修仙, 金丹 or 灵根, not 公子 alone). Romance and urban
  books skip it. The pack is a curated web-novel list, not a general Chinese dictionary, because
  pinning everyday words would wreck sentences.
- **Cultivation pack** always applies the pack. **Names only** uses just your own terms and the
  names learned from the book. **Off** leaves everything to the translator.
- Your terms live in `~/.huaepub/glossary.json` and always apply unless the glossary is Off.
- During translation HuaEPUB **mines names, sects and techniques** from the book's own Chinese and
  romanises them with pinyin (not Google) into `~/.huaepub/glossaries/<title>.json`.
- If the Polish Qwen model (7B or larger) is already on disk, **Help → Polish glossaries with
  Qwen…** classifies those candidates and shows **Accept all** or **Discard**. It never starts a
  model download itself.

Changing the glossary mode changes the cache keys, so a book is translated again once.

## Polish English

Keep the Translator on Google (or LibreTranslate) and tick **Polish English**. Google still
translates; a local language model then copy-edits the awkward English in the same EPUB.

- **Ollama is not required.** The first polish run downloads
  [llama.cpp](https://github.com/ggml-org/llama.cpp) (`llama-server`) and a **Qwen2.5 Instruct**
  GGUF into `~/.huaepub/polish` (about 2 to 9 GB; a dialog explains this the first time you tick
  it). Later runs reuse it.
- The model size follows your hardware: roughly **3B** on CPU or low VRAM, **7B** on mid-range
  GPUs, **14B** with plenty of VRAM (NVIDIA CUDA, AMD Vulkan or Apple Silicon).
- Only awkward spans go to the model. Fluent sentences, titles and leftover Chinese are copied
  as they are, and polished spans are reused from the translations table in `cache.db`.
- If llama.cpp is already listening on `:8080` (or vLLM on `:8000`) that server is used. If Ollama
  is holding the GPU so llama.cpp can't start, quit Ollama from the tray and retry.
- Cancelling during polish still **saves** the EPUB with the machine translation.
- Progress and errors are in `~/.huaepub/logs/huaepub.log` (File → Open log file).

Polish is greyed out when Translate is off or the Translator is Ollama (that model already
rewrites the whole book).

## Offline NMT

Pick **Translator → Offline NMT** for a free local engine. It isn't bundled in the prebuilt app:

```bash
pip install -r requirements-nmt.txt   # ctranslate2 + sentencepiece + CUDA 12 libraries
```

The first **translate** pass (after the chapters are fetched) downloads Helsinki-NLP
**opus-mt-zh-en** (about 320 MB) into `~/.huaepub/nmt/`, once rather than per chapter. Quality is
below Google plus Polish; run Polish English after it if you want a copy-edit. The model stays on
this PC.

### NVIDIA GPU

CTranslate2 needs the **CUDA 12** libraries, especially `cublas64_12.dll`. The Game Ready driver
is not that, and CUDA 13 is the wrong major version. cuDNN is not needed.

1. Use the same Python that launches `app.py`.
2. Install the CUDA 12 wheels (`requirements-nmt.txt` pulls them too):
   ```bash
   python -m pip install nvidia-cublas-cu12 nvidia-cuda-runtime-cu12
   ```
3. **Fully quit and reopen** HuaEPUB; an already-running process won't see new DLLs.

Still failing? On Windows, `winget install Nvidia.CUDA --version 12.9` (the runtime is enough).
On Linux install CUDA 12.x from NVIDIA or your distro and reboot. macOS has no Metal backend for
CTranslate2, so Offline NMT runs on the CPU there. An Ollama install's `cuda_v12` folder is used
automatically. If CUDA 12 still can't load, the app falls back to **CPU Offline NMT**, not Google,
and prints these same steps in the log.

## Ollama

Ollama is only for **full** on-PC translation (no Google), and it is much slower than Google plus
Polish. Install it from [ollama.com](https://ollama.com). The suggested model is **`qwen2.5:3b`**
(Apache-2.0, strong Chinese to English, about 2 GB, usable on CPU); an untagged `qwen2.5` can pull
a much larger build.

```bash
ollama list                  # what you already have
# or let HuaEPUB offer to download qwen2.5:3b when you pick Ollama
```

Set **Translator** to **Ollama** and use 1 to 4 workers. Translations are cached.
