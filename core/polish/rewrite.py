# Author: joelsnl
"""KEEP/REPLACE rewrite helpers used by novel polish (no EPUB I/O)."""

from __future__ import annotations

from collections.abc import Callable

from core.polish.engine import LLMEngine
from core.polish.glossary import Glossary
from core.polish.prompts import (
    POLISH_SYSTEM,
    TRANSLATE_SYSTEM,
    build_span_user_prompt,
    parse_numbered,
    span_prefix_text,
    span_system_prompt,
)
from core.polish.qwen_tokens import prompt_token_budget, qwen_token_count
from core.polish.spans import SpanJob, replacement_ok, trim_echo


def max_tokens_for_spans(
    jobs: list[SpanJob],
    num_ctx: int,
    count_tokens: Callable[[str], int] | None = None,
) -> int:
    n = max(1, len(jobs))
    if count_tokens:
        replace_tokens = sum(count_tokens(job.text) for job in jobs)
        estimate = int(replace_tokens * 1.35) + 24 * n
    else:
        chars = sum(len(job.text) for job in jobs)
        estimate = int(chars * 0.85) + 32 * n
    cap = max(128, num_ctx // 2)
    return max(48 * n, min(estimate, cap, 2048))


def _gate_span_outputs(jobs: list[SpanJob], parsed: list[str]) -> tuple[list[str], int]:
    gated: list[str] = []
    good = 0
    for job, raw in zip(jobs, parsed):
        out = trim_echo(raw, job.text, job.before, job.after)
        if replacement_ok(job.text, out):
            gated.append(out)
            good += 1
        else:
            gated.append(job.text)
    return gated, good


def rewrite_span_jobs(
    client: LLMEngine,
    jobs: list[SpanJob],
    glossary: Glossary,
    previous: str,
    style: str,
    retries: int,
    num_ctx: int,
    *,
    glossary_block: str = "",
    count_tokens: Callable[[str], int] | None = None,
    speculate: bool = True,
    n_keep: int = 0,
) -> list[str]:
    del previous
    if not glossary_block:
        numbered = " ".join(job.text for job in jobs)
        glossary_block = glossary.as_prompt(numbered) if glossary.unapplied_hits(numbered) else ""
    system = span_system_prompt(glossary_block, style)
    user = build_span_user_prompt(jobs)
    last = ""
    max_tokens = max_tokens_for_spans(jobs, num_ctx, count_tokens=count_tokens)
    stop = ["KEEP before", "KEEP after"]
    if len(jobs) == 1:
        stop.extend(["\n[1]", "\n[2]"])
    for attempt in range(retries + 1):
        prompt = user
        if attempt:
            prompt += f"\n\nOutput [{len(jobs)}] numbered REPLACE block(s) only."
        last = client.generate(
            prompt,
            system=system,
            max_tokens=max_tokens,
            stop=stop,
            speculate=speculate and attempt == 0,
            n_keep=n_keep,
        )
        parsed = parse_numbered(last, len(jobs))
        if not parsed:
            continue
        gated, good = _gate_span_outputs(jobs, parsed)
        if good:
            return gated
    parsed = parse_numbered(last, len(jobs))
    if parsed:
        gated, good = _gate_span_outputs(jobs, parsed)
        if good:
            return gated
    if len(jobs) > 1:
        mid = max(1, len(jobs) // 2)
        print(f"REPLACE pack of {len(jobs)} failed checks; splitting.")
        return rewrite_span_jobs(
            client,
            jobs[:mid],
            glossary,
            "",
            style,
            retries,
            num_ctx,
            glossary_block=glossary_block,
            count_tokens=count_tokens,
            speculate=speculate,
            n_keep=n_keep,
        ) + rewrite_span_jobs(
            client,
            jobs[mid:],
            glossary,
            "",
            style,
            retries,
            num_ctx,
            glossary_block=glossary_block,
            count_tokens=count_tokens,
            speculate=speculate,
            n_keep=n_keep,
        )
    return [job.text for job in jobs]


def pack_kwargs(
    *,
    max_chars: int,
    num_ctx: int,
    token_pack: bool,
    glossary_block: str = "",
    style: str = "",
    span_mode: bool = False,
    mode: str = "polish",
) -> dict:
    count_tokens = qwen_token_count if token_pack else None
    prefix_tokens = 0
    max_prompt_tokens = 0
    if count_tokens:
        max_prompt_tokens = prompt_token_budget(num_ctx)
        if span_mode:
            prefix_tokens = count_tokens(span_prefix_text(glossary_block, style))
        else:
            system = POLISH_SYSTEM if mode == "polish" else TRANSLATE_SYSTEM
            prefix_tokens = count_tokens(
                system + "\n\nPolish these passages. Keep the same [n] labels:\n\n"
            )
    return {
        "max_chars": max_chars,
        "max_prompt_tokens": max_prompt_tokens,
        "prefix_tokens": prefix_tokens,
        "count_tokens": count_tokens,
    }
