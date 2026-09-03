"""Offline tests for Google/LibreTranslate segment packing."""

from core.translation.pack import (
    group_by_char_budget,
    pack_mt_segments,
    unpack_mt_segments,
)


def test_pack_unpack_preserves_order():
    texts = ["甲" * 10, "乙" * 10, "丙" * 10]
    assert unpack_mt_segments(pack_mt_segments(texts), 3) == texts


def test_unpack_rejects_missing_marker():
    assert unpack_mt_segments("just english", 2) is None


def test_single_segment_without_marker_is_ok():
    assert unpack_mt_segments("Hello there.", 1) == ["Hello there."]


def test_group_budget_splits_long_list():
    texts = ["中文" * 80 for _ in range(10)]
    groups = group_by_char_budget(texts, max_chars=400)
    assert len(groups) > 1
    flat = [i for g in groups for i in g]
    assert flat == list(range(10))
