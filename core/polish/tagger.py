from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from core.polish.detect import CJK_RE, cjk_ratio
from core.polish.glossary import Glossary
from core.polish.paths import cache_dir, env_value, package_data_dir
from core.polish.router import ARTIFACTS, mtl_score

# Seq2Edits-lite: sentence KEEP/REPLACE. CPU logistic model so it never
# sits on the GPU next to the 14B. Extra patterns are features only.
_EXTRA_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bat this moment\b", re.I),
    re.compile(r"\bin the next second\b", re.I),
    re.compile(r"\bit was only then\b", re.I),
    re.compile(r"\ba trace of\b", re.I),
    re.compile(r"\bhis heart (?:was|skipped|trembled|stirred)\b", re.I),
    re.compile(r"\bcouldn't help but\b", re.I),
    re.compile(r"\bas if he was\b", re.I),
    re.compile(r"\bwithout (?:him|her|them) noticing\b", re.I),
    re.compile(r"\bthe next moment\b", re.I),
    re.compile(r"\beyes (?:were|was) (?:full of|filled with)\b", re.I),
    re.compile(r"\bthat kind of\b", re.I),
    re.compile(r"\bone after another\b", re.I),
]

_LEAKY_RE = re.compile(
    r"\[(?:\d+|n)\]|^\s*REPLACE\s*:|KEEP (?:before|after)",
    re.I | re.M,
)
_FEATURE_VERSION = 2

_UNSET = object()
_LOADED: Any = _UNSET


def _sigmoid(z: float) -> float:
    z = max(-30.0, min(30.0, z))
    return 1.0 / (1.0 + math.exp(-z))


def feature_vector(
    text: str,
    mode: str = "polish",
    glossary: Glossary | None = None,
    tag: str = "p",
) -> list[float]:
    score = float(mtl_score(text, mode, glossary, tag))
    stripped = text.strip()
    letters = sum(ch.isalpha() for ch in stripped) or 1
    features = [
        1.0,
        score / 10.0,
        cjk_ratio(text),
        min(len(stripped), 400) / 400.0,
        1.0 if CJK_RE.search(text) else 0.0,
        stripped.count("  ") / max(len(stripped), 1),
        sum(ch in ",;" for ch in stripped) / max(len(stripped), 1) * 20.0,
        sum(1 for w in stripped.split() if w[:1].isupper()) / max(len(stripped.split()), 1),
        len(stripped.split()) / 40.0,
        1.0 if letters / max(len(stripped), 1) < 0.55 else 0.0,
    ]
    for pattern, _weight in ARTIFACTS:
        features.append(1.0 if pattern.search(text) else 0.0)
    for pattern in _EXTRA_PATTERNS:
        features.append(1.0 if pattern.search(text) else 0.0)
    return features


def leaky_model_text(text: str) -> bool:
    return bool(_LEAKY_RE.search(text or ""))


def _normalize(text: str) -> str:
    return " ".join((text or "").split()).lower()


def _has_mtl_cues(text: str) -> bool:
    if mtl_score(text, "polish") >= 1:
        return True
    return any(pattern.search(text) for pattern in _EXTRA_PATTERNS)


@dataclass
class LabeledSpan:
    text: str
    replace: bool
    source: str = "synthetic"


