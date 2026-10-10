"""The documentation must not point at things that are gone.

Checks every Markdown file in the repo for: relative links and images that resolve, `#anchors`
that match a heading in the target page, and backticked repository paths (``core/updater.py``)
that exist. It catches the common way docs rot: a file is renamed or split and the prose is not.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = [ROOT / "README.md", ROOT / "CLAUDE.md", *sorted((ROOT / "docs").glob("*.md"))]

LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
SRC_ATTR = re.compile(r"""<img[^>]+src="([^"]+)\"""")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$", re.MULTILINE)
CODE_FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
INLINE_PATH = re.compile(
    r"`((?:core|gui|web|parsers|tests|tools|docs|packaging)/[A-Za-z0-9_./-]*[A-Za-z0-9_/])`"
)
ROOT_FILES = re.compile(
    r"`(VERSION|app\.py|build\.py|pyproject\.toml|snapcraft\.yaml|requirements[a-z-]*\.txt)`"
)


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading: lowercase, drop punctuation, spaces become hyphens."""
    text = re.sub(r"`([^`]*)`", r"\1", heading).strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s", "-", text)


def _anchors(path: Path) -> set:
    text = CODE_FENCE.sub("", path.read_text(encoding="utf-8"))
    return {_slug(m.group(2)) for m in HEADING.finditer(text)}


def _prose(path: Path) -> str:
    return CODE_FENCE.sub("", path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_links_and_images_resolve(doc):
    problems = []
    text = _prose(doc)
    for target in [*LINK.findall(text), *SRC_ATTR.findall(text)]:
        if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("mailto:"):
            continue  # external
        path_part, _, anchor = target.partition("#")
        dest = doc if not path_part else (doc.parent / path_part).resolve()
        if not dest.exists():
            problems.append(f"{target}: no such file")
        elif anchor and dest.suffix == ".md" and anchor not in _anchors(dest):
            problems.append(f"{target}: no heading '{anchor}' in {dest.name}")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_paths_named_in_the_docs_exist(doc):
    text = _prose(doc)
    missing = []
    for rel in INLINE_PATH.findall(text):
        if not (ROOT / rel).exists():
            missing.append(rel)
    for rel in ROOT_FILES.findall(text):
        if not (ROOT / rel).exists():
            missing.append(rel)
    assert not missing, f"{doc.name} mentions files that do not exist: {sorted(set(missing))}"


def test_every_doc_page_is_linked_from_the_readme():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    unlinked = [p.name for p in (ROOT / "docs").glob("*.md") if f"docs/{p.name}" not in readme]
    assert not unlinked, f"README does not link: {unlinked}"
