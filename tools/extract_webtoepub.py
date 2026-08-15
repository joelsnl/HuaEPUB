#!/usr/bin/env python3
"""
Extract CSS-selector site specs from WebToEpub parser JS files.

Usage:
  python tools/extract_webtoepub.py path/to/WebToEpub/plugin/js/parsers

Writes parsers/webtoepub_sites.py (imported by parsers.selector).
Does not copy JavaScript — only hostnames and CSS selectors.
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

WP_CONTENT = [
    "div.entry-content",
    "div.post-content",
    "ul.wp-block-post-template",
    ".wp-block-cover__inner-container",
]
WP_CHAPTER_TITLE = [
    ".entry-title",
    ".page-title",
    "header.post-title h1",
    ".post-title",
    "#chapter-heading",
    ".wp-block-post-title",
]
MADARA_CONTENT = [
    ".reading-content .text-left",
    "div.reading-content",
]
MADARA_CHAPTER_LIST = "li.wp-manga-chapter a:not([title])"

SKIP_FILES = {
    "DefaultParser.js",
    "WikipediaParser.js",
    "DeviantArtParser.js",
    "TumblrParser.js",
    "WixParser.js",
    "XenforoBatchParser.js",
}

# Domains already covered by dedicated Python parsers
SKIP_DOMAINS = {
    "twkan.com",
    "69shuba.com",
    "69shu.com",
    "69shuba.cx",
    "69shu.pro",
    "69shuba.pro",
    "uukanshu.cc",
}

REGISTER_RE = re.compile(
    r'parserFactory\.register\(\s*"([^"]+)"\s*,\s*'
    r'(?:\(\)\s*=>\s*)?(?:function\s*\([^)]*\)\s*\{\s*return\s+)?'
    r'new\s+(\w+)',
    re.S,
)
CLASS_RE = re.compile(
    r'class\s+(\w+)\s+extends\s+(\w+)\s*\{',
)
QS_RE = re.compile(r'querySelector(?:All)?\(\s*([\'"])(.*?)\1\s*\)')
IMG_SRC_RE = re.compile(r'getFirstImgSrc\(\s*\w+\s*,\s*([\'"])(.*?)\1')
REMOVE_RE = re.compile(
    r'removeChildElementsMatchingSelector\(\s*\w+\s*,\s*([\'"])(.*?)\1'
)
LANG_RE = re.compile(
    r'extractLanguage\s*\([^)]*\)\s*\{[^}]*return\s*[\'"](\w+)',
    re.S,
)


def _method_body(class_src: str, name: str) -> str:
    m = re.search(
        rf'(?:async\s+)?{name}\s*\([^)]*\)\s*\{{',
        class_src,
    )
    if not m:
        return ""
    start = m.end() - 1
    depth = 0
    for i, ch in enumerate(class_src[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return class_src[start + 1 : i]
    return ""


def _selectors(body: str) -> list[str]:
    out = []
    for m in QS_RE.finditer(body):
        sel = m.group(2).strip()
        if sel and sel not in out:
            out.append(sel)
    return out


def _split_classes(src: str) -> dict[str, tuple[str, str]]:
    """class name -> (parent, body)."""
    found = {}
    matches = list(CLASS_RE.finditer(src))
    for i, m in enumerate(matches):
        name, parent = m.group(1), m.group(2)
        start = m.end() - 1
        end = matches[i + 1].start() if i + 1 < len(matches) else len(src)
        found[name] = (parent, src[start:end])
    return found


def _complex_toc(body: str) -> bool:
    if not body:
        return False
    markers = (
        "getChapterUrlsFromMultipleTocPages",
        "fetchJson",
        "for (let",
        "for(let",
        "walkPagesOfChapter",
        "getUrlsOfTocPages",
    )
    return any(s in body for s in markers)


def extract_file(path: Path) -> tuple[dict[str, list[str]], dict[str, dict]]:
    src = path.read_text(encoding="utf-8", errors="replace")
    domain_to_class: dict[str, list[str]] = defaultdict(list)
    for m in REGISTER_RE.finditer(src):
        domain_to_class[m.group(2)].append(m.group(1).lower())

    classes = _split_classes(src)
    specs: dict[str, dict] = {}
    for name, (parent, body) in classes.items():
        content_body = _method_body(body, "findContent")
        content = _selectors(content_body) if content_body else []
        if not content:
            if parent in ("WordpressBaseParser", "Parser") and name.endswith(
                ("WordpressParser",)
            ):
                content = list(WP_CONTENT)
            elif parent == "WordpressBaseParser":
                content = list(WP_CONTENT)
            elif parent in ("MadaraParser", "MadaraVariantParser"):
                content = list(MADARA_CONTENT)
            elif name in ("WordpressBaseParser",):
                content = list(WP_CONTENT)
            elif name in ("MadaraParser", "MadaraVariantParser"):
                content = list(MADARA_CONTENT)
        if not content:
            continue

        spec: dict = {"content": content}

        toc_body = _method_body(body, "getChapterUrls")
        if parent in ("MadaraParser", "MadaraVariantParser") and not toc_body:
            spec["chapter_list"] = MADARA_CHAPTER_LIST
            spec["reverse"] = True
        elif name in ("MadaraParser", "MadaraVariantParser"):
            spec["chapter_list"] = MADARA_CHAPTER_LIST
            spec["reverse"] = True
        elif toc_body and not _complex_toc(toc_body):
            wrap = "wrapFetch" in toc_body
            sels = _selectors(toc_body)
            if wrap and len(sels) >= 2:
                spec["toc_link"] = sels[0]
                spec["chapter_list"] = sels[1]
            elif sels:
                spec["chapter_list"] = sels[0]
            if ".reverse(" in toc_body or ".reverse()" in toc_body:
                spec["reverse"] = True

        title_s = _selectors(_method_body(body, "extractTitleImpl"))
        if title_s:
            spec["title"] = title_s[0]
        author_s = _selectors(_method_body(body, "extractAuthor"))
        if author_s:
            spec["author"] = author_s[0]
        ch_title = _selectors(_method_body(body, "findChapterTitle"))
        if not ch_title and parent == "WordpressBaseParser":
            ch_title = list(WP_CHAPTER_TITLE)
        if ch_title:
            spec["chapter_title"] = ch_title[0]
        desc = _selectors(_method_body(body, "extractDescription"))
        if desc:
            spec["description"] = desc[0]
        cover_body = _method_body(body, "findCoverImageUrl")
        img = IMG_SRC_RE.search(cover_body) if cover_body else None
        if img:
            spec["cover"] = img.group(2)
        else:
            cover_s = _selectors(cover_body) if cover_body else []
            if cover_s:
                spec["cover"] = cover_s[0]
        remove_body = _method_body(body, "removeUnwantedElementsFromContentElement")
        if remove_body:
            rms = [m.group(2) for m in REMOVE_RE.finditer(remove_body)]
            if rms:
                spec["remove"] = ", ".join(rms)
        lang_m = LANG_RE.search(body)
        if lang_m:
            spec["language"] = lang_m.group(1)

        specs[name] = spec
    return dict(domain_to_class), specs


def main() -> int:
    src_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "")
    if not src_dir.is_dir():
        print("Usage: python tools/extract_webtoepub.py <webtoepub parsers dir>", file=sys.stderr)
        return 2

    repo_root = Path(__file__).resolve().parent.parent
    out_path = repo_root / "parsers" / "webtoepub_sites.py"

    all_class_domains: dict[str, list[str]] = defaultdict(list)
    all_specs: dict[str, dict] = {}
    files = sorted(src_dir.glob("*Parser.js"))
    for path in files:
        if path.name in SKIP_FILES:
            continue
        class_domains, specs = extract_file(path)
        for cls, domains in class_domains.items():
            all_class_domains[cls].extend(domains)
        all_specs.update(specs)

    sites = []
    seen_domains: set[str] = set()
    for cls, domains in sorted(all_class_domains.items()):
        spec = all_specs.get(cls)
        if not spec:
            continue
        keep = []
        for d in domains:
            d = d.lower().removeprefix("www.")
            if d in SKIP_DOMAINS or d in seen_domains:
                continue
            keep.append(d)
            seen_domains.add(d)
        if not keep:
            continue
        entry = {
            "name": keep[0],
            "domains": keep,
            **spec,
        }
        sites.append(entry)

    # Stable sort by name
    sites.sort(key=lambda s: s["name"])

    header = (
        "# Auto-generated by tools/extract_webtoepub.py from WebToEpub\n"
        "# (https://github.com/dteviot/WebToEpub) parser CSS selectors.\n"
        "# Do not edit by hand — re-run the extractor instead.\n"
        "# WebToEpub is Apache-2.0; this file stores hostnames + selectors only.\n\n"
        "SITES = "
    )
    body = json.dumps(sites, indent=2, ensure_ascii=False)
    # JSON true/false/null -> Python
    body = body.replace("\n    ", "\n    ").replace(": true", ": True").replace(
        ": false", ": False"
    ).replace(": null", ": None")
    out_path.write_text(header + body + "\n", encoding="utf-8")
    print(f"Wrote {len(sites)} site specs ({len(seen_domains)} domains) -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
