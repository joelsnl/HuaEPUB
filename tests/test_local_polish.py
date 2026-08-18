"""Offline tests for KEEP/REPLACE polish wiring."""

from core.local_polish import wants_polish
from core.translator import should_polish_english


def test_wants_polish_skips_fluent_and_titles():
    assert wants_polish("He walked into the room and sat down by the window.") is False
    assert wants_polish("Chapter 12") is False
    assert wants_polish("Li Ming") is False
    assert wants_polish("这是中文段落内容测试") is False


def test_wants_polish_catches_grammar_and_mtl_calques():
    assert should_polish_english("She go to school every morning.") is True
    assert wants_polish("She go to school every morning.") is True
    assert wants_polish(
        "The corners of her mouth raised slightly, revealing a playful smile."
    ) is True
    assert wants_polish("Jiang Kai's eyes narrowed.") is True


def test_polish_stack_lives_in_huaepub():
    import core.polish.api as api
    import core.polish.paths as paths

    path = api.__file__.replace("\\", "/")
    assert "/core/polish/" in path
    assert "copydecode" not in path
    assert not hasattr(paths, "extra_model_dirs")
    assert api.wants_polish("Jiang Kai's eyes narrowed.") is True
