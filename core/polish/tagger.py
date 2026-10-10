from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


def default_tagger_path() -> Path:
    return cache_dir() / "span_tagger.json"


def bundled_tagger_path() -> Path:
    return package_data_dir() / "span_tagger.json"


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


def _tagger_fingerprint(weights: list[float], anchors: list[str]) -> str:
    return hashlib.sha256(
        json.dumps({"w": weights, "a": anchors}, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]


def __getattr__(name: str):
    if name in {"train_tagger", "synthetic_examples"}:
        from core.polish import tagger_train
        return getattr(tagger_train, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
