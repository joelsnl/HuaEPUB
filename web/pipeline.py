# Author: joelsnl and Anthropic Claude
"""The one place HuaEPUB Simple sequences fetch then build.

``run_single_download`` is deliberately not used: it records the book in the
desktop library. This mirrors its sequencing without library recording,
polish, or resume persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.download_runner import (
    DownloadControl,
    build_epub,
    completion_has_warnings,
    download_chapters_with_cache,
    engines_for_chapter_fetch,
    format_completion_notes,
)
from core.parser import Chapter, NovelInfo

# report(state, fraction_or_None, message): state is fetching/translating/writing
ReportFn = Callable[[str, Optional[float], str], None]


@dataclass
class PipelineResult:
    path: str
    notes: str = ""
    has_warnings: bool = False
    flagged: List[int] = field(default_factory=list)  # 0-based positions in the slice


def _flagged_positions(chapters: List[Chapter], titles: List[str]) -> List[int]:
    wanted = set(titles)
    return [pos for pos, ch in enumerate(chapters) if ch.title in wanted]


def run_pipeline(
    *,
    control: DownloadControl,
    cache: Any,
    parser: Any,
    info: NovelInfo,
    chapters: List[Chapter],
    output_path: str,
    report: ReportFn,
    clean: bool = True,
    translate: bool = True,
    backend: str = "google",
    workers: int = 200,
    glossary_mode: str = "auto",
    use_cache: bool = True,
) -> PipelineResult:
    """Fetch (sequential, cached) then clean, translate, and write one EPUB.

    Raises ``DownloadCancelled`` when the control is cancelled before the file
    is committed. Polish is always off here, whatever ``settings.json`` says.
    """
    workers = max(1, min(int(workers or 200), 200))
    book_key = info.source_url if info else ""
    state: Dict[str, str] = {"phase": "fetching", "msg": ""}

    def set_status(text: str) -> None:
        if text:
            state["msg"] = text
            report(state["phase"], None, text)

    def fetch_progress(fraction: float, status: str = "") -> None:
        if status:
            state["msg"] = status
        report("fetching", fraction, state["msg"])

    def build_progress(fraction: float, status: str = "") -> None:
        if status:
            state["msg"] = status
        low = state["msg"].lower()
        if not translate or low.startswith("writing"):
            state["phase"] = "writing"
        else:
            state["phase"] = "translating"
        report(state["phase"], fraction, state["msg"])

    translator, cleaner = engines_for_chapter_fetch(
        cache=cache,
        workers=workers,
        backend=backend,
        libretranslate_url="https://libretranslate.com",
        ollama_url="http://127.0.0.1:11434",
        ollama_model="qwen2.5:3b",
        clean=clean,
        translate=translate,
        glossary_mode=glossary_mode,
        novel_info=info,
        chapters=chapters,
    )

    failed = download_chapters_with_cache(
        control=control,
        cache=cache,
        parser=parser,
        chapters=chapters,
        book_key=book_key,
        use_cache=use_cache,
        set_status=set_status,
        set_progress=fetch_progress,
        translator=translator,
        cleaner=cleaner,
    )

    state["phase"] = "translating" if translate else "writing"
    build_progress(0.0, "Translating chapters…" if translate else "Writing EPUB…")
    result = build_epub(
        control=control,
        cache=cache,
        info=info,
        chapters=chapters,
        output_path=output_path,
        clean=clean,
        translate=translate,
        workers=workers,
        backend=backend,
        libretranslate_url="https://libretranslate.com",
        ollama_url="http://127.0.0.1:11434",
        ollama_model="qwen2.5:3b",
        ollama_polish=False,
        glossary_mode=glossary_mode,
        set_status=set_status,
        set_progress=build_progress,
        translator=translator,
        cleaner=cleaner,
    )

    notes = format_completion_notes(
        failed_chapters=failed,
        translation_warnings=result.translation_warnings,
        polish_cancelled=False,
        heuristic_chapters=result.heuristic_chapters,
    )
    flagged_titles = (
        list(failed)
        + [title for title, _count in result.translation_warnings]
        + list(result.heuristic_chapters)
    )
    return PipelineResult(
        path=result.output_path,
        notes=notes,
        has_warnings=completion_has_warnings(notes),
        flagged=_flagged_positions(chapters, flagged_titles),
    )
