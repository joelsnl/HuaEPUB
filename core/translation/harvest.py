# Author: joelsnl
"""
Per-novel glossary mining for the protect/restore lock list.

The built-in cultivation pack stays a curated list (it is not CEDICT). A
generic Chinese dictionary would pin everyday words and garble MT syntax.
Instead, each book grows ``~/.huaepub/glossaries/<title>.json`` from names
and domain terms that actually appear in that text.

Person names are romanized with pypinyin (not Google). User-edited targets
are never overwritten. Never Drive-synced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from core.polish.glossary import Term
from core.translation.glossary import (
    GlossaryEngine,
    _load_json_glossary,
    load_builtin_glossary,
    novel_glossary_path,
    save_glossary_file,
)

_CJK = r"[\u4e00-\u9fff]"
_INTRO = re.compile(
    rf"(?:名叫|名为|叫做|人称|自称)({_CJK}{{1,4}})"
)
_SURNAME = re.compile(
    rf"姓({_CJK}{{1,2}})(?:名|，名|、名)({_CJK}{{1,2}})"
)
_ADDRESSED = re.compile(
    rf"({_CJK}{{2,4}})(?:道友|师兄|师姐|师弟|师妹|仙子|姑娘|兄弟|兄台)"
)
_SAID = re.compile(
    rf"({_CJK}{{2,4}})(?:说道?|问道|喝道|笑道|冷道|怒道|叹道|叫道|答道)"
)
_CALLED = re.compile(
    rf"(?:他叫|她叫|人叫)(?!做)({_CJK}{{2,4}})"
)
_ZHENGSHI = re.compile(
    rf"(?:此人正是|正是那)({_CJK}{{2,4}})"
)
_ORG_SUFFIX = re.compile(r"(宗|门|教|帮|谷|峰|城|国|殿|阁|楼|盟|派)")
_BOOK_TITLE = re.compile(rf"《({_CJK}{{2,8}})》")
_SKILL = re.compile(
    rf"({_CJK}{{2,6}})(诀|功|经|录)"
)
_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_NAME_OK = re.compile(r"^[\sA-Za-z][A-Za-z\s\-'.]*[A-Za-z]$")

_HONORIFIC_SUFFIXES = tuple(
    sorted(
        (
            "道友", "师兄", "师姐", "师弟", "师妹", "仙子", "姑娘",
            "兄弟", "兄台", "前辈", "大人", "公子", "先生", "小姐",
        ),
        key=len,
        reverse=True,
    )
)

# Function words, chapter labels, and other strings that are not lock terms.
_STOP = frozenset(
    """
    的 了 是 在 我 他 她 它 这 那 有 一 个 上 不 中 下 和 就 都 也 要 会 能
    什么 没有 一个 我们 他们 她们 自己 因为 所以 但是 可是 然后 这个 那个
    现在 已经 还是 知道 觉得 出来 进去 起来 时候 地方 东西 声音 眼睛 身体
    心里 心中 脸上 之中 之间 之后 之前 以上 以下 不是 只是 还是 或者 而且
    如果 虽然 只见 忽然 于是 便是 却是 乃是 正是 只得 不得 不敢 不能 不会
    这里 那里 哪里 怎么 为什么 多少 几个 第一 第二 第三 第章 本章 本章
    第一章 第二章 第三章 第几章 上一章 下一章 卷 章 节 回
    修士 修炼 修仙 凡人 宗门 家族 长老 掌门 弟子 道友 师兄 师姐 师弟 师妹
    前辈 晚辈 公子 少爷 少主 陛下 殿下 师父 师傅 师尊
    今日 明日 昨日 此时 此刻 此人 此子 此女 对方 众人 两人 三人
    突然 立刻 马上 终于 原来 竟然 果然 几乎 似乎 仿佛 好像
    说道 问道 喝道 笑道 冷道
    中国 美国 时间 世界 人间 其中 外门 内门 本门 此门
    方法 办法 想法 说法 无法 合法 看法 做法 用法 算法 语法
    民法 刑法 加法 减法 乘法 除法 功法 心法
    """.split()
)

_ORG_STEM_STOP = frozenset("外 内 本 此 其 一 那 这 我 你 他 大 小 正 真".split())
_ORG_NOISE = set("了到来在的是有去说着过给把被让从向往和与及个们叫")

_BAD_TARGETS = frozenset(
    {
        "mortal",
        "cultivate",
        "cultivator",
        "sect",
        "clan",
        "elder",
        "master",
        "senior",
        "junior",
        "pill",
        "formation",
        "disciple",
        "immortal",
        "the",
        "he",
        "she",
        "said",
        "asked",
    }
)

_COMPOUND_SURNAMES = frozenset(
    """
    欧阳 太史 端木 上官 司马 东方 独孤 南宫 万俟 闻人 夏侯 诸葛 尉迟
    公孙 慕容 仲孙 钟离 长孙 司徒 司空 司寇 子车 巫马 公西 漆雕 乐正
    壤驷 公良 拓跋 夹谷 宰父 谷梁 呼延 羊舌 微生 梁丘 左丘 东门 西门
    南荣 令狐 鲜于 宇文 司城 太叔 申屠 公羊 贺兰 轩辕 皇甫 澹台 公冶
    """.split()
)

HARVEST_LIMIT = 24
MINE_LIMIT = 80

KIND_CHARACTER = "character"
KIND_PLACE = "place"
KIND_ORG = "organization"
KIND_TECHNIQUE = "technique"


@dataclass
class MineCandidate:
    """One lock-list candidate extracted from this book's Chinese."""

    source: str
    kind: str
    score: int
    count: int
    evidence: str = ""
    default_target: str = ""
    aliases: list[str] = field(default_factory=list)


