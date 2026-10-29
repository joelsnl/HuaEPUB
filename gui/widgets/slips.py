# Author: joelsnl and Anthropic Claude
"""The step-sequencer progress strip and the 译 key: the same marks as the browser pages."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from gui import theme

WAITING, FETCHED, DONE = 0, 1, 2


def slip_states(n: int, fraction: float, phase: str, *, finished: bool = False) -> list[int]:
    """Per-slip state for an overall ``fraction``.

    While fetching, the filled part reads as fetched (mid). After fetching,
    everything is fetched and the filled part reads as done (accent).
    """
    n = max(0, int(n))
    if finished:
        return [DONE] * n
    frac = max(0.0, min(1.0, float(fraction or 0.0)))
    filled = min(n, int(frac * n))
    if phase in ("", "fetching"):
        return [FETCHED] * filled + [WAITING] * (n - filled)
    return [DONE] * filled + [FETCHED] * (n - filled)


class SlipStrip(QWidget):
    """A row of steps on an LCD. Width decides how many steps show."""

    SLIP_W = 6
    GAP = 3

    def __init__(self, parent=None, *, height: int = 52):
        super().__init__(parent)
        self.setObjectName("slipStrip")
        self.setMinimumHeight(height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._height = height
        self._fraction = 0.0
        self._phase = ""
        self._finished = False
        self._flagged = False

    def sizeHint(self) -> QSize:
        return QSize(400, self._height)

    def slip_count(self) -> int:
        usable = max(0, self.width() - 2)
        return max(8, min(64, usable // (self.SLIP_W + self.GAP)))

    def set_progress(self, fraction: float, phase: str = "") -> None:
        self._fraction = float(fraction or 0.0)
        self._phase = phase or ""
        self._finished = False
        self.update()

    def set_finished(self, flagged: bool = False) -> None:
        self._finished = True
        self._flagged = bool(flagged)
        self.update()

    def paintEvent(self, _event):
        pal = theme.current()
        n = self.slip_count()
        states = slip_states(n, self._fraction, self._phase, finished=self._finished)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # The LCD tray the steps sit in.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(pal.lcd))
        painter.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 9, 9)
        pad = 9.0
        step = max(1.0, (self.width() - 2 * pad) / max(1, n))
        slip_w = max(2.0, min(step * 0.62, 14.0))
        top = pad
        slip_h = self.height() - 2 * pad - (6 if self._flagged and self._finished else 0)
        ink = QColor(pal.lcd_ink)
        dim = QColor(ink)
        dim.setAlphaF(0.12)
        fetched = QColor(ink)
        fetched.setAlphaF(0.42)
        accent = QColor(pal.accent)
        cur = -1
        if not self._finished:
            for i, st in enumerate(states):
                if (self._phase in ("", "fetching") and st == WAITING) or \
                        (self._phase not in ("", "fetching") and st == FETCHED):
                    cur = i if self._fraction > 0 else -1
                    break
        for i, st in enumerate(states):
            rect = QRectF(pad + i * step + (step - slip_w) / 2, top, slip_w, slip_h)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(accent if st == DONE else fetched if st == FETCHED else dim)
            if i == cur:
                # The step the sequencer is on: ringed, like a lit key.
                painter.setPen(QPen(ink, 1.5))
            painter.drawRoundedRect(rect, 2, 2)
        if self._flagged and self._finished:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(pal.flag))
            painter.drawEllipse(QRectF(pad, self.height() - pad + 1, 5, 5))
        painter.end()


class SealMark(QWidget):
    """The lit 译 key shown on a finished translated build."""

    def __init__(self, parent=None, *, size: int = 40):
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size + 6, size + 8)
        self.setToolTip("Translated")

    def paintEvent(self, _event):
        pal = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.translate(self.width() / 2, self.height() / 2 - 2)
        painter.rotate(-4)
        s = self._size
        rect = QRectF(-s / 2, -s / 2, s, s)
        radius = s * 0.24
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(pal.edge))  # the hard edge under the key
        painter.drawRoundedRect(rect.translated(0, 3), radius, radius)
        painter.setBrush(QColor(pal.seal))
        painter.drawRoundedRect(rect, radius, radius)
        painter.setPen(QColor(pal.seal_ink))
        painter.setFont(theme.serif_font(s * 0.42))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "译")
        painter.end()
