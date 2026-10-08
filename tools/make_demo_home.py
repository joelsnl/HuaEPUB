"""Build a demo ~/.huaepub with made-up books, for README screenshots.

    python tools/make_demo_home.py <folder>

Creates <folder>/.huaepub with a library of invented novels (painted covers, a
table of contents and readable chapters for the first book, reading positions).
Point HOME/USERPROFILE (or Path.home) at <folder> to run the app on it. Nothing
here is a real novel, site or author.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BOOKS = [
    ("灯下藏经阁", "The Library Beneath the Lantern", "Qing Mu", 412, 412, 3),
    ("雨夜听剑", "Listening to Swords in the Rain", "Lan Xi", 238, 120, 0),
    ("青石巷的修书人", "The Bookbinder of Bluestone Lane", "Shen Yao", 96, 96, 12),
    ("星河渡口", "Ferry at the Edge of the Galaxy", "Bai Qiu", 1031, 640, 0),
    ("一纸山河", "A Kingdom on One Sheet of Paper", "Gu Ning", 187, 0, 0),
    ("茶馆里的仙人", "The Immortal Who Ran a Teahouse", "Lu Ran", 655, 655, 27),
    ("北境守灯人", "Lamplighter of the Northern Border", "He Chen", 302, 41, 0),
    ("纸鹤与铁城", "Paper Cranes and an Iron City", "Wen Shu", 128, 0, 0),
    ("落月书院", "Academy of the Setting Moon", "Xu Lan", 774, 302, 0),
]
COVER_COLOURS = [
    ("#2B4C7E", "#E9D8A6"), ("#7A2E2E", "#F2E3D5"), ("#2F5D50", "#E8E4D8"),
    ("#1F2A44", "#C9D6F0"), ("#5B4B8A", "#EDE7F6"), ("#8A5A2B", "#F6EBDD"),
    ("#264653", "#E9C46A"), ("#3D405B", "#F4F1DE"), ("#6B2737", "#F7E1D7"),
]
HOST = "https://demo.example/book/{id}.html"

CHAPTERS = [
    ("Chapter 1 The Lantern That Would Not Go Out",
     ["The archive under the old city had been sealed for three hundred years, and still the "
      "lantern above its door burned with a steady, patient light.",
      "Qin Yu had walked past it every evening on his way home from the copy shop. Tonight he "
      "stopped, because tonight the lantern turned to look at him.",
      "“You read too slowly,” it said. “But you never skip a page. Come in.”"]),
    ("Chapter 2 Ten Thousand Shelves",
     ["Inside, the shelves went down farther than the stairs did. Each book was bound in a "
      "different leather, and each one was humming quietly, like a kettle about to boil.",
      "“They are waiting to be read,” the lantern explained. “A book that is never "
      "read forgets what it was about.”",
      "Qin Yu pulled the nearest volume from its place. The humming stopped, and the room "
      "seemed to lean closer."]),
    ("Chapter 3 The First Borrower",
     ["The ledger by the door listed every borrower since the archive opened. The last name "
      "had been written in a careful hand three centuries ago.",
      "Under it, the ink was still wet: Qin Yu, borrower number two."]),
]


def paint_cover(path: Path, zh: str, en: str, colours: tuple) -> None:
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen

    w, h = 300, 420
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(colours[0]))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    ink = QColor(colours[1])
    p.setPen(QPen(ink, 3))
    p.drawRect(QRectF(18, 18, w - 36, h - 36))
    zh_font = QFont("Microsoft YaHei", 30)
    zh_font.setBold(True)
    p.setFont(zh_font)
    p.drawText(QRectF(30, 60, w - 60, 200), Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap, zh)
    p.drawLine(110, 280, w - 110, 280)
    p.setFont(QFont("Georgia", 15))
    p.drawText(QRectF(34, 296, w - 68, 100), Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap, en)
    p.end()
    img.save(str(path), "PNG")


def build(root: Path) -> Path:
    from PySide6.QtGui import QGuiApplication

    _app = QGuiApplication.instance() or QGuiApplication(sys.argv)  # noqa: F841 - fonts need it
    data = root / ".huaepub"
    data.mkdir(parents=True, exist_ok=True)
    (data / "settings.json").write_text(json.dumps({
        "translate": True, "clean": True, "translation_backend": "google",
        "translation_glossary": "off", "auto_check_updates": False, "ui_look": "auto",
        "server_mode": "lan",
    }, indent=2), encoding="utf-8")

    import core.settings as settings_mod

    settings_mod.Path.home = classmethod(lambda cls: root)  # type: ignore[assignment]
    from core.cache import NovelCache
    from core.library import LibraryStore
    from core.reading import set_position

    cache = NovelCache(data / "cache.db")
    store = LibraryStore(data / "library.json")
    covers = data / "_covers"
    covers.mkdir(exist_ok=True)
    for i, (zh, en, author, chapters, read, _new) in enumerate(BOOKS):
        url = HOST.format(id=1001 + i)
        cover_url = f"https://demo.example/cover/{1001 + i}.png"
        png = covers / f"{1001 + i}.png"
        paint_cover(png, zh, en, COVER_COLOURS[i % len(COVER_COLOURS)])
        cache.put_cover(png.read_bytes(), cover_url=cover_url, source_url=url, content_type="image/png")
        store.upsert_library(
            url, title=zh, translated_title=en, author=author, cover_url=cover_url,
            chapter_count=chapters, last_chapter_title=f"Chapter {chapters}",
            description=f"An invented demo book for screenshots. {en} has {chapters} chapters.",
        )
        if read:
            set_position(url, chapter_url=f"{url}#c{read}", chapter_index=read - 1, data_dir=data)

    # The first book is readable: a table of contents and three cached chapters.
    first = HOST.format(id=1001)
    toc = [{"url": f"https://demo.example/read/1001/{n}.html", "title": t} for n, (t, _) in
           enumerate(CHAPTERS, start=1)]
    cache.put_chapter_list(first, toc)
    for row, (title, paras) in zip(toc, CHAPTERS):
        body = "".join(f"<p>{p}</p>" for p in paras * 6)
        cache.put_chapter("demo-1001", row["url"], title, f"<div id=\"content\">{body}</div>")
    set_position(first, chapter_url=toc[1]["url"], chapter_index=1, data_dir=data)
    cache.close()
    for png in covers.iterdir():
        png.unlink()
    covers.rmdir()
    return data


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    print(build(Path(sys.argv[1]).resolve()))