def plain_text(html_or_text: str) -> str:
    raw = _TAGS.sub(" ", html_or_text or "")
    return _SPACE.sub(" ", raw).strip()


def cjk_count(text: str) -> int:
    return sum(1 for ch in text or "" if "\u4e00" <= ch <= "\u9fff")


def strip_honorific(name: str) -> str:
    src = (name or "").strip()
    for suf in _HONORIFIC_SUFFIXES:
        if src.endswith(suf) and cjk_count(src) - len(suf) >= 2:
            return src[: -len(suf)]
    return src


def _blocked(existing: Optional[GlossaryEngine]) -> set[str]:
    out = set(_STOP)
    try:
        for term in load_builtin_glossary().terms:
            if term.source:
                out.add(term.source)
    except Exception:
        pass
    if existing is not None:
        out.update(existing._targets.keys())
    return out


def _cap_syl(part: str) -> str:
    raw = (part or "").strip()
    if not raw:
        return ""
    return raw[:1].upper() + raw[1:]


def _join_given(parts: list[str]) -> str:
    out = ""
    for i, part in enumerate(parts):
        syl = (part or "").strip()
        if not syl:
            continue
        if i > 0 and syl[:1].lower() in "aoe":
            out += "'"
        out += syl
    return _cap_syl(out)


def format_personal_pinyin(source: str) -> str:
    """Han Li / Lin Wan'er / Dongfang Bubai. Empty if pypinyin is missing."""
    src = (source or "").strip()
    if cjk_count(src) < 2:
        return ""
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError:
        return ""
    syl = [s for s in lazy_pinyin(src, style=Style.NORMAL) if s]
    if not syl:
        return ""
    if len(src) >= 3 and src[:2] in _COMPOUND_SURNAMES and len(syl) >= 3:
        surname = _cap_syl(syl[0]) + syl[1]
        given = _join_given(syl[2:])
        return f"{surname} {given}".strip()
    surname = _cap_syl(syl[0])
    given = _join_given(syl[1:])
    return f"{surname} {given}".strip()


def format_phrase_pinyin(source: str) -> str:
    src = (source or "").strip()
    if cjk_count(src) < 2:
        return ""
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError:
        return ""
    syl = [s for s in lazy_pinyin(src, style=Style.NORMAL) if s]
    return " ".join(_cap_syl(s) for s in syl if s)


def default_target_for(source: str, kind: str) -> str:
    if kind == KIND_CHARACTER:
        return format_personal_pinyin(source)
    return format_phrase_pinyin(source)


def _evidence(blob: str, src: str, width: int = 48) -> str:
    i = blob.find(src)
    if i < 0:
        return ""
    start = max(0, i - 16)
    end = min(len(blob), i + len(src) + width)
    clip = blob[start:end].strip()
    return clip[:80]


def _drop_substrings(scores: dict[str, int]) -> dict[str, int]:
    """Prefer the longer term; fold shorter-substring scores into it."""
    sources = list(scores)
    drop: set[str] = set()
    for short in sources:
        longer = [other for other in sources if short != other and short in other]
        if not longer:
            continue
        for other in longer:
            scores[other] = scores.get(other, 0) + scores[short]
        drop.add(short)
    return {src: n for src, n in scores.items() if src not in drop}


def harvest_candidates(
    texts: Iterable[str],
    *,
    existing: Optional[GlossaryEngine] = None,
    limit: int = HARVEST_LIMIT,
) -> list[str]:
    """Chinese name-like strings from intro / address / 'X said' patterns."""
    return [
        c.source
        for c in mine_glossary_candidates(
            texts,
            existing=existing,
            limit=limit,
            kinds={KIND_CHARACTER},
        )
    ]


