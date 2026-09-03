"""Offline tests for Qwen glossary classify (no GPU)."""

from core.polish.glossary import Glossary, Term
from core.translation.harvest import MineCandidate
from core.translation.qwen_glossary import (
    apply_glossary_proposals,
    filter_qwen_term,
    merge_qwen_terms,
    parse_classifier_output,
    polish_glossaries_with_qwen,
    should_offer_glossary_qwen,
    terms_from_qwen_json,
)


def test_filter_rejects_everyday_chinese():
    assert filter_qwen_term("的", "of") is None
    assert filter_qwen_term("因为", "because") is None
    assert filter_qwen_term("一个", "one") is None
    assert filter_qwen_term("韩立", "Han Li went to the mountain to cultivate.") is None


def test_filter_accepts_names_and_ranks():
    name = filter_qwen_term("韩立", "Han Li")
    assert name is not None and name.target == "Han Li"
    rank = filter_qwen_term("玉简", "jade slip")
    assert rank is not None
    locked = filter_qwen_term("韩立", "Han Li", locked_sources={"韩立"})
    assert locked is None


def test_parse_fenced_json_and_skip_junk():
    raw = """```json
{"terms":[
  {"source":"韩立","target":"Han Li","type":"character"},
  {"source":"因为","target":"because","type":"other"},
  {"source":"玉简","target":"jade slip","type":"item"}
]}
```"""
    terms = terms_from_qwen_json(raw, locked_sources=set())
    sources = {t.source for t in terms}
    assert "韩立" in sources
    assert "玉简" in sources
    assert "因为" not in sources


def test_parse_tsv_by_id_drops_unknown_sources():
    cands = [
        MineCandidate("韩立", "character", 8, 4, default_target="Han Li"),
        MineCandidate("黄枫谷", "place", 6, 3, default_target="Huang Feng Gu"),
    ]
    raw = (
        "1\tfix\tHan Li\tcharacter\n"
        "2\tkeep\tHuang Feng Gu\tplace\n"
        "9\tfix\tJade Emperor\tcharacter\n"
    )
    parsed = parse_classifier_output(raw, cands)
    sources = {c.source for c, _a, _t, _k in parsed}
    assert sources == {"韩立", "黄枫谷"}
    by = {c.source: (a, t) for c, a, t, _k in parsed}
    assert by["韩立"] == ("fix", "Han Li")


def test_json_cannot_invent_sources():
    cands = [MineCandidate("韩立", "character", 8, 4, default_target="Han Li")]
    raw = (
        '{"terms":[{"source":"玉清仙帝","target":"Jade Emperor","type":"character"},'
        '{"id":1,"target":"Han Li","action":"fix","type":"character"}]}'
    )
    parsed = parse_classifier_output(raw, cands)
    assert [c.source for c, *_ in parsed] == ["韩立"]


def test_merge_overwrites_harvested_not_user():
    gloss = Glossary()
    gloss.add(Term(source="韩立", target="Cold Stand", kind="character", notes="harvested"))
    gloss.add(Term(source="王林", target="Wang Lin", kind="character", notes=""))
    incoming = [
        Term(source="韩立", target="Han Li", kind="character", notes="qwen"),
        Term(source="王林", target="King Forest", kind="character", notes="qwen"),
        Term(source="张浩", target="Zhang Hao", kind="character", notes="qwen"),
    ]
    added, updated = merge_qwen_terms(gloss, incoming)
    by = {t.source: t for t in gloss.terms}
    assert by["韩立"].target == "Han Li"
    assert by["王林"].target == "Wang Lin"
    assert by["张浩"].target == "Zhang Hao"
    assert added == 1
    assert updated == 1
    gloss.add(Term(source="古河", target="Gu He", kind="character", notes="harvested"))
    added2, updated2 = merge_qwen_terms(
        gloss,
        [Term(source="古河", target="Gu He", kind="character", notes="qwen")],
    )
    assert added2 == 0
    assert updated2 == 1
    assert {t.source: t.notes for t in gloss.terms}["古河"] == "qwen"