@dataclass
class SpanTagger:
    weights: list[float]
    threshold: float
    feature_version: int = _FEATURE_VERSION
    replace_recall: float = 0.0
    keep_rate: float = 0.0
    keep_precision: float = 0.0
    n_replace: int = 0
    n_keep: int = 0
    fingerprint: str = ""
    anchors: list[str] = field(default_factory=list)

    def _anchor_set(self) -> set[str]:
        cached = getattr(self, "_anchors_cached", None)
        if cached is None:
            cached = {a for a in self.anchors if a}
            self._anchors_cached = cached
        return cached

    def anchor_hit(self, text: str) -> bool:
        folded = _normalize(text)
        if not folded:
            return False
        anchors = self._anchor_set()
        if folded in anchors:
            return True
        if len(folded) < 48:
            return False
        for anchor in anchors:
            if len(anchor) >= 48 and (anchor in folded or folded in anchor):
                return True
        return False

    def logit(self, text: str, mode: str = "polish", glossary: Glossary | None = None, tag: str = "p") -> float:
        vec = feature_vector(text, mode, glossary, tag)
        if len(vec) != len(self.weights):
            return float(mtl_score(text, mode, glossary, tag))
        return sum(weight * value for weight, value in zip(self.weights, vec))

    def probability(self, text: str, mode: str = "polish", glossary: Glossary | None = None, tag: str = "p") -> float:
        return _sigmoid(self.logit(text, mode, glossary, tag))

    def is_replace(self, text: str, mode: str = "polish", glossary: Glossary | None = None, tag: str = "p") -> bool:
        if self.anchor_hit(text):
            return True
        return self.probability(text, mode, glossary, tag) >= self.threshold

    def strength(self, text: str, mode: str = "polish", glossary: Glossary | None = None, tag: str = "p") -> float:
        if self.anchor_hit(text):
            return 1.0
        return self.probability(text, mode, glossary, tag)

    def to_json(self) -> dict[str, Any]:
        return {
            "weights": self.weights,
            "threshold": self.threshold,
            "feature_version": self.feature_version,
            "replace_recall": self.replace_recall,
            "keep_rate": self.keep_rate,
            "keep_precision": self.keep_precision,
            "n_replace": self.n_replace,
            "n_keep": self.n_keep,
            "fingerprint": self.fingerprint,
            "anchors": self.anchors,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SpanTagger:
        weights = [float(x) for x in data["weights"]]
        anchors = [str(a) for a in data.get("anchors") or [] if str(a).strip()]
        digest = hashlib.sha256(
            json.dumps({"w": weights, "a": anchors}, sort_keys=True).encode("utf-8")
        ).hexdigest()[:12]
        return cls(
            weights=weights,
            threshold=float(data.get("threshold", 0.5)),
            feature_version=int(data.get("feature_version", _FEATURE_VERSION)),
            replace_recall=float(data.get("replace_recall", 0.0)),
            keep_rate=float(data.get("keep_rate", 0.0)),
            keep_precision=float(data.get("keep_precision", 0.0)),
            n_replace=int(data.get("n_replace", 0)),
            n_keep=int(data.get("n_keep", 0)),
            fingerprint=str(data.get("fingerprint") or digest),
            anchors=anchors,
        )


def examples_from_changelog(path: Path) -> list[LabeledSpan]:
    from core.polish.spans import replacement_ok, split_units

    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[LabeledSpan] = []
    for edit in data.get("edits") or []:
        before = str(edit.get("before") or "").strip()
        after = str(edit.get("after") or "").strip()
        if not before:
            continue
        for unit in split_units(before) or [before]:
            stripped = unit.strip()
            if stripped and _has_mtl_cues(stripped):
                out.append(LabeledSpan(stripped, True, str(path)))
        if after and not leaky_model_text(after) and replacement_ok(before, after):
            for unit in split_units(after) or [after]:
                if unit.strip() and not _has_mtl_cues(unit):
                    out.append(LabeledSpan(unit, False, str(path)))
    for item in data.get("unchanged") or []:
        before = str(item.get("before") or "").strip()
        if not before:
            continue
        for unit in split_units(before) or [before]:
            stripped = unit.strip()
            if not stripped:
                continue
            if _has_mtl_cues(stripped):
                out.append(LabeledSpan(stripped, True, str(path)))
            else:
                out.append(LabeledSpan(stripped, False, str(path)))
    return out


def load_changelog_paths(paths: Iterable[Path]) -> list[LabeledSpan]:
    examples: list[LabeledSpan] = []
    for path in paths:
        if path.is_file() and path.suffix.lower() == ".json":
            examples.extend(examples_from_changelog(path))
    return examples


def default_tagger_path() -> Path:
    return cache_dir() / "span_tagger.json"


def bundled_tagger_path() -> Path:
    return package_data_dir() / "span_tagger.json"


def save_tagger(tagger: SpanTagger, path: Path | None = None) -> Path:
    dest = path or default_tagger_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(tagger.to_json(), indent=2), encoding="utf-8")
    reset_tagger_cache()
    return dest


def load_tagger_file(path: Path) -> SpanTagger:
    return SpanTagger.from_json(json.loads(path.read_text(encoding="utf-8")))


def reset_tagger_cache() -> None:
    global _LOADED
    _LOADED = _UNSET


