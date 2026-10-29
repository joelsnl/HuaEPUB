# Author: joelsnl and Anthropic Claude
"""The desktop look: a pocket-instrument style, the same two palettes as the browser pages.

``style.qss`` is a template (``${ground}``, ``${accent}``, …). ``apply_look``
renders it for the chosen palette and sets it on the application. ``auto``
follows the system colour scheme (the dark unit when dark, the light one when
light). Looks saved by older versions map to the nearest of the two.
"""

from __future__ import annotations

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
    seal_ink: str = "#121212"
    # The raised panel, the accent as text on a panel, the hard edge under a lit key, and the
    # always-dark LCD (progress strip, readouts).
    card: str = ""
    accent_text: str = ""
    edge: str = ""
    lcd: str = "#131312"
    lcd_ink: str = "#F1EFE8"

    def tokens(self) -> Dict[str, str]:
        t = {k: v for k, v in asdict(self).items() if isinstance(v, str)}
        # A panel (cards, tiles, buttons) and a field (inputs) sit either side of the ground.
        t["surface"] = self.card
        t["field"] = _mix(self.ground, self.card, 0.45)
        t["accent_ink"] = self.seal_ink
        t["accent_hover"] = (QColor(self.accent).lighter(108) if self.dark
                             else QColor(self.accent).lighter(106)).name()
        t["sans"] = SANS
        t["mono"] = MONO
        t["serif"] = serif_family()
        return t


PALETTES: Dict[str, Palette] = {p.id: p for p in (
    # The night unit: near-black housing, warm paper-white ink, one orange.
    Palette("dark", "Dark", True, "#131312", "#ECEAE3", "#8F8D86", "#3D3C38", "#2A2926", "#5B5953",
            "#FF6B2C", "#FF4A6A", "#FF6B2C", "#121212", "#1D1C1A", "#FF6B2C", "#B34A12", "#070707"),
    # The field unit: warm light-grey housing, off-white panels, the same orange.
    Palette("light", "Light", False, "#E4E2DC", "#121212", "#66645E", "#B9B6AD", "#D2CFC6", "#9C988D",
            "#FF5A1F", "#E2183F", "#FF5A1F", "#121212", "#F2F1ED", "#B93A08", "#C2400C", "#131312"),
)}

# Menu order and labels; the same ids as the browser's Look menu.
LOOKS = (
    ("auto", "Auto (follow system)"),
    ("light", "Light"),
    ("dark", "Dark"),
)
# Looks saved by older versions (six palettes, and "Surprise me") map to the nearest of the two.
_LEGACY = {"graphite": "dark", "indigo": "dark", "gold": "dark", "cinnabar": "dark",
           "celadon": "light", "mist": "light", "random": "auto"}

_current: Optional[Palette] = None
_fonts_loaded = False


def _mix(a: str, b: str, t: float) -> str:
    """``a`` blended towards ``b`` by ``t`` (0..1), as a hex colour."""
    ca, cb = QColor(a), QColor(b)
    return QColor(
        round(ca.red() + (cb.red() - ca.red()) * t),
        round(ca.green() + (cb.green() - ca.green()) * t),
        round(ca.blue() + (cb.blue() - ca.blue()) * t),
    ).name()


def normalize_look(look: str) -> str:
    look = (look or "auto").strip().lower()
    look = _LEGACY.get(look, look)
    return look if look in dict(LOOKS) else "auto"


def system_is_dark() -> bool:
    app = QGuiApplication.instance()
    if app is None:
        return True
    # Dark or Unknown: the app was dark before it had a light look
    return app.styleHints().colorScheme() != Qt.ColorScheme.Light


def resolve(look: str, *, system_dark: Optional[bool] = None) -> Palette:
    look = normalize_look(look)
    if look == "auto":
        dark = system_is_dark() if system_dark is None else system_dark
        return PALETTES["dark" if dark else "light"]
    return PALETTES[look]


def current() -> Palette:
    return _current or PALETTES["dark"]


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
