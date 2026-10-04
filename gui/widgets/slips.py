# Author: joelsnl and Anthropic Claude
"""Bamboo-slip progress strip and the 译 seal: the same marks as the browser pages."""

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
    """A row of slips tied by two cords. Width decides how many slips show."""

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
        # Spread the slips over the full width so the cords never run past them.
        step = max(1.0, (self.width() - 2) / max(1, n))
        slip_w = max(2.0, min(step * 0.62, 14.0))
        x0 = 1.0
        h = self.height()
        slip_h = h - 10
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        line, mid, accent, ink = (QColor(pal.line), QColor(pal.mid), QColor(pal.accent),
                                  QColor(pal.ink))
        cur = -1
        if not self._finished:
            for i, st in enumerate(states):
                if (self._phase in ("", "fetching") and st == WAITING) or \
                        (self._phase not in ("", "fetching") and st == FETCHED):
                    cur = i if self._fraction > 0 else -1
                    break
        for i, st in enumerate(states):
            rect = QRectF(x0 + i * step + 0.5, 1.5, slip_w - 1, slip_h)
            if st == DONE:
                painter.setPen(QPen(accent, 1))
                painter.setBrush(accent)
            elif st == FETCHED:
                painter.setPen(QPen(mid, 1))
                painter.setBrush(mid)
            else:
                painter.setPen(QPen(line, 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
            if i == cur:
                painter.setPen(QPen(ink, 1))
            painter.drawRect(rect)
        # The two cords that bind the slips.
        painter.setPen(QPen(line, 1))
        for y in (int(slip_h * 0.28) + 1, int(slip_h * 0.74) + 1):
            painter.drawLine(0, y, self.width(), y)
        if self._flagged and self._finished:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(pal.flag))
            painter.drawEllipse(QRectF(x0, h - 6, 5, 5))
        painter.end()


class SealMark(QWidget):
    """The red 译 stamp shown on a finished translated build."""

    def __init__(self, parent=None, *, size: int = 40):
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size + 4, size + 4)
        self.setToolTip("Translated")

    def paintEvent(self, _event):
        pal = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.translate(self.width() / 2, self.height() / 2)
        painter.rotate(-4)
        s = self._size
        rect = QRectF(-s / 2, -s / 2, s, s)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(pal.seal))
        painter.drawRoundedRect(rect, 3, 3)
        painter.setPen(QColor(pal.seal_ink))
        painter.setFont(theme.serif_font(s * 0.42))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "译")
        painter.end()
