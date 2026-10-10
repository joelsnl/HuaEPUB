from __future__ import annotations

import re


CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
LATIN_RE = re.compile(r"[A-Za-z]")
FOREIGN_SCRIPT_RE = re.compile(
    r"["
    r"\u0400-\u04ff"
    r"\u0600-\u06ff"
    r"\u0900-\u097f"
    r"\u0e00-\u0e7f"
    r"\u1100-\u11ff"
    r"\u3040-\u30ff"
    r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    r"\uac00-\ud7af"
    r"]"
)


def cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    cjk = len(CJK_RE.findall(text))
    latin = len(LATIN_RE.findall(text))
    total = cjk + latin
    if total == 0:
        return 0.0
    return cjk / total


def foreign_script_ratio(text: str) -> float:
    """Share of letters that are not Latin (CJK, kana, Hangul, Arabic, …)."""
    if not text:
        return 0.0
    foreign = len(FOREIGN_SCRIPT_RE.findall(text))
    latin = len(LATIN_RE.findall(text))
    total = foreign + latin
    if total == 0:
        return 0.0
    return foreign / total