def mine_glossary_candidates(
    texts: Iterable[str],
    *,
    existing: Optional[GlossaryEngine] = None,
    limit: int = MINE_LIMIT,
    kinds: Optional[set[str]] = None,
) -> list[MineCandidate]:
    """
    Names, sects/places, and techniques that actually occur in ``texts``.
    Does not invent terms. Builtin pack sources are skipped.
    """
    want = kinds or {KIND_CHARACTER, KIND_PLACE, KIND_ORG, KIND_TECHNIQUE}
    blocked = _blocked(existing)
    blobs = [plain_text(t) for t in texts if t]
    if not blobs:
        return []
    corpus = "\n".join(blobs)

    name_scores: dict[str, int] = {}
    org_scores: dict[str, int] = {}
    tech_scores: dict[str, int] = {}

    def add_name(name: str, weight: int) -> None:
        src = strip_honorific((name or "").strip())
        if cjk_count(src) < 2 or len(src) > 4:
            return
        if src in blocked or any(ch.isdigit() for ch in src):
            return
        if src.startswith("第") and src.endswith("章"):
            return
        name_scores[src] = name_scores.get(src, 0) + weight

    def add_org(full: str, weight: int) -> None:
        src = (full or "").strip()
        if cjk_count(src) < 2 or len(src) > 8:
            return
        if src in blocked or src in _STOP:
            return
        stem = src[:-1] if src else src
        if stem in _STOP or (stem and stem[-1:] in _ORG_STEM_STOP):
            return
        if any(ch in _ORG_NOISE for ch in stem):
            return
        org_scores[src] = org_scores.get(src, 0) + weight

    def add_tech(src: str, weight: int) -> None:
        raw = (src or "").strip()
        if cjk_count(raw) < 2 or len(raw) > 8:
            return
        if raw in blocked or raw in _STOP:
            return
        tech_scores[raw] = tech_scores.get(raw, 0) + weight

    for blob in blobs:
        if KIND_CHARACTER in want:
            for match in _INTRO.finditer(blob):
                add_name(match.group(1), 6)
            for match in _SURNAME.finditer(blob):
                add_name(match.group(1) + match.group(2), 8)
            for match in _ADDRESSED.finditer(blob):
                add_name(match.group(1), 3)
            for match in _SAID.finditer(blob):
                add_name(match.group(1), 2)
            for match in _CALLED.finditer(blob):
                add_name(match.group(1), 4)
            for match in _ZHENGSHI.finditer(blob):
                add_name(match.group(1), 5)
        if KIND_ORG in want or KIND_PLACE in want:
            for match in _ORG_SUFFIX.finditer(blob):
                suf = match.group(1)
                start = match.start()
                picked = ""
                for n in (4, 3, 2):
                    if start < n:
                        continue
                    stem = blob[start - n : start]
                    if cjk_count(stem) != n:
                        continue
                    candidate = stem + suf
                    if candidate in blocked or candidate in _STOP:
                        continue
                    if stem in _STOP or stem[-1:] in _ORG_STEM_STOP:
                        continue
                    if any(ch in _ORG_NOISE for ch in stem):
                        continue
                    picked = candidate
                    break
                if picked:
                    add_org(picked, 3)
        if KIND_TECHNIQUE in want:
            for match in _BOOK_TITLE.finditer(blob):
                add_tech(match.group(1), 8)
            for match in _SKILL.finditer(blob):
                add_tech(match.group(1) + match.group(2), 3)

    def flush(
        scores: dict[str, int],
        kind: str,
        min_score: int,
        min_count: int,
    ) -> list[MineCandidate]:
        clustered = _drop_substrings(dict(scores))
        rows: list[MineCandidate] = []
        for src, score in clustered.items():
            count = corpus.count(src)
            if score < min_score and count < min_count:
                continue
            if count < 1:
                continue
            target = default_target_for(src, kind)
            if not target:
                continue
            rows.append(
                MineCandidate(
                    source=src,
                    kind=kind,
                    score=score,
                    count=count,
                    evidence=_evidence(corpus, src),
                    default_target=target,
                )
            )
        return rows

    out: list[MineCandidate] = []
    if KIND_CHARACTER in want:
        out.extend(flush(name_scores, KIND_CHARACTER, min_score=2, min_count=2))
    if KIND_ORG in want or KIND_PLACE in want:
        org_rows = flush(org_scores, KIND_ORG, min_score=3, min_count=2)
        for row in org_rows:
            if row.source.endswith(("谷", "峰", "城", "国", "殿")):
                row.kind = KIND_PLACE
        out.extend(org_rows)
    if KIND_TECHNIQUE in want:
        out.extend(flush(tech_scores, KIND_TECHNIQUE, min_score=3, min_count=1))

    out.sort(key=lambda c: (-c.score, -c.count, -len(c.source), c.source))
    # Drop remaining shorter substrings across kinds (韩 vs 韩立).
    kept: list[MineCandidate] = []
    seen: list[str] = []
    for cand in out:
        if any(cand.source != other and cand.source in other for other in seen):
            continue
        kept.append(cand)
        seen.append(cand.source)
        if len(kept) >= limit:
            break
    return kept


