# Author: joelsnl
"""
Novel translation extras: glossary protect/restore, optional CTranslate2,
and the NovelTranslator facade used by the download pipeline.

Lives under core/ (not a separate huaepub/ package). HTTP engines stay in
core.translator.GoogleTranslator. Never Drive-sync ~/.huaepub/nmt/,
glossary.json, glossary-qwen.json, or glossaries/.
"""

from core.translation.glossary import (
    GlossaryEngine,
    build_novel_glossary,
    load_default_novel_glossary,
    load_novel_glossary_file,
    load_user_glossary,
    looks_like_xianxia,
    user_glossary_path,
)
from core.translation.harvest import harvest_and_apply, harvest_candidates
from core.translation.nmt import (
    CTranslate2Engine,
    nmt_cache_dir,
    nmt_model_ready,
    nmt_runtime_available,
)
from core.translation.novel_translator import NovelTranslator

__all__ = [
    "CTranslate2Engine",
    "GlossaryEngine",
    "NovelTranslator",
    "build_novel_glossary",
    "harvest_and_apply",
    "harvest_candidates",
    "load_default_novel_glossary",
    "load_novel_glossary_file",
    "load_user_glossary",
    "looks_like_xianxia",
    "nmt_cache_dir",
    "nmt_model_ready",
    "nmt_runtime_available",
    "user_glossary_path",
]
