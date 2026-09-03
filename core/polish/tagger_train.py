from __future__ import annotations

import hashlib
import json

from core.polish.tagger import (
    LabeledSpan,
    SpanTagger,
    _normalize,
    _sigmoid,
    feature_vector,
)


def synthetic_examples() -> list[LabeledSpan]:
    # Imported lazily-safe: tagger re-exports this after load. Defined here.
    replace_seeds = [
        "He could not help but smile at the news.",
        "The corners of his mouth curled up slightly.",
        "His eyes flashed with a cold light.",
        "She sucked in a breath of cold air.",
        "In the next moment, the youth vanished.",
        "He did not expect that the elder would agree.",
        "This was a heaven-defying treasure.",
        "He said in a deep voice, \"Leave.\"",
        "The youth clenched his fists tightly.",
        "He suddenly discovered that the formation had changed.",
        "A look of shock flashed across his face.",
        "This daddy will not spare you.",
        "That is very much so the case.",
        "Incontinently he rushed forward.",
        "He saw the the mountain peak ahead.",
        "The man already was standing there.",
        "She was very much unwilling.",
        "His jindan trembled as qi surged.",
        "At this moment, his heart was filled with killing intent.",
        "In the next second, it was only then that he understood.",
        "A trace of fear appeared in her eyes.",
        "He couldn't help but take a step back.",
        "It was as if he was looking at an ant.",
        "Without him noticing, night had fallen.",
        "His eyes were full of unwillingness.",
        "One after another, experts arrived.",
        "He already was very much so in the next moment.",
        "The corners of her mouth rose as her eyes shone.",
        "Sucked in a cold air, the youth said in a heavy voice.",
    ]
    keep_seeds = [
        "The mountain wind was cold against his face.",
        "Then he walked toward the sect gate.",
        "The valley was quiet after the storm.",
        "She set the cup down and waited.",
        "Clouds hung low over the river.",
        "Luo Feng did not answer at once.",
        "The hall doors opened onto torchlight.",
        "Rain ticked against the tiled roof.",
        "He counted the remaining arrows twice.",
        "No one spoke for a long time.",
        "The map showed three unmarked passes.",
        "Her voice was steady, almost bored.",
        "They crossed the bridge before dawn.",
        "A single lantern marked the warehouse.",
        "He remembered the lesson and slowed his breath.",
        "The elder nodded once and left it at that.",
        "Snow had drifted against the north wall.",
        "It was a small room with a clean floor.",
        "The letter had no seal and no name.",
        "She tied the horse and checked the cinch.",
        "Morning found the camp already struck.",
        "He kept the token inside his sleeve.",
        "The road east was empty for miles.",
        "Nothing in the ledger matched the story.",
        "They ate in silence and slept in shifts.",
        "The sword stayed in its sheath.",
        "A dog barked once, then thought better of it.",
        "He closed the book without marking the page.",
        "The tide had pulled the boats sideways.",
        "She smiled, and this time it reached her eyes.",
        "The formation lines were still intact.",
        "Golden Core was a distant problem.",
        "He bowed, received the token, and left.",
        "The night watch changed at the third drum.",
        "None of the names on the list were his.",
        "The tea had gone cold beside the window.",
        "He knew the path well enough to walk it dark.",
        "A child waved from the upper window.",
        "The contract was short and badly copied.",
        "They would not reach the city before snow.",
    ]
    out = [LabeledSpan(text, True, "synthetic") for text in replace_seeds]
    out.extend(LabeledSpan(text, False, "synthetic") for text in keep_seeds)
    return out


def _fit_weights(examples: list[LabeledSpan], epochs: int = 450, lr: float = 0.25, l2: float = 0.02) -> list[float]:
    dim = len(feature_vector("x"))
    weights = [0.0] * dim
    n_replace = sum(1 for ex in examples if ex.replace) or 1
    n_keep = sum(1 for ex in examples if not ex.replace) or 1
    w_pos = n_keep / n_replace
    w_neg = n_replace / n_keep
    scale = 2.0 / (w_pos + w_neg)
    w_pos *= scale
    w_neg *= scale
    vectors = [(feature_vector(ex.text), 1.0 if ex.replace else 0.0, w_pos if ex.replace else w_neg) for ex in examples]
    n = len(vectors)
    for _ in range(epochs):
        grad = [l2 * w for w in weights]
        grad[0] = 0.0
        for vec, y, weight in vectors:
            z = sum(a * b for a, b in zip(weights, vec))
            err = _sigmoid(z) - y
            coef = weight * err / n
            for i, value in enumerate(vec):
                grad[i] += coef * value
        for i, g in enumerate(grad):
            weights[i] -= lr * g
    return weights


def _metrics(weights: list[float], examples: list[LabeledSpan], threshold: float) -> tuple[float, float, float]:
    replace = [ex for ex in examples if ex.replace]
    keep = [ex for ex in examples if not ex.replace]
    def pred(ex: LabeledSpan) -> bool:
        z = sum(w * v for w, v in zip(weights, feature_vector(ex.text)))
        return _sigmoid(z) >= threshold

    tp = sum(1 for ex in replace if pred(ex))
    tn = sum(1 for ex in keep if not pred(ex))
    recall = tp / max(len(replace), 1)
    keep_rate = tn / max(len(keep), 1)
    predicted_keep = sum(1 for ex in examples if not pred(ex))
    keep_precision = tn / max(predicted_keep, 1)
    return recall, keep_rate, keep_precision


def _choose_threshold(weights: list[float], examples: list[LabeledSpan], min_recall: float) -> float:
    replace_scores = sorted(
        _sigmoid(sum(w * v for w, v in zip(weights, feature_vector(ex.text))))
        for ex in examples
        if ex.replace
    )
    if not replace_scores:
        return 0.5
    candidates = sorted({round(s - 0.002, 3) for s in replace_scores} | {0.15, 0.25, 0.35, 0.45, 0.5})
    best = min(replace_scores)
    best_keep = -1.0
    for threshold in candidates:
        recall, keep_rate, _prec = _metrics(weights, examples, threshold)
        if recall + 1e-9 < min_recall:
            continue
        if keep_rate > best_keep:
            best_keep = keep_rate
            best = threshold
    return best


def train_tagger(
    examples: list[LabeledSpan],
    *,
    min_recall: float = 0.99,
    include_synthetic: bool = True,
) -> SpanTagger:
    pooled = list(examples)
    if include_synthetic:
        pooled.extend(synthetic_examples())
    if not pooled:
        raise ValueError("No tagger examples to train on.")
    anchors = sorted(
        {
            _normalize(ex.text)
            for ex in examples
            if ex.replace and len(_normalize(ex.text)) >= 12
        }
    )
    logistic: list[LabeledSpan] = []
    for ex in pooled:
        if ex.replace and ex.source != "synthetic":
            continue
        logistic.append(ex)
    if not logistic:
        logistic = synthetic_examples() if include_synthetic else pooled
    weights = _fit_weights(logistic)
    threshold = _choose_threshold(weights, logistic, min_recall)
    recall, keep_rate, keep_precision = _metrics(weights, logistic, threshold)
    n_replace = sum(1 for ex in logistic if ex.replace)
    n_keep = sum(1 for ex in logistic if not ex.replace)
    digest = hashlib.sha256(
        json.dumps({"w": weights, "a": anchors}, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    return SpanTagger(
        weights=weights,
        threshold=threshold,
        replace_recall=recall,
        keep_rate=keep_rate,
        keep_precision=keep_precision,
        n_replace=n_replace,
        n_keep=n_keep,
        fingerprint=digest,
        anchors=anchors,
    )
