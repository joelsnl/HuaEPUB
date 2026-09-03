# Author: joelsnl
"""
Local-Qwen glossary classifier.

Qwen only labels candidates already mined from a book's Chinese. It does
not invent terms from titles and does not rewrite shipped novel_terms.json.
Writes go to ``~/.huaepub/glossaries/<title>.json`` only (never a global
dump). Never Drive-synced. Everyday Chinese is rejected — this is not CEDICT.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Optional

from core.polish.glossary import Glossary, Term
from core.polish.prompts import parse_glossary_json
from core.translation.glossary import (
    _load_json_glossary,
    load_builtin_glossary,
    looks_like_xianxia,
    novel_glossaries_dir,
    novel_glossary_path,
    save_glossary_file,
    user_glossary_path,
)
from core.translation.harvest import (
    KIND_CHARACTER,
    MINE_LIMIT,
    MineCandidate,
    _STOP,
    _ok_lock_target,
    is_usable_name_target,
    mine_glossary_candidates,
    plain_text,
)

LogFn = Callable[[str], None]
Cancelled = Callable[[], bool]
CompleteFn = Callable[[str, str], str]

GLOSSARY_QWEN_INTERVAL_S = 7 * 24 * 3600
_MAX_CANDIDATES = 30
_MAX_PROMPT_LOCKED = 40
_QWEN_MIN_PARAMS_B = 6.5
_OVERWRITE_NOTES = frozenset({"harvested", "qwen", "pinyin"})

GLOSSARY_CLASSIFY_SYSTEM = """You classify glossary candidates from one Chinese web novel.
Return TSV only, no markdown, no extra lines:
id<TAB>action<TAB>target<TAB>type
action is keep, fix, or drop.
type is character, place, organization, technique, item, or title.
Person names: pinyin (Han Li), never calques (Cold Stand, King Forest).
You may drop any id. Do not invent ids or sources that are not listed.
If the book is not cultivation, drop ranks and xianxia jargon.
"""


@dataclass
class GlossaryProposal:
    novel_title: str
    source: str
    target: str
    kind: str
    notes: str = "qwen"
    evidence: str = ""
    count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _in_pytest() -> bool:
    from core.utils import in_pytest

    return in_pytest()


def polish_gguf_on_disk() -> bool:
    """True when the GGUF this hardware would use is already cached."""
    try:
        from core.polish.hardware import detect_device
        from core.polish.serve import find_local_gguf, find_ollama_blob, gguf_choice
    except Exception:
        return False
    try:
        profile = detect_device()
        alias, filename, _url = gguf_choice(profile)
    except Exception:
        return False
    if find_local_gguf(filename):
        return True
    try:
        return bool(find_ollama_blob(alias))
    except Exception:
        return False


def qwen_glossary_capable() -> bool:
    """3B polish profiles skip JSON/TSV classification."""
    try:
        from core.polish.hardware import detect_device

        return float(detect_device().max_params_b) >= _QWEN_MIN_PARAMS_B
    except Exception:
        return False


def should_offer_glossary_qwen(
    settings: dict[str, Any],
    *,
    has_library: bool,
    has_harvested: bool,
    now: Optional[float] = None,
    force: bool = False,
    model_ready: Optional[bool] = None,
    qwen_capable: Optional[bool] = None,
) -> bool:
    """True when a startup modal should ask to run the Qwen glossary pass."""
    if force:
        return True
    if not settings.get("glossary_qwen_ask", True):
        return False
    mode = str(settings.get("translation_glossary") or "auto").strip().lower()
    if mode in ("off", "none", "false", "0"):
        return False
    ready = polish_gguf_on_disk() if model_ready is None else bool(model_ready)
    capable = qwen_glossary_capable() if qwen_capable is None else bool(qwen_capable)
    if not ready or not capable:
        return False
    if not has_library and not has_harvested:
        return False
    if has_harvested:
        return True
    last = float(settings.get("glossary_qwen_last_at") or 0)
    t = time.time() if now is None else now
    if last <= 0:
        return True
    return (t - last) >= GLOSSARY_QWEN_INTERVAL_S


def has_harvested_terms() -> bool:
    folder = novel_glossaries_dir()
    if not folder.is_dir():
        return False
    for path in folder.glob("*.json"):
        gloss = _load_json_glossary(path)
        if any((t.notes or "") == "harvested" for t in gloss.terms):
            return True
    return False


def filter_qwen_term(
    source: str,
    target: str,
    *,
    kind: str = "term",
    notes: str = "qwen",
    locked_sources: Optional[set[str]] = None,
) -> Optional[Term]:
    src = (source or "").strip()
    tgt = " ".join((target or "").split())
    n_cjk = sum(1 for ch in src if "\u4e00" <= ch <= "\u9fff")
    if n_cjk < 2 or len(src) > 8:
        return None
    if src in _STOP:
        return None
    if locked_sources and src in locked_sources:
        return None
    if not is_usable_name_target(src, tgt) and not _ok_lock_target(tgt):
        return None
    if tgt.casefold() in _STOP:
        return None
    return Term(source=src, target=tgt, kind=kind or "term", notes=notes or "qwen")


def terms_from_qwen_json(
    raw: str,
    *,
    locked_sources: Optional[set[str]] = None,
    allowed_sources: Optional[set[str]] = None,
) -> list[Term]:
    out: list[Term] = []
    seen: set[str] = set()
    for row in parse_glossary_json(raw or ""):
        src = str(row.get("source") or "")
        if allowed_sources is not None and src.strip() not in allowed_sources:
            continue
        term = filter_qwen_term(
            src,
            str(row.get("target") or ""),
            kind=str(row.get("type") or row.get("kind") or "term"),
            notes="qwen",
            locked_sources=locked_sources,
        )
        if term is None or term.source in seen:
            continue
        seen.add(term.source)
        out.append(term)
    return out


def merge_qwen_terms(existing: Glossary, incoming: list[Term]) -> tuple[int, int]:
    """
    Add new terms. Overwrite only harvested/qwen/pinyin rows. Hand-edited rows win.
    Returns (added, updated).
    """
    added = 0
    updated = 0
    by_source = {t.source: t for t in existing.terms}
    for term in incoming:
        old = by_source.get(term.source)
        if old is None:
            existing.add(term, overwrite=False)
            by_source[term.source] = term
            added += 1
            continue
        if (old.notes or "") not in _OVERWRITE_NOTES:
            continue
        if old.target == term.target and (old.notes or "") == (term.notes or ""):
            continue
        existing.add(term, overwrite=True)
        updated += 1
    return added, updated


def apply_glossary_proposals(
    proposals: list[GlossaryProposal | dict[str, Any]],
) -> tuple[int, int]:
    """Write accepted proposals into per-novel files. Never a global dump."""
    added = 0
    updated = 0
    by_title: dict[str, list[Term]] = {}
    for item in proposals:
        if isinstance(item, dict):
            title = str(item.get("novel_title") or "").strip()
            term = filter_qwen_term(
                str(item.get("source") or ""),
                str(item.get("target") or ""),
                kind=str(item.get("kind") or "term"),
                notes=str(item.get("notes") or "qwen"),
            )
        else:
            title = (item.novel_title or "").strip()
            term = filter_qwen_term(
                item.source,
                item.target,
                kind=item.kind,
                notes=item.notes or "qwen",
            )
        if not title or term is None:
            continue
        by_title.setdefault(title, []).append(term)
    for title, incoming in by_title.items():
        path = novel_glossary_path(title)
        gloss = _load_json_glossary(path)
        a, u = merge_qwen_terms(gloss, incoming)
        marked = False
        for term in gloss.terms:
            if (term.notes or "") == "harvested":
                term.notes = "qwen"
                marked = True
        if a or u or marked:
            save_glossary_file(path, gloss)
        added += a
        updated += u
    return added, updated


def _locked_sources() -> set[str]:
    locked = set(_STOP)
    for term in load_builtin_glossary().terms:
        if term.source:
            locked.add(term.source)
    for term in _load_json_glossary(user_glossary_path()).terms:
        if term.source:
            locked.add(term.source)
    return locked


def _mark_harvested_notes(gloss: Glossary) -> None:
    for term in gloss.terms:
        if (term.notes or "") == "harvested":
            term.notes = "qwen"


def _mark_harvested_seen(title: str) -> None:
    path = novel_glossary_path(title)
    gloss = _load_json_glossary(path)
    before = [(t.source, t.notes) for t in gloss.terms]
    _mark_harvested_notes(gloss)
    after = [(t.source, t.notes) for t in gloss.terms]
    if before != after:
        save_glossary_file(path, gloss)


_TSV_LINE = re.compile(
    r"^\s*(\d+)\s+([A-Za-z]+)\s+(.+?)\s+([A-Za-z]+)\s*$"
)
_TSV_DROP = re.compile(r"^\s*(\d+)\s+drop\b", re.I)


def parse_classifier_output(
    raw: str,
    candidates: list[MineCandidate],
) -> list[tuple[MineCandidate, str, str, str]]:
    """
    Map model output onto the candidate list.
    Returns (candidate, action, target, kind). Unknown ids / new sources are dropped.
    """
    by_id = {i: cand for i, cand in enumerate(candidates, start=1)}
    by_src = {c.source: c for c in candidates}
    rows: list[tuple[MineCandidate, str, str, str]] = []
    seen: set[str] = set()

    def add(cand: MineCandidate, action: str, target: str, kind: str) -> None:
        act = (action or "keep").strip().lower()
        if act not in ("keep", "fix", "drop"):
            act = "keep"
        if cand.source in seen:
            return
        seen.add(cand.source)
        tgt = " ".join((target or cand.default_target or "").split())
        k = (kind or cand.kind or "term").strip().lower() or cand.kind
        rows.append((cand, act, tgt, k))

    for line in (raw or "").splitlines():
        text = line.strip().strip("`")
        if not text or text.lower().startswith("id"):
            continue
        drop = _TSV_DROP.match(text)
        if drop:
            cand = by_id.get(int(drop.group(1)))
            if cand is not None:
                add(cand, "drop", "", cand.kind)
            continue
        match = _TSV_LINE.match(text.replace("\t", "  "))
        if not match:
            continue
        cand = by_id.get(int(match.group(1)))
        if cand is None:
            continue
        add(cand, match.group(2), match.group(3).strip(), match.group(4))

    if rows:
        return rows

    payload = None
    cleaned = (raw or "").strip()
    if "```" in cleaned:
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.MULTILINE)
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            payload = None
    terms = []
    if isinstance(payload, dict):
        terms = payload.get("terms") or []
    if not isinstance(terms, list):
        terms = []
    for row in terms:
        if not isinstance(row, dict):
            continue
        cid = row.get("id")
        cand = None
        if cid is not None:
            try:
                cand = by_id.get(int(cid))
            except (TypeError, ValueError):
                cand = None
        if cand is None:
            src = str(row.get("source") or "").strip()
            cand = by_src.get(src)
        if cand is None:
            continue
        add(
            cand,
            str(row.get("action") or "fix"),
            str(row.get("target") or ""),
            str(row.get("type") or row.get("kind") or cand.kind),
        )
    return rows


def _format_candidate_block(candidates: list[MineCandidate]) -> str:
    lines = []
    for i, cand in enumerate(candidates, start=1):
        ev = (cand.evidence or "").replace("\n", " ")
        lines.append(
            f"{i}\t{cand.source}\t{cand.kind}\t{cand.count}\t"
            f"{cand.default_target}\t{ev}"
        )
    return "\n".join(lines)


def _build_user_prompt(
    *,
    title: str,
    xianxia: bool,
    candidates: list[MineCandidate],
    already: list[Term],
) -> str:
    genre = "cultivation" if xianxia else "urban/romance/other"
    genre_note = (
        "Cultivation ranks/items are useful."
        if xianxia
        else "Do not force xianxia ranks onto this book."
    )
    locked = "\n".join(
        f"{t.source} → {t.target}" for t in already[:_MAX_PROMPT_LOCKED] if t.source
    )
    parts = [
        f"Genre: {genre}",
        f"Book: {title or '(untitled)'}",
        genre_note,
    ]
    if locked:
        parts.append("Already locked for this book:")
        parts.append(locked)
    parts.append("Candidates (id, source, kind, count, default, evidence):")
    parts.append(_format_candidate_block(candidates))
    parts.append("Classify every id you keep or fix. Drop the rest.")
    return "\n".join(parts)


def _harvested_as_candidates(gloss: Glossary, corpus: str) -> list[MineCandidate]:
    out: list[MineCandidate] = []
    for term in gloss.terms:
        if (term.notes or "") != "harvested":
            continue
        src = (term.source or "").strip()
        if not src:
            continue
        out.append(
            MineCandidate(
                source=src,
                kind=term.kind or KIND_CHARACTER,
                score=8,
                count=max(corpus.count(src), 1),
                evidence=term.target or "",
                default_target=term.target or "",
            )
        )
    return out


def load_book_corpus(
    *,
    title: str = "",
    source_url: str = "",
    description: str = "",
    extra_texts: Optional[list[str]] = None,
    cache=None,
) -> list[str]:
    texts: list[str] = []
    if title:
        texts.append(title)
    if description:
        texts.append(description)
    if extra_texts:
        texts.extend(t for t in extra_texts if t)
    if cache is not None and source_url:
        try:
            toc = cache.get_chapter_list(source_url)
        except Exception:
            toc = None
        if toc:
            texts.extend((item.get("title") or "") for item in toc[:80])
        sample = getattr(cache, "sample_chapter_contents", None)
        if callable(sample):
            try:
                texts.extend(sample(source_url, limit=8) or [])
            except Exception:
                pass
    return [plain_text(t) for t in texts if t and str(t).strip()]


def classify_candidates(
    candidates: list[MineCandidate],
    *,
    title: str,
    complete: CompleteFn,
    xianxia: bool,
    already: Optional[list[Term]] = None,
    log: Optional[LogFn] = None,
) -> list[GlossaryProposal]:
    """One Qwen call over this book's candidate list. Invented sources are dropped."""
    emit = log or (lambda _msg: None)
    if not candidates:
        return []
    batch = candidates[:_MAX_CANDIDATES]
    user = _build_user_prompt(
        title=title,
        xianxia=xianxia,
        candidates=batch,
        already=already or [],
    )
    raw = ""
    parsed: list[tuple[MineCandidate, str, str, str]] = []
    for attempt in range(2):
        raw = complete(GLOSSARY_CLASSIFY_SYSTEM, user) or ""
        parsed = parse_classifier_output(raw, batch)
        if parsed:
            break
        emit(f"  Qwen glossary parse empty (try {attempt + 1}); retrying…")
        user = user + "\nYour last output was invalid. Emit TSV rows only."
    if not parsed:
        print(f"  Qwen glossary raw (truncated): {(raw or '')[:400]!r}")
        return []

    locked = _locked_sources()
    proposals: list[GlossaryProposal] = []
    for cand, action, target, kind in parsed:
        if action == "drop":
            continue
        tgt = target or cand.default_target
        if action == "keep":
            tgt = cand.default_target or target
        term = filter_qwen_term(
            cand.source,
            tgt,
            kind=kind or cand.kind,
            notes="qwen",
            locked_sources=locked,
        )
        if term is None:
            continue
        proposals.append(
            GlossaryProposal(
                novel_title=title,
                source=term.source,
                target=term.target,
                kind=term.kind,
                notes="qwen",
                evidence=cand.evidence,
                count=cand.count,
            )
        )
    return proposals


