# Author: joelsnl and Anthropic Claude
"""Draw a QR code into a QPixmap (qrcode gives the module matrix; Qt paints it)."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPixmap


def qr_matrix(text: str):
    """Module rows (True = dark), quiet zone included, or None without the qrcode package."""
    try:
        import qrcode
    except ImportError:
        return None
    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    return qr.get_matrix()


def qr_pixmap(text: str, size: int, *, dark: str = "#000000", light: str = "#FFFFFF",
              ratio: float = 1.0) -> QPixmap:
    """A square pixmap of ``size`` logical pixels; null when qrcode is missing."""
    matrix = qr_matrix(text) if text else None
    if not matrix:
        return QPixmap()
    px = max(1, int(round(size * ratio)))
    pix = QPixmap(px, px)
    pix.fill(QColor(light))
    n = len(matrix)
    cell = px / n
    painter = QPainter(pix)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(dark))
    for y, row in enumerate(matrix):
        for x, on in enumerate(row):
            if on:
                # Overlap by a hair so no seams show between modules.
                painter.drawRect(QRectF(x * cell, y * cell, cell + 0.5, cell + 0.5))
    painter.end()
    pix.setDevicePixelRatio(ratio)
    return pix
