# Author: joelsnl and Anthropic Claude
"""The Slips look for the desktop app: the same palettes as the browser pages.

``style.qss`` is a template (``${ground}``, ``${accent}``, …). ``apply_look``
renders it for the chosen palette and sets it on the application. ``auto``
follows the system colour scheme (Graphite & Cyan when dark, Celadon Day when
light); ``random`` is opt-in only.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass
from pathlib import Path
from string import Template
from typing import Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication

FONT_DIR = Path(__file__).with_name("assets") / "fonts"
SANS = "Hanken Grotesk"
MONO = "JetBrains Mono"
# Not bundled (the CJK serif is ~10 MB per weight): use whatever the system has.
SERIF_CANDIDATES = ("Noto Serif SC", "Noto Serif CJK SC", "Source Han Serif SC", "Songti SC",
                    "SimSun", "Georgia", "Times New Roman", "serif")


@dataclass(frozen=True)
class Palette:
    id: str
    name: str
    dark: bool
    ground: str
    ink: str
    muted: str
    line: str
    rule: str
    mid: str
    accent: str
    flag: str
    seal: str
    seal_ink: str = "#FFF3EE"

    def tokens(self) -> Dict[str, str]:
        t = {k: v for k, v in asdict(self).items() if isinstance(v, str)}
        ground = QColor(self.ground)
        # A field / tile surface one small step off the ground.
        t["surface"] = (ground.lighter(118) if self.dark else ground.darker(104)).name()
        t["accent_ink"] = self.ground
        t["accent_hover"] = (QColor(self.accent).lighter(112) if self.dark
                             else QColor(self.accent).darker(115)).name()
        t["sans"] = SANS
        t["mono"] = MONO
        t["serif"] = serif_family()
        return t


PALETTES: Dict[str, Palette] = {p.id: p for p in (
    Palette("graphite", "Graphite & Cyan", True, "#0F1214", "#E8ECEE", "#8A959B", "#4A565C",
            "#262E33", "#2E3A41", "#5CC8E8", "#F2A65A", "#D2452F"),
    Palette("celadon", "Celadon Day", False, "#E4ECE7", "#14201B", "#4E6258", "#8FA79B",
            "#C3D2C9", "#A9C2B5", "#1E6B52", "#B4361F", "#B4361F"),
    Palette("indigo", "Indigo & Jade", True, "#12172A", "#E6EEE9", "#8C98B5", "#4A5680",
            "#2C3556", "#3A4676", "#8FD3B6", "#F0806B", "#D2452F"),
    Palette("gold", "Ink & Gold", True, "#181611", "#EDE7D8", "#A29A86", "#5A5442",
            "#33301F", "#4A4433", "#D9B45A", "#EF8A6A", "#C2402B"),
    Palette("cinnabar", "Cinnabar Night", True, "#1A1213", "#F1E6E3", "#B09890", "#6B4B4D",
            "#3A2628", "#5A3436", "#F08A6B", "#F2C46D", "#B3362A"),
    Palette("mist", "Blue Mist", False, "#E8ECF2", "#131A2B", "#505B75", "#97A3BE",
            "#C8D0E0", "#B4BFD8", "#2F4BA6", "#B4361F", "#B4361F"),
)}

# Menu order and labels; the same ids as the browser's Look menu.
LOOKS = (
    ("auto", "Auto (follow system)"),
    ("dark", "Dark"),
    ("light", "Light"),
    ("indigo", "Indigo & Jade"),
    ("gold", "Ink & Gold"),
    ("cinnabar", "Cinnabar Night"),
    ("mist", "Blue Mist"),
    ("random", "Surprise me"),
)
# The other looks share their palette's id.
_LOOK_TO_PALETTE = {"dark": "graphite", "light": "celadon"}

_current: Optional[Palette] = None
_fonts_loaded = False


def normalize_look(look: str) -> str:
    look = (look or "auto").strip().lower()
    return look if look in dict(LOOKS) else "auto"


def system_is_dark() -> bool:
    app = QGuiApplication.instance()
    if app is None:
        return True
    # Dark or Unknown: the app was dark before the Slips look
    return app.styleHints().colorScheme() != Qt.ColorScheme.Light


def resolve(look: str, *, system_dark: Optional[bool] = None) -> Palette:
    look = normalize_look(look)
    if look == "auto":
        dark = system_is_dark() if system_dark is None else system_dark
        return PALETTES["graphite" if dark else "celadon"]
    if look == "random":
        return PALETTES[random.choice(list(PALETTES))]
    return PALETTES[_LOOK_TO_PALETTE.get(look, look)]


def current() -> Palette:
    return _current or PALETTES["graphite"]


def render_qss(palette: Palette) -> str:
    template_text = Path(__file__).with_name("style.qss").read_text(encoding="utf-8")
    return Template(template_text).safe_substitute(palette.tokens())


def load_fonts() -> None:
    """Register the bundled Hanken Grotesk / JetBrains Mono (OFL). Missing files are fine."""
    global _fonts_loaded
    if _fonts_loaded:
        return
    _fonts_loaded = True
    if not FONT_DIR.is_dir():
        return
    for path in sorted(FONT_DIR.glob("*.ttf")):
        QFontDatabase.addApplicationFont(str(path))


def serif_family() -> str:
    families = set(QFontDatabase.families())
    for name in SERIF_CANDIDATES:
        if name in families:
            return name
    return "serif"


def serif_font(point_size: float, weight: QFont.Weight = QFont.Weight.Medium) -> QFont:
    font = QFont(serif_family())
    font.setPointSizeF(point_size)
    font.setWeight(weight)
    font.setStyleHint(QFont.StyleHint.Serif)
    return font


def apply_look(app, look: str) -> Palette:
    """Render and set the stylesheet for ``look``; returns the palette in use."""
    global _current
    load_fonts()
    palette = resolve(look)
    _current = palette
    app.setFont(_app_font(app))
    app.setStyleSheet(render_qss(palette))
    return palette


def _app_font(app) -> QFont:
    font = QFont(app.font())
    if SANS in QFontDatabase.families():
        font.setFamily(SANS)
    return font