def test_should_offer_rules():
    base = {"glossary_qwen_ask": True, "translation_glossary": "auto"}
    assert should_offer_glossary_qwen(
        base, has_library=True, has_harvested=False, now=100,
        model_ready=True, qwen_capable=True,
    )
    assert should_offer_glossary_qwen(
        {**base, "glossary_qwen_last_at": 50},
        has_library=True,
        has_harvested=True,
        now=60,
        model_ready=True,
        qwen_capable=True,
    )
    assert not should_offer_glossary_qwen(
        {**base, "glossary_qwen_last_at": 50},
        has_library=True,
        has_harvested=False,
        now=60,
        model_ready=True,
        qwen_capable=True,
    )
    assert not should_offer_glossary_qwen(
        {**base, "glossary_qwen_ask": False},
        has_library=True,
        has_harvested=True,
        model_ready=True,
        qwen_capable=True,
    )
    assert not should_offer_glossary_qwen(
        {**base, "translation_glossary": "off"},
        has_library=True,
        has_harvested=True,
        model_ready=True,
        qwen_capable=True,
    )
    assert should_offer_glossary_qwen(
        {**base, "glossary_qwen_ask": False},
        has_library=False,
        has_harvested=False,
        force=True,
    )
    assert not should_offer_glossary_qwen(
        base,
        has_library=True,
        has_harvested=True,
        model_ready=False,
        qwen_capable=True,
    )
    assert not should_offer_glossary_qwen(
        base,
        has_library=True,
        has_harvested=True,
        model_ready=True,
        qwen_capable=False,
    )


def test_polish_with_injected_complete(tmp_path, monkeypatch):
    gloss_dir = tmp_path / "glossaries"
    gloss_dir.mkdir()

    def novel_path(title):
        return gloss_dir / f"{title}.json"

    monkeypatch.setattr(
        "core.translation.qwen_glossary.user_glossary_path",
        lambda: tmp_path / "glossary.json",
    )
    monkeypatch.setattr(
        "core.translation.qwen_glossary.novel_glossaries_dir",
        lambda: gloss_dir,
    )
    monkeypatch.setattr(
        "core.translation.qwen_glossary.novel_glossary_path",
        novel_path,
    )
    (gloss_dir / "book.json").write_text(
        '{"terms":[{"source":"韩立","target":"Cold Stand","type":"character","notes":"harvested"}]}',
        encoding="utf-8",
    )

    def complete(system, user):
        assert "Cold Stand" in user or "韩立" in user
        return "1\tfix\tHan Li\tcharacter"

    result = polish_glossaries_with_qwen(
        library_titles=["凡人修仙传"],
        complete_fn=complete,
        apply=True,
    )
    assert result["cancelled"] is False
    novel = (gloss_dir / "book.json").read_text(encoding="utf-8")
    assert "Han Li" in novel
    assert "Cold Stand" not in novel
    invented = [p for p in result["proposals"] if p.get("source") == "玉清仙帝"]
    assert invented == []


def test_apply_proposals_writes_per_novel_only(tmp_path, monkeypatch):
    gloss_dir = tmp_path / "glossaries"
    gloss_dir.mkdir()
    monkeypatch.setattr(
        "core.translation.qwen_glossary.novel_glossary_path",
        lambda title: gloss_dir / f"{title}.json",
    )
    added, updated = apply_glossary_proposals(
        [
            {
                "novel_title": "凡人修仙传",
                "source": "韩立",
                "target": "Han Li",
                "kind": "character",
                "notes": "qwen",
            }
        ]
    )
    assert added == 1
    text = (gloss_dir / "凡人修仙传.json").read_text(encoding="utf-8")
    assert "Han Li" in text
    assert updated == 0