def is_usable_name_target(source: str, target: str) -> bool:
    src = (source or "").strip()
    tgt = re.sub(r"\s+", " ", (target or "").strip())
    if not src or not tgt or src == tgt:
        return False
    if src in tgt:
        return False
    if any("\u4e00" <= ch <= "\u9fff" for ch in tgt):
        return False
    if tgt.casefold() in _BAD_TARGETS:
        return False
    if len(tgt) > 40 or len(tgt.split()) > 4:
        return False
    if not _NAME_OK.match(tgt):
        return False
    return True


def render_harvested_names(
    sources: list[str],
    translate_many: Callable[[list[str]], list[str]],
) -> list[tuple[str, str]]:
    if not sources:
        return []
    try:
        rendered = translate_many(sources)
    except Exception as exc:
        print(f"  Name harvest render failed: {exc}")
        return []
    pairs: list[tuple[str, str]] = []
    for src, tgt in zip(sources, rendered):
        if is_usable_name_target(src, tgt):
            pairs.append((src, tgt.strip()))
    return pairs


def persist_harvested_terms(
    novel_title: str,
    pairs: Iterable[tuple[str, str]],
    *,
    kind: str = KIND_CHARACTER,
    notes: str = "harvested",
) -> int:
    """Merge mined terms into the per-novel file. User targets win."""
    title = (novel_title or "").strip()
    rows = [(s.strip(), t.strip()) for s, t in pairs if s.strip() and t.strip()]
    if not title or not rows:
        return 0
    path = novel_glossary_path(title)
    existing = _load_json_glossary(path)
    before = {t.source for t in existing.terms}
    for source, target in rows:
        existing.add(
            Term(source=source, target=target, kind=kind, notes=notes),
            overwrite=False,
        )
    added = len({t.source for t in existing.terms} - before)
    if added:
        try:
            save_glossary_file(path, existing)
        except Exception as exc:
            print(f"  Per-novel glossary not saved: {exc}")
            return 0
    return added


def harvest_and_apply(
    translator,
    texts: Iterable[str],
    *,
    novel_title: str = "",
    limit: int = MINE_LIMIT,
) -> int:
    """
    Find names and domain terms in ``texts``, romanize with pypinyin,
    merge into this run, persist for the next. Returns terms added.
    """
    gloss = getattr(translator, "glossary", None)
    if gloss is None:
        return 0
    if getattr(translator, "_cancel_requested", False):
        return 0
    sample = [plain_text(t) for t in texts if t]
    if not sample:
        return 0
    candidates = mine_glossary_candidates(sample, existing=gloss, limit=limit)
    if not candidates:
        return 0
    pairs: list[tuple[str, str, str]] = []
    for cand in candidates:
        tgt = (cand.default_target or "").strip()
        if not tgt:
            continue
        if cand.kind == KIND_CHARACTER:
            if not is_usable_name_target(cand.source, tgt):
                continue
        elif not _ok_lock_target(tgt):
            continue
        pairs.append((cand.source, tgt, cand.kind))
    if not pairs:
        return 0
    gloss.add_terms(
        [
            Term(source=s, target=t, kind=k, notes="pinyin")
            for s, t, k in pairs
        ],
        overwrite=False,
    )
    by_kind: dict[str, list[tuple[str, str]]] = {}
    for source, target, kind in pairs:
        by_kind.setdefault(kind, []).append((source, target))
    saved = 0
    for kind, group in by_kind.items():
        saved += persist_harvested_terms(
            novel_title, group, kind=kind, notes="pinyin"
        )
    print(
        f"Glossary harvest: {len(pairs)} term(s) this run"
        + (f", {saved} new in per-novel file" if saved else "")
    )
    return len(pairs)


def _ok_lock_target(target: str) -> bool:
    tgt = " ".join((target or "").split())
    if not tgt or len(tgt) > 48 or len(tgt.split()) > 6:
        return False
    if any("\u4e00" <= ch <= "\u9fff" for ch in tgt):
        return False
    if tgt.casefold() in _BAD_TARGETS or tgt.casefold() in _STOP:
        return False
    letters = sum(c.isalpha() or c.isspace() or c in "-'" for c in tgt)
    return letters / max(len(tgt), 1) >= 0.7
