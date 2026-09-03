"""Offline tests for per-novel name harvest."""

from core.polish.glossary import Glossary, Term
from core.translation.glossary import GlossaryEngine
from core.translation.harvest import (
    format_personal_pinyin,
    harvest_and_apply,
    harvest_candidates,
    is_usable_name_target,
    mine_glossary_candidates,
    persist_harvested_terms,
    render_harvested_names,
    strip_honorific,
)


def test_harvests_said_and_named_patterns():
    text = (
        "此人名叫韩立。韩立说道：“我去闭关。”"
        "王林问道：“可是为何？”韩立笑道：“机缘。”"
        "又有人叫做张浩。"
    )
    names = harvest_candidates([text])
    assert "韩立" in names
    assert "王林" in names
    assert "张浩" in names
    assert "说道" not in names
    assert "闭关" not in names


def test_harvest_skips_stopwords_and_existing_terms():
    gloss = GlossaryEngine(Glossary())
    gloss.add_terms([Term(source="韩立", target="Han Li")])
    text = "韩立说道。于是说道。两人笑道。长老问道。"
    names = harvest_candidates([text], existing=gloss)
    assert "韩立" not in names
    assert "于是" not in names
    assert "两人" not in names
    assert "长老" not in names


def test_usable_name_target():
    assert is_usable_name_target("韩立", "Han Li")
    assert is_usable_name_target("林婉儿", "Lin Wan'er")
    assert not is_usable_name_target("韩立", "韩立")
    assert not is_usable_name_target("凡人", "mortal")
    assert not is_usable_name_target("韩立", "Han Li went to the mountain to cultivate.")
    assert not is_usable_name_target("韩立", "Han 立")


def test_render_filters_bad_engine_output():
    pairs = render_harvested_names(
        ["韩立", "凡人", "张浩"],
        lambda srcs: ["Han Li", "mortal", "Zhang Hao"],
    )
    assert pairs == [("韩立", "Han Li"), ("张浩", "Zhang Hao")]


def test_persist_does_not_overwrite_user_targets(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "core.translation.harvest.novel_glossary_path",
        lambda title: tmp_path / f"{title}.json",
    )
    persist_harvested_terms("Book", [("韩立", "Han Li")])
    persist_harvested_terms("Book", [("韩立", "Wrong"), ("王林", "Wang Lin")])
    data = (tmp_path / "Book.json").read_text(encoding="utf-8")
    assert "Han Li" in data
    assert "Wrong" not in data
    assert "Wang Lin" in data


class _FakeTranslator:
    def __init__(self, glossary):
        self.glossary = glossary
        self._cancel_requested = False

    def translate_texts(self, texts, progress_callback=None):
        table = {"韩立": "Han Li", "王林": "Wang Lin"}
        return [table.get(t, t) for t in texts]


def test_harvest_and_apply_merges_into_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "core.translation.harvest.novel_glossary_path",
        lambda title: tmp_path / f"{title}.json",
    )
    engine = GlossaryEngine(Glossary())
    t = _FakeTranslator(engine)
    n = harvest_and_apply(
        t,
        ["名叫韩立。韩立说道。王林问道。韩立笑道。"],
        novel_title="凡人修仙传",
    )
    assert n >= 1
    sources = {term.source for term in engine.glossary.terms}
    assert "韩立" in sources
    assert (tmp_path / "凡人修仙传.json").is_file()
    data = (tmp_path / "凡人修仙传.json").read_text(encoding="utf-8")
    assert "harvested" not in data
    assert "pinyin" in data


def test_strip_honorific_and_cluster_longer_name():
    assert strip_honorific("韩立师兄") == "韩立"
    text = (
        "韩立师兄说道：“走。”韩立笑道：“好。”韩立问道：“何处？”"
        "韩立来到了黄枫谷。黄枫谷外门弟子很多。"
        "他修习《长春功》。"
    )
    mined = mine_glossary_candidates([text])
    sources = {c.source for c in mined}
    assert "韩立" in sources
    assert "韩立师兄" not in sources
    assert "黄枫谷" in sources
    assert "长春功" in sources
    kinds = {c.source: c.kind for c in mined}
    assert kinds["黄枫谷"] == "place"
    assert kinds["长春功"] == "technique"


def test_pinyin_personal_name():
    assert format_personal_pinyin("韩立") == "Han Li"
    assert format_personal_pinyin("王林") == "Wang Lin"
    wan = format_personal_pinyin("林婉儿")
    assert wan.startswith("Lin")
    assert "Wan" in wan


def test_harvest_and_apply_uses_pinyin_not_google(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "core.translation.harvest.novel_glossary_path",
        lambda title: tmp_path / f"{title}.json",
    )
    engine = GlossaryEngine(Glossary())
    t = _FakeTranslator(engine)

    def boom(texts, progress_callback=None):
        raise AssertionError("harvest must not call Google for names")

    t.translate_texts = boom
    n = harvest_and_apply(
        t,
        ["名叫韩立。韩立说道。韩立笑道。"],
        novel_title="凡人修仙传",
    )
    assert n >= 1
    by = {term.source: term for term in engine.glossary.terms}
    assert by["韩立"].target == "Han Li"
    assert by["韩立"].notes == "pinyin"