def classify_novel_with_qwen(
    *,
    novel_title: str,
    texts: list[str],
    complete_fn: Optional[CompleteFn] = None,
    cancelled: Optional[Cancelled] = None,
    log: Optional[LogFn] = None,
    apply: bool = True,
    engine=None,
    allow_download: bool = False,
) -> dict[str, Any]:
    """
    Mine this book, classify with Qwen (if available), optionally write.
    ``complete_fn`` is injected in tests. Never writes glossary-qwen.json.
    """
    emit = log or (lambda _msg: None)
    stop = cancelled or (lambda: False)
    title = (novel_title or "").strip()
    empty = {
        "added": 0,
        "updated": 0,
        "cancelled": False,
        "message": "Qwen glossary pass finished — nothing new to add.",
        "proposals": [],
    }
    if stop():
        return {
            "added": 0,
            "updated": 0,
            "cancelled": True,
            "message": "Cancelled.",
            "proposals": [],
        }
    sample = [plain_text(t) for t in texts if t]
    existing_file = _load_json_glossary(novel_glossary_path(title)) if title else Glossary()
    mined = mine_glossary_candidates(sample, limit=MINE_LIMIT)
    harvested = _harvested_as_candidates(existing_file, "\n".join(sample))
    by_src: dict[str, MineCandidate] = {}
    for cand in harvested + mined:
        prev = by_src.get(cand.source)
        if prev is None or cand.score > prev.score:
            by_src[cand.source] = cand
    candidates = list(by_src.values())
    candidates.sort(key=lambda c: (-c.score, -c.count, c.source))
    if not candidates:
        if apply and title:
            _mark_harvested_seen(title)
        return empty

    own_client = False
    client = None
    complete = complete_fn
    proposals: list[GlossaryProposal] = []
    if complete is None:
        if _in_pytest():
            emit("Qwen glossary skipped — pytest (inject complete_fn to test the LLM path).")
        elif not allow_download and not polish_gguf_on_disk():
            emit("Qwen glossary skipped — polish GGUF is not on disk.")
        elif not qwen_glossary_capable():
            emit("Qwen glossary skipped — this machine is on a 3B polish profile.")
        else:
            emit("Starting local Qwen (llama.cpp, no model download)…")
            from core.polish.api import connect_engine

            client, _profile = connect_engine(
                auto_serve=True,
                download=allow_download,
                log=emit,
                temperature=0.0,
            )
            own_client = True

            def complete(system: str, user: str) -> str:
                return client.generate(
                    user,
                    system=system,
                    max_tokens=min(1024, max(256, 20 * len(candidates[:_MAX_CANDIDATES]))),
                )

    try:
        if complete is None:
            locked = _locked_sources()
            for cand in candidates[:_MAX_CANDIDATES]:
                term = filter_qwen_term(
                    cand.source,
                    cand.default_target,
                    kind=cand.kind,
                    notes="pinyin",
                    locked_sources=locked,
                )
                if term is None:
                    continue
                proposals.append(
                    GlossaryProposal(
                        novel_title=title,
                        source=term.source,
                        target=term.target,
                        kind=term.kind,
                        notes="pinyin",
                        evidence=cand.evidence,
                        count=cand.count,
                    )
                )
        else:
            emit(f"Asking Qwen to classify {min(len(candidates), _MAX_CANDIDATES)} term(s)…")
            xianxia = looks_like_xianxia(title, *sample[:8])
            proposals = classify_candidates(
                candidates,
                title=title,
                complete=complete,
                xianxia=xianxia,
                already=list(existing_file.terms),
                log=emit,
            )
    finally:
        if own_client and client is not None:
            try:
                client.close()
            except Exception:
                pass

    added = 0
    updated = 0
    if apply and title and proposals:
        incoming = [
            Term(source=p.source, target=p.target, kind=p.kind, notes=p.notes)
            for p in proposals
        ]
        a, u = merge_qwen_terms(existing_file, incoming)
        _mark_harvested_notes(existing_file)
        save_glossary_file(novel_glossary_path(title), existing_file)
        added, updated = a, u
        if engine is not None:
            merge_qwen_terms(engine.glossary, incoming)
            engine._rebuild()
    elif apply and title:
        _mark_harvested_seen(title)

    message = (
        f"Qwen glossary: {len(proposals)} proposed, {added} new, {updated} updated."
        if proposals
        else "Qwen glossary pass finished — nothing new to add."
    )
    emit(message)
    return {
        "added": added,
        "updated": updated,
        "cancelled": False,
        "message": message,
        "proposals": [p.to_dict() for p in proposals],
    }


