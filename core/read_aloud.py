# Author: joelsnl
"""
EPUB 3 Media Overlays for Google Play Books Read Aloud.

Calibre's "Add Text-to-speech narration" bakes Piper audio into the EPUB.
That is the wrong fit here: a novel-length MP3/M4A overlay is hundreds of MB,
over Google Play Books' ~100 MB personal-upload limit, and Play Books ignores
embedded overlay audio on uploaded files anyway (it synthesizes with TTS).

What Play Books *does* use:
- reflowable EPUB 3 with real text (not images / fixed layout)
- dc:language / xml:lang so the TTS voice matches the book
- Media Overlay SMIL that maps playing fragments to element ids (highlighting)

So we wrap sentences (or whole blocks) with ids and emit TTS-only SMIL
(text pars, no audio files). Duration is estimated from word/character
count so the OPF stays spec-valid.
"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape as xml_escape

from bs4 import BeautifulSoup, NavigableString, Tag

Fragment = Tuple[str, str]  # (element id, spoken text)

_BLOCK_TAGS = frozenset({"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"})
_INLINE_OK = frozenset({"br", "wbr"})

# End of a sentence: Western or CJK punctuation, optional closing quotes,
# then whitespace, a CJK char, a new English sentence, or EOS.
_SENTENCE_END = re.compile(
    r'([.!?…]+|[。！？]+)(["\'”’)\]]*)(?:\s+|(?=(?:[\u4e00-\u9fff]|[A-Z“"]|$)))'
)

_LANG_MAP = {
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh-hans": "zh-CN",
    "zh-sg": "zh-CN",
    "zh-tw": "zh-TW",
    "zh-hant": "zh-TW",
    "zh-hk": "zh-TW",
    "en": "en",
    "en-us": "en",
    "en-gb": "en",
}

ACTIVE_CLASS = "-epub-media-overlay-active"
NARRATOR_LABEL = "Text-to-speech"


def epub_language_code(raw: Optional[str], *, translated: bool = False) -> str:
    """BCP-47 tag for dc:language / xml:lang. Play Books picks TTS from this."""
    if translated:
        return "en"
    key = (raw or "zh").strip().lower().replace("_", "-")
    if key in _LANG_MAP:
        return _LANG_MAP[key]
    if key.startswith("zh-hans"):
        return "zh-CN"
    if key.startswith("zh-hant"):
        return "zh-TW"
    if key.startswith("zh"):
        return "zh-CN"
    if key.startswith("en"):
        return "en"
    return (raw or "en").strip() or "en"


def split_sentences(text: str) -> List[str]:
    """Split a block into TTS-sized fragments. Best-effort, not linguistic."""
    text = (text or "").strip()
    if not text:
        return []
    sentences: List[str] = []
    start = 0
    for match in _SENTENCE_END.finditer(text):
        piece = text[start:match.end()].strip()
        if piece:
            sentences.append(re.sub(r"\s+", " ", piece))
        start = match.end()
    tail = text[start:].strip()
    if tail:
        sentences.append(re.sub(r"\s+", " ", tail))
    return sentences or [re.sub(r"\s+", " ", text)]


def estimate_narration_seconds(text: str, lang: str) -> float:
    """Rough spoken duration so media:duration is present and non-zero."""
    lang = lang or ""
    if lang.startswith("zh"):
        n = len(re.findall(r"[\u4e00-\u9fff]", text))
        if n:
            return max(0.4, n / 4.5)  # ~270 chars/min
    words = len(re.findall(r"[A-Za-z0-9']+", text))
    if words:
        return max(0.4, words / 2.5)  # 150 wpm
    chars = len(re.sub(r"\s+", "", text or ""))
    return max(0.4, chars / 12.0) if chars else 0.4


def smil_clock(seconds: float) -> str:
    """SMIL Full-clock-value (H:MM:SS.mmm), hours unbounded."""
    total_ms = max(0, int(round(float(seconds) * 1000)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours}:{minutes:02d}:{secs:02d}.{ms:03d}"


def _is_simple_block(el: Tag) -> bool:
    for child in el.children:
        if isinstance(child, NavigableString):
            continue
        if isinstance(child, Tag) and child.name in _INLINE_OK:
            continue
        return False
    return True


def mark_narration_fragments(html: str, id_prefix: str) -> Tuple[str, List[Fragment]]:
    """
    Add id= attributes Play Books / SMIL can highlight.

    Simple paragraphs (text + br only) are split into sentence spans.
    Nested markup keeps a single id on the block so we do not break tags.
    """
    soup = BeautifulSoup(html or "", "lxml")
    root = soup.body if soup.body is not None else soup
    fragments: List[Fragment] = []
    n = 1

    blocks = [el for el in root.find_all(_BLOCK_TAGS) if isinstance(el, Tag)]
    if not blocks:
        text = root.get_text(separator=" ", strip=True)
        if not text:
            return html or "", []
        p = soup.new_tag("p")
        if soup.body is not None:
            soup.body.clear()
            soup.body.append(p)
        else:
            root.clear()
            root.append(p)
        blocks = [p]
        p.string = text

    for el in blocks:
        text = el.get_text(separator=" ", strip=True)
        if not text:
            continue
        if _is_simple_block(el):
            sentences = split_sentences(text)
            el.clear()
            for i, sent in enumerate(sentences):
                sid = f"{id_prefix}-{n:04d}"
                n += 1
                span = soup.new_tag("span", attrs={"id": sid})
                span.string = sent
                el.append(span)
                if i < len(sentences) - 1:
                    el.append(" ")
                fragments.append((sid, sent))
        else:
            sid = el.get("id") or f"{id_prefix}-{n:04d}"
            n += 1
            el["id"] = sid
            fragments.append((sid, text))

    if soup.body is not None:
        inner = "".join(str(child) for child in soup.body.children)
    else:
        inner = str(soup)
    return inner, fragments


def build_smil(xhtml_href: str, fragments: Sequence[Fragment]) -> str:
    """TTS-only Media Overlay (text pars; audio is synthesized by the reader)."""
    href = xml_escape(xhtml_href)
    pars = []
    for i, (elem_id, _text) in enumerate(fragments, 1):
        src = f"{href}#{xml_escape(elem_id)}"
        pars.append(
            f'      <par id="par-{i}">\n'
            f'        <text src="{src}"/>\n'
            f"      </par>"
        )
    body = "\n".join(pars)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<smil xmlns="http://www.w3.org/ns/SMIL" '
        'xmlns:epub="http://www.idpf.org/2007/ops" version="3.0">\n'
        "  <body>\n"
        f'    <seq epub:textref="{href}" epub:type="chapter">\n'
        f"{body}\n"
        "    </seq>\n"
        "  </body>\n"
        "</smil>\n"
    )


def overlay_css() -> str:
    return f"""
.{ACTIVE_CLASS},
span.{ACTIVE_CLASS} {{
    background-color: #ffe08a;
}}
"""
