# Author: joelsnl and Anthropic Claude
"""Reader notes: the store (core/notes.py) and the browser reader's /notes routes."""

from __future__ import annotations

from core import notes
from core.parser import Chapter
from tests.test_server_mode import H, _ctx, _signed_in, home, session  # noqa: F401 - fixtures

URL = "https://example.com/book/notes"


def test_add_list_label_delete_round_trip(tmp_path):
    first = notes.add_note(URL, "Qi Refining -> Foundation", chapter_index=2, scroll=0.5,
                           data_dir=tmp_path)
    second = notes.add_note(URL, "Elder Wu", label="People", data_dir=tmp_path)
    assert first and second
    assert [n["text"] for n in notes.list_notes(URL, data_dir=tmp_path)][0] == "Elder Wu"
    assert notes.set_label(URL, first["id"], "  Realms ", data_dir=tmp_path)
    labels = {n["id"]: n["label"] for n in notes.list_notes(URL, data_dir=tmp_path)}
    assert labels[first["id"]] == "Realms"
    assert notes.delete_note(URL, first["id"], data_dir=tmp_path)
    assert [n["id"] for n in notes.list_notes(URL, data_dir=tmp_path)] == [second["id"]]


def test_empty_text_and_unknown_ids_change_nothing(tmp_path):
    assert notes.add_note(URL, "   ", data_dir=tmp_path) is None
    assert notes.add_note("", "text", data_dir=tmp_path) is None
    assert notes.delete_note(URL, "nope", data_dir=tmp_path) is False
    assert notes.list_notes(URL, data_dir=tmp_path) == []


def test_notes_are_kept_per_book_and_survive_a_bad_file(tmp_path):
    notes.add_note(URL, "mine", data_dir=tmp_path)
    assert notes.list_notes(URL + "/other", data_dir=tmp_path) == []
    (tmp_path / notes.NOTES_FILE).write_text("not json", encoding="utf-8")
    assert notes.list_notes(URL, data_dir=tmp_path) == []


def test_note_cap_per_book(tmp_path, monkeypatch):
    monkeypatch.setattr(notes, "MAX_NOTES_PER_BOOK", 2)
    assert notes.add_note(URL, "a", data_dir=tmp_path)
    assert notes.add_note(URL, "b", data_dir=tmp_path)
    assert notes.add_note(URL, "c", data_dir=tmp_path) is None


def test_reader_routes_save_label_and_delete(session):  # noqa: F811 - the imported pytest fixture
    session.cache.put_chapter_list(URL, [Chapter(title="Chapter 1", url=f"{URL}/1"),
                                         Chapter(title="Chapter 2", url=f"{URL}/2")])
    session.cache.put_chapter(URL, f"{URL}/1", "Chapter 1", "<p>First.</p>")
    c = _signed_in(_ctx(session))
    opened = c.post("/api/read/open", json={"url": URL}, headers=H).json()
    assert opened["notes"] == []
    bid = opened["book_id"]
    saved = c.post(f"/api/read/{bid}/notes", json={"text": "Golden Core realm", "index": 1,
                                                    "scroll": 0.25}, headers=H)
    assert saved.status_code == 200, saved.text
    note = saved.json()["notes"][0]
    assert note["text"] == "Golden Core realm" and note["chapter_index"] == 1
    labelled = c.post(f"/api/read/{bid}/notes/{note['id']}/label", json={"label": "Realms"},
                      headers=H)
    assert labelled.json()["notes"][0]["label"] == "Realms"
    reopened = c.post("/api/read/open", json={"url": URL}, headers=H).json()
    assert reopened["notes"][0]["label"] == "Realms"
    gone = c.delete(f"/api/read/{bid}/notes/{note['id']}", headers=H)
    assert gone.json()["notes"] == []


def test_reader_routes_reject_bad_input(session):  # noqa: F811 - the imported pytest fixture
    session.cache.put_chapter_list(URL, [Chapter(title="Chapter 1", url=f"{URL}/1")])
    c = _signed_in(_ctx(session))
    bid = c.post("/api/read/open", json={"url": URL}, headers=H).json()["book_id"]
    assert c.post(f"/api/read/{bid}/notes", json={"text": "x", "index": 9}, headers=H).status_code == 400
    assert c.post(f"/api/read/{bid}/notes", json={"text": "  ", "index": 0}, headers=H).status_code == 400
    assert c.post("/api/read/nope/notes", json={"text": "x", "index": 0}, headers=H).status_code == 404
