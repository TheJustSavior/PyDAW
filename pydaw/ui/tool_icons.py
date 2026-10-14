"""Small theme-aware icons for editing tools."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import (QBrush, QColor, QIcon, QPainter, QPainterPath,
                           QPen, QPixmap)


def mouse_icon(color: QColor | str, size: int = 20) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing, True)
    c = QColor(color)
    scale = size / 20.0
    cursor = QPainterPath()
    cursor.moveTo(5.0 * scale, 2.0 * scale)
    cursor.lineTo(5.0 * scale, 15.4 * scale)
    cursor.lineTo(8.7 * scale, 12.0 * scale)
    cursor.lineTo(10.9 * scale, 17.0 * scale)
    cursor.lineTo(13.1 * scale, 16.0 * scale)
    cursor.lineTo(10.8 * scale, 11.0 * scale)
    cursor.lineTo(15.1 * scale, 11.0 * scale)
    cursor.closeSubpath()
    p.setPen(QPen(c, max(1.0, size * 0.055), Qt.SolidLine,
                  Qt.SquareCap, Qt.MiterJoin))
    p.setBrush(QBrush(c))
    p.drawPath(cursor)
    p.end()
    return QIcon(pix)


def select_icon(color: QColor | str, size: int = 20) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing, True)
    c = QColor(color)
    pen = QPen(c, max(1.0, size * 0.045), Qt.DashLine)
    pen.setDashPattern([1.7, 1.35])
    pen.setCapStyle(Qt.FlatCap)
    p.setPen(pen)
    p.setBrush(QBrush(Qt.NoBrush))
    side = size * 0.58
    offset = (size - side) / 2.0
    p.drawRect(QRectF(offset, offset, side, side))
    p.end()
    return QIcon(pix)


def icon_size(size: int = 20) -> QSize:
    return QSize(size, size)