def get_tagger() -> SpanTagger | None:
    global _LOADED
    if _LOADED is not _UNSET:
        return _LOADED
    raw = env_value("HUAEPUB_POLISH_TAGGER")
    if raw.lower() in {"0", "off", "none", "heuristic"}:
        _LOADED = None
        return None
    candidates: list[Path] = []
    if raw:
        candidates.append(Path(raw))
    candidates.append(default_tagger_path())
    candidates.append(bundled_tagger_path())
    for path in candidates:
        if path.is_file():
            try:
                tagger = load_tagger_file(path)
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            if tagger.feature_version != _FEATURE_VERSION:
                continue
            if len(tagger.weights) != len(feature_vector("x")):
                continue
            _LOADED = tagger
            return tagger
    _LOADED = None
    return None


def tagger_id() -> str:
    tagger = get_tagger()
    if tagger is None:
        return "heur"
    return tagger.fingerprint or "learned"


def evaluate_against_changelog(path: Path, tagger: SpanTagger | None = None) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    gold_replace = []
    gold_keep = []
    leaky = 0
    for edit in data.get("edits") or []:
        before = str(edit.get("before") or "").strip()
        after = str(edit.get("after") or "").strip()
        if before:
            gold_replace.append(before)
        if after and leaky_model_text(after):
            leaky += 1
        elif after:
            gold_keep.append(after)
    for item in data.get("unchanged") or []:
        before = str(item.get("before") or "").strip()
        if before:
            gold_keep.append(before)

    def rate(texts: list[str], want_replace: bool, learned: bool) -> float:
        if not texts:
            return 1.0
        hits = 0
        for text in texts:
            if learned:
                pred = bool(tagger and tagger.is_replace(text))
            else:
                pred = mtl_score(text, "polish") >= 1
            hits += int(pred == want_replace)
        return hits / len(texts)

    learned = tagger or get_tagger()
    return {
        "file": str(path),
        "llm_spans_sent": data.get("llm_spans_sent"),
        "llm_edits": data.get("llm_edits") or len(data.get("edits") or []),
        "llm_unchanged": data.get("llm_unchanged"),
        "leaky_after": leaky,
        "gold_replace": len(gold_replace),
        "gold_keep": len(gold_keep),
        "heuristic_replace_recall": rate(gold_replace, True, False),
        "learned_replace_recall": rate(gold_replace, True, True) if learned else None,
        "heuristic_keep_rate": rate(gold_keep, False, False),
        "learned_keep_rate": rate(gold_keep, False, True) if learned else None,
        "tagger": learned.fingerprint if learned else None,
    }


def export_kd_pairs(path: Path) -> list[dict[str, str]]:
    from core.polish.spans import replacement_ok

    data = json.loads(path.read_text(encoding="utf-8"))
    pairs = []
    for edit in data.get("edits") or []:
        before = str(edit.get("before") or "").strip()
        after = str(edit.get("after") or "").strip()
        if not before or not after or leaky_model_text(after):
            continue
        if not replacement_ok(before, after):
            continue
        pairs.append({"source": before, "target": after})
    return pairs


def _tagger_fingerprint(weights: list[float], anchors: list[str]) -> str:
    return hashlib.sha256(
        json.dumps({"w": weights, "a": anchors}, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]


def merge_anchors(tagger: SpanTagger, extra: Iterable[str]) -> SpanTagger:
    merged = sorted({_normalize(item) for item in [*tagger.anchors, *extra] if _normalize(item)})
    tagger.anchors = merged
    tagger._anchors_cached = None  # type: ignore[attr-defined]
    tagger.fingerprint = _tagger_fingerprint(tagger.weights, merged)
    return tagger


def train_from_files(
    paths: list[Path],
    *,
    dest: Path | None = None,
    min_recall: float = 0.99,
    include_synthetic: bool = True,
    merge_existing: bool = True,
) -> tuple[SpanTagger, Path]:
    previous = get_tagger() if merge_existing else None
    examples = load_changelog_paths(paths)
    from core.polish.tagger_train import train_tagger as _train_tagger
    tagger = _train_tagger(examples, min_recall=min_recall, include_synthetic=include_synthetic)
    if previous and previous.anchors:
        merge_anchors(tagger, previous.anchors)
    return tagger, save_tagger(tagger, dest)


def __getattr__(name: str):
    if name in {"train_tagger", "synthetic_examples"}:
        from core.polish import tagger_train
        return getattr(tagger_train, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