def polish_glossaries_with_qwen(
    *,
    library_titles: Optional[list[str]] = None,
    books: Optional[list[dict[str, Any]]] = None,
    cache=None,
    cancelled: Optional[Cancelled] = None,
    log: Optional[LogFn] = None,
    complete_fn: Optional[CompleteFn] = None,
    apply: bool = True,
    allow_download: bool = False,
) -> dict[str, Any]:
    """
    Classify per-novel candidates (cache corpus when available).
    Does not write ``glossary-qwen.json``.
    """
    emit = log or (lambda _msg: None)
    stop = cancelled or (lambda: False)
    jobs: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for book in books or []:
        title = str(book.get("title") or "").strip()
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)
        jobs.append(book)
    for title in library_titles or []:
        name = (title or "").strip()
        if not name or name in seen_titles:
            continue
        seen_titles.add(name)
        jobs.append({"title": name})
    folder = novel_glossaries_dir()
    if folder.is_dir():
        for path in sorted(folder.glob("*.json")):
            name = path.stem
            if name in seen_titles:
                continue
            gloss = _load_json_glossary(path)
            if any((t.notes or "") == "harvested" for t in gloss.terms):
                seen_titles.add(name)
                jobs.append({"title": name})

    added = 0
    updated = 0
    all_proposals: list[dict[str, Any]] = []
    own_client = False
    client = None
    complete = complete_fn
    if complete is None and jobs and not _in_pytest():
        if not allow_download and not polish_gguf_on_disk():
            return {
                "added": 0,
                "updated": 0,
                "cancelled": False,
                "message": (
                    "Polish GGUF is not on disk. Tick Polish English once, then retry."
                ),
                "proposals": [],
            }
        if not qwen_glossary_capable():
            return {
                "added": 0,
                "updated": 0,
                "cancelled": False,
                "message": "This PC uses a 3B polish profile. Glossary Qwen needs 7B+.",
                "proposals": [],
            }
        emit("Starting local Qwen (llama.cpp, no model download)…")
        from core.polish.api import connect_engine

        client, _profile = connect_engine(
            auto_serve=True,
            download=allow_download,
            log=emit,
            temperature=0.0,
        )
        own_client = True

        def complete(system: str, user: str) -> str:
            n = max(256, min(1024, _MAX_CANDIDATES * 20))
            return client.generate(user, system=system, max_tokens=n)

    try:
        for book in jobs:
            if stop():
                return {
                    "added": added,
                    "updated": updated,
                    "cancelled": True,
                    "message": "Cancelled.",
                    "proposals": all_proposals,
                }
            title = str(book.get("title") or "").strip()
            texts = list(book.get("texts") or [])
            if not texts:
                texts = load_book_corpus(
                    title=title,
                    source_url=str(book.get("source_url") or ""),
                    description=str(book.get("description") or ""),
                    cache=cache,
                )
            emit(f"Glossary Qwen: {title or 'untitled'}…")
            result = classify_novel_with_qwen(
                novel_title=title,
                texts=texts,
                complete_fn=complete,
                cancelled=stop,
                log=emit,
                apply=apply,
                allow_download=allow_download,
            )
            added += int(result.get("added") or 0)
            updated += int(result.get("updated") or 0)
            all_proposals.extend(result.get("proposals") or [])
    finally:
        if own_client and client is not None:
            try:
                client.close()
            except Exception:
                pass

    message = (
        f"Qwen glossary pass: {len(all_proposals)} proposed, "
        f"{added} new, {updated} updated."
        if all_proposals
        else "Qwen glossary pass finished — nothing new to add."
    )
    emit(message)
    return {
        "added": added,
        "updated": updated,
        "cancelled": False,
        "message": message,
        "proposals": all_proposals,
    }
