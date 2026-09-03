# Author: joelsnl
"""
Pre/post glossary for Chinese web-novel NMT.

Directly swapping Chinese for English before a zh→en model often garbles
syntax. Instead we:

1. Protect hits with ASCII placeholders the engine usually copies (``§G0§``).
2. Restore those slots to the English rendering after the engine returns.
3. Sweep any leftover Chinese sources with ``Glossary.apply_to_text``.

Built-in xianxia/wuxia terms ship in ``data/novel_terms.json`` (a curated
pack, not CEDICT — it is expanded in releases, not by swallowing a general
dictionary). Auto attaches them only when the title/description/chapter list
looks like cultivation. During a translate pass, character names are harvested
into ``~/.huaepub/glossaries/<safe-title>.json`` so the next run (and the
final pass of this run) can lock them. User terms live in
``~/.huaepub/glossary.json``. None of these are Drive-synced.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from core.polish.glossary import Glossary, Term, glossary_from_data, load_glossary_file
from core.utils import safe_filename

_TOKEN = re.compile(r"§\s*G(\d+)\s*§", re.IGNORECASE)
_BUILTIN = Path(__file__).resolve().parent / "data" / "novel_terms.json"

# One hit is enough. Everyday words like 公子 / 凡人 / 家族 are *not* here —
# those would fire on urban/romance and force "Young Master" / "mortal" / "clan".
_XIANXIA_STRONG = (
    "修仙", "修真", "筑基", "金丹", "元婴", "化神", "炼气期", "炼气",
    "结丹", "渡劫", "灵根", "灵石", "丹田", "纳戒", "储物袋",
    "飞剑", "御剑", "功法", "心法", "真元", "辟谷", "散修",
    "内门弟子", "外门弟子", "核心弟子", "真传弟子",
    "洞府", "秘境", "传送阵", "符箓", "妖兽", "灵兽",
    "气运之子", "废灵根", "双灵根", "灵力", "法力",
    "xianxia", "xuanhuan", "wuxia",
    "golden core", "nascent soul", "foundation establishment",
    "qi refining", "spiritual root",
)
# Need two distinct hits. 公子 / 凡人 / 家族 / 师父 stay out.
_XIANXIA_WEAK = (
    "宗门", "修士", "修炼", "长老", "掌门", "宗主", "道友",
    "师兄", "师姐", "师尊", "灵气", "法宝", "阵法", "禁制",
)


@dataclass
class ProtectedText:
    """One segment after the protect pass."""

    text: str
    slots: list[str] = field(default_factory=list)


class GlossaryEngine:
    """Fast longest-first protect/restore over a ``Glossary``."""

    def __init__(self, glossary: Optional[Glossary] = None):
        self.glossary = glossary or Glossary()
        self._pattern: Optional[re.Pattern[str]] = None
        self._targets: dict[str, str] = {}
        self._fingerprint = ""
        self._rebuild()

    def _rebuild(self) -> None:
        pairs: list[tuple[str, str]] = []
        seen: set[str] = set()
        for term in self.glossary.terms:
            src = (term.source or "").strip()
            tgt = (term.target or "").strip()
            if not src or not tgt or src == tgt:
                continue
            if src in seen:
                continue
            seen.add(src)
            pairs.append((src, tgt))
        pairs.sort(key=lambda item: len(item[0]), reverse=True)
        self._targets = {src: tgt for src, tgt in pairs}
        if pairs:
            self._pattern = re.compile("|".join(re.escape(src) for src, _ in pairs))
        else:
            self._pattern = None
        blob = "\n".join(f"{src}\t{tgt}" for src, tgt in pairs)
        self._fingerprint = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def __len__(self) -> int:
        return len(self._targets)

    def merge(self, other: Glossary, overwrite: bool = True) -> None:
        self.glossary.merge(other, overwrite=overwrite)
        self._rebuild()

    def add_terms(self, terms: Iterable[tuple[str, str] | Term], overwrite: bool = True) -> None:
        for item in terms:
            if isinstance(item, Term):
                self.glossary.add(item, overwrite=overwrite)
            else:
                source, target = item
                self.glossary.add(Term(source=str(source), target=str(target)), overwrite=overwrite)
        self._rebuild()

    def protect(self, text: str) -> ProtectedText:
        raw = text or ""
        if not raw or self._pattern is None:
            return ProtectedText(text=raw)
        slots: list[str] = []

        def repl(match: re.Match[str]) -> str:
            src = match.group(0)
            slots.append(self._targets.get(src, src))
            return f"§G{len(slots) - 1}§"

        return ProtectedText(text=self._pattern.sub(repl, raw), slots=slots)

    def restore(self, text: str, protected: Optional[ProtectedText] = None) -> str:
        out = text or ""
        slots = protected.slots if protected is not None else []

        def repl(match: re.Match[str]) -> str:
            idx = int(match.group(1))
            if 0 <= idx < len(slots):
                return slots[idx]
            return match.group(0)

        if slots:
            out = _TOKEN.sub(repl, out)
        if self.glossary.terms:
            out = self.glossary.apply_to_text(out)
        return out

    def protect_many(self, texts: list[str]) -> list[ProtectedText]:
        return [self.protect(t) for t in texts]

    def restore_many(self, texts: list[str], protected: list[ProtectedText]) -> list[str]:
        out: list[str] = []
        for i, text in enumerate(texts):
            job = protected[i] if i < len(protected) else None
            out.append(self.restore(text, job))
        return out


def package_terms_path() -> Path:
    return _BUILTIN


def user_glossary_path() -> Path:
    from core.settings import get_data_dir

    return get_data_dir() / "glossary.json"


def novel_glossary_path(title: str) -> Path:
    from core.settings import get_data_dir

    name = safe_filename((title or "").strip()) or "untitled"
    return get_data_dir() / "glossaries" / f"{name}.json"


def qwen_glossary_path() -> Path:
    from core.settings import get_data_dir

    return get_data_dir() / "glossary-qwen.json"


def novel_glossaries_dir() -> Path:
    from core.settings import get_data_dir

    return get_data_dir() / "glossaries"


def save_glossary_file(path: Path, glossary: Glossary) -> None:
    """Atomic tmp+replace. Never Drive-synced."""
    from core.atomic_io import atomic_write_json

    path = Path(path)
    atomic_write_json(path, glossary.to_dict(), fsync=False)


def _load_json_glossary(path: Path) -> Glossary:
    if not path.is_file():
        return Glossary()
    try:
        return load_glossary_file(path)
    except Exception as exc:
        print(f"  Glossary ignored ({path}): {exc}")
        return Glossary()


def load_builtin_glossary() -> Glossary:
    if not _BUILTIN.is_file():
        return Glossary()
    try:
        data = json.loads(_BUILTIN.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  Built-in glossary failed to load: {exc}")
        return Glossary()
    return glossary_from_data(data)


def normalize_glossary_mode(mode: Optional[str]) -> str:
    """Return ``auto``, ``xianxia``, ``user``, or ``off``."""
    raw = (mode or "auto").strip().lower()
    if raw in ("off", "none", "false", "0", "disabled"):
        return "off"
    if raw in ("xianxia", "cultivation", "wuxia", "always", "on", "builtin"):
        return "xianxia"
    if raw in ("user", "names", "custom"):
        return "user"
    return "auto"


def looks_like_xianxia(*parts: str, min_weak: int = 2) -> bool:
    """True when title/description/TOC looks like cultivation, not urban/romance."""
    blob = "\n".join(p or "" for p in parts)
    if not blob.strip():
        return False
    lower = blob.lower()
    for term in _XIANXIA_STRONG:
        if term.isascii():
            if term in lower:
                return True
        elif term in blob:
            return True
    weak_hits = 0
    for term in _XIANXIA_WEAK:
        if term in blob:
            weak_hits += 1
            if weak_hits >= min_weak:
                return True
    return False


def load_user_glossary(novel_title: str = "") -> GlossaryEngine:
    """``glossary.json`` plus optional per-novel file. No built-in cultivation pack."""
    engine = GlossaryEngine(_load_json_glossary(user_glossary_path()))
    if novel_title:
        engine.merge(_load_json_glossary(novel_glossary_path(novel_title)), overwrite=True)
    return engine


def build_novel_glossary(
    *,
    novel_title: str = "",
    mode: str = "auto",
    detect_text: str = "",
    extra_terms: Optional[Iterable] = None,
) -> Optional[GlossaryEngine]:
    """
    Assemble the glossary for one novel.

    ``auto`` (default) attaches the built-in xianxia pack only when
    ``detect_text`` / the title look like cultivation. User and per-novel
    names always apply unless mode is ``off``.
    """
    resolved = normalize_glossary_mode(mode)
    if resolved == "off":
        return None
    use_builtin = resolved == "xianxia"
    if resolved == "auto":
        use_builtin = looks_like_xianxia(detect_text, novel_title)
    engine = GlossaryEngine()
    if use_builtin:
        engine.merge(load_builtin_glossary(), overwrite=False)
        # Legacy global Qwen dump — only on cultivation books (never urban).
        engine.merge(_load_json_glossary(qwen_glossary_path()), overwrite=False)
    engine.merge(_load_json_glossary(user_glossary_path()), overwrite=True)
    if novel_title:
        engine.merge(_load_json_glossary(novel_glossary_path(novel_title)), overwrite=True)
    if extra_terms:
        engine.add_terms(extra_terms)
    return engine


def load_default_novel_glossary(novel_title: str = "") -> GlossaryEngine:
    """Always include the built-in pack, then user / per-novel overlays."""
    engine = build_novel_glossary(novel_title=novel_title, mode="xianxia")
    return engine if engine is not None else GlossaryEngine()


def load_novel_glossary_file(path: str | Path) -> GlossaryEngine:
    return GlossaryEngine(_load_json_glossary(Path(path)))
