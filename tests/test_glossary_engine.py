"""Offline tests for the novel glossary protect/restore engine."""

from core.polish.glossary import Glossary, Term
from core.translation.glossary import (
    GlossaryEngine,
    build_novel_glossary,
    load_builtin_glossary,
    looks_like_xianxia,
    normalize_glossary_mode,
)


def test_builtin_includes_grand_elder():
    gloss = load_builtin_glossary()
    sources = {t.source for t in gloss.terms}
    assert "大长老" in sources
    assert "金丹" in sources
    assert "道友" in sources


def test_protect_longest_match_first():
    engine = GlossaryEngine(load_builtin_glossary())
    job = engine.protect("大长老与长老一同前来。")
    assert "大长老" not in job.text
    assert "长老" not in job.text
    assert "§G0§" in job.text
    restored = engine.restore(job.text, job)
    assert "Grand Elder" in restored
    assert "Elder" in restored
    assert "大长老" not in restored


def test_custom_character_name():
    engine = GlossaryEngine(Glossary())
    engine.add_terms([("韩立", "Han Li")])
    job = engine.protect("韩立走出洞府。")
    assert "韩立" not in job.text
    out = engine.restore("§G0§ left the cave.", job)
    assert out.startswith("Han Li")


def test_restore_tolerates_spaces_in_token():
    engine = GlossaryEngine(Glossary())
    engine.add_terms([("大长老", "Grand Elder")])
    job = engine.protect("大长老来了")
    assert engine.restore("§ G0 § arrived.", job) == "Grand Elder arrived."


def test_leftover_chinese_replaced_after_nmt():
    engine = GlossaryEngine(load_builtin_glossary())
    job = engine.protect("无关文本")
    out = engine.restore("The 大长老 nodded.", job)
    assert "Grand Elder" in out
    assert "大长老" not in out


def test_fingerprint_changes_when_terms_change():
    engine = GlossaryEngine(Glossary())
    a = engine.fingerprint
    engine.add_terms([Term(source="韩立", target="Han Li")])
    assert engine.fingerprint != a


def test_empty_glossary_is_noop():
    engine = GlossaryEngine(Glossary())
    job = engine.protect("大长老")
    assert job.text == "大长老"
    assert engine.restore(job.text, job) == "大长老"


def test_builtin_pack_is_a_fixed_list():
    gloss = load_builtin_glossary()
    assert 200 <= len(gloss.terms) <= 500
    assert "玉简" in {t.source for t in gloss.terms}


def test_normalize_glossary_mode():
    assert normalize_glossary_mode(None) == "auto"
    assert normalize_glossary_mode("Cultivation") == "xianxia"
    assert normalize_glossary_mode("names") == "user"
    assert normalize_glossary_mode("off") == "off"


def test_looks_like_xianxia_strong_and_weak():
    assert looks_like_xianxia("凡人修仙传")
    assert looks_like_xianxia("穿越凡人修仙低调修仙")
    assert looks_like_xianxia("宗门长老大会")
    assert looks_like_xianxia("I Shall Seal the Heavens: a xianxia epic")
    assert not looks_like_xianxia("公子回来了")
    assert not looks_like_xianxia("凡人的日常")
    assert not looks_like_xianxia("霸道总裁爱上我")
    assert not looks_like_xianxia("长老说了一句话")
    assert not looks_like_xianxia("")


def test_auto_skips_builtin_for_urban(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "core.translation.glossary.user_glossary_path",
        lambda: tmp_path / "glossary.json",
    )
    monkeypatch.setattr(
        "core.translation.glossary.qwen_glossary_path",
        lambda: tmp_path / "glossary-qwen.json",
    )
    monkeypatch.setattr(
        "core.translation.glossary.novel_glossary_path",
        lambda title: tmp_path / "glossaries" / f"{title}.json",
    )
    engine = build_novel_glossary(
        novel_title="霸道总裁爱上我",
        mode="auto",
        detect_text="都市恋爱 霸道总裁爱上我",
    )
    assert engine is not None
    sources = {t.source for t in engine.glossary.terms}
    assert "公子" not in sources
    assert "金丹" not in sources
    assert "凡人" not in sources


def test_auto_includes_builtin_for_xianxia(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "core.translation.glossary.user_glossary_path",
        lambda: tmp_path / "glossary.json",
    )
    monkeypatch.setattr(
        "core.translation.glossary.qwen_glossary_path",
        lambda: tmp_path / "glossary-qwen.json",
    )
    monkeypatch.setattr(
        "core.translation.glossary.novel_glossary_path",
        lambda title: tmp_path / "glossaries" / f"{title}.json",
    )
    engine = build_novel_glossary(novel_title="凡人修仙传", mode="auto")
    sources = {t.source for t in engine.glossary.terms}
    assert "金丹" in sources
    assert "大长老" in sources


def test_user_names_apply_when_pack_is_skipped(tmp_path, monkeypatch):
    (tmp_path / "glossary.json").write_text(
        '{"terms": [{"source": "林婉儿", "target": "Lin Waner"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "core.translation.glossary.user_glossary_path",
        lambda: tmp_path / "glossary.json",
    )
    monkeypatch.setattr(
        "core.translation.glossary.qwen_glossary_path",
        lambda: tmp_path / "glossary-qwen.json",
    )
    monkeypatch.setattr(
        "core.translation.glossary.novel_glossary_path",
        lambda title: tmp_path / "missing.json",
    )
    engine = build_novel_glossary(
        novel_title="霸道总裁",
        mode="auto",
        detect_text="霸道总裁",
    )
    job = engine.protect("林婉儿走进办公室。")
    assert "林婉儿" not in job.text
    assert engine.restore(job.text, job).startswith("Lin Waner")
    sources = {t.source for t in engine.glossary.terms}
    assert "金丹" not in sources


def test_legacy_qwen_global_skips_urban(tmp_path, monkeypatch):
    (tmp_path / "glossary-qwen.json").write_text(
        '{"terms":[{"source":"古河","target":"Gu He","type":"character"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "core.translation.glossary.user_glossary_path",
        lambda: tmp_path / "glossary.json",
    )
    monkeypatch.setattr(
        "core.translation.glossary.qwen_glossary_path",
        lambda: tmp_path / "glossary-qwen.json",
    )
    monkeypatch.setattr(
        "core.translation.glossary.novel_glossary_path",
        lambda title: tmp_path / "missing.json",
    )
    urban = build_novel_glossary(
        novel_title="霸道总裁爱上我",
        mode="auto",
        detect_text="都市恋爱 霸道总裁爱上我",
    )
    assert "古河" not in {t.source for t in urban.glossary.terms}
    xianxia = build_novel_glossary(novel_title="凡人修仙传", mode="auto")
    assert "古河" in {t.source for t in xianxia.glossary.terms}


def test_off_returns_none():
    assert build_novel_glossary(mode="off") is None
