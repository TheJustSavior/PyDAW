"""Waveform rendering: peaks come from the C++ engine (min/max blocks),
drawn into a QPixmap that clip items reuse while zoom is unchanged.

Look: crisp per-pixel min/max bars filled with a vertical gradient that
is brightest at the center line, a denser pseudo-RMS core, and 1-px
peak caps. All colors derive from the owning track's accent color, so
the wave matches every theme and track palette.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen, QPixmap


def _column_minmax(peaks: list[float], width: int) -> tuple[list, list]:
    """Per-pixel-column (min, max) from interleaved (min, max) blocks."""
    n_blocks = len(peaks) // 2
    mins = [0.0] * width
    maxs = [0.0] * width
    for x in range(width):
        b0 = int(x * n_blocks / width)
        b1 = max(b0 + 1, int((x + 1) * n_blocks / width))
        mn, mx = 1.0, -1.0
        for b in range(b0, min(b1, n_blocks)):
            mn = min(mn, peaks[b * 2])
            mx = max(mx, peaks[b * 2 + 1])
        if mn > mx:
            mn = mx = 0.0
        mins[x] = mn
        maxs[x] = mx
    return mins, maxs


def render_waveform(peaks: list[float], width: int, height: int,
                    color: QColor) -> QPixmap:
    """Render (min, max) peak pairs into a themed waveform pixmap."""
    pixmap = QPixmap(max(1, width), max(1, height))
    pixmap.fill(Qt.transparent)
    if not peaks or width <= 0 or height <= 0:
        return pixmap

    mins, maxs = _column_minmax(peaks, width)
    mid = height / 2.0
    half = height / 2.0 - 1.0

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, False)

    # Layer 1: full min/max bars with a vertical gradient — brightest at
    # the center line, fading toward the peaks. Reads as depth without
    # blurring the shape.
    peak_c = QColor(color)
    peak_c.setAlpha(110)
    center_c = QColor(color).lighter(135)
    center_c.setAlpha(235)
    grad = QLinearGradient(0, 0, 0, height)
    grad.setColorAt(0.0, peak_c)
    grad.setColorAt(0.5, center_c)
    grad.setColorAt(1.0, peak_c)
    painter.setPen(Qt.NoPen)
    painter.setBrush(grad)
    for x in range(width):
        y0 = mid - maxs[x] * half
        y1 = mid - mins[x] * half
        painter.drawRect(x, int(y0), 1, max(1, int(y1 - y0 + 0.5)))

    # Layer 2: pseudo-RMS core at half amplitude — a hotter inner band
    # (classic two-tone DAW waveform).
    core_c = QColor(color).lighter(170)
    core_c.setAlpha(170)
    painter.setBrush(core_c)
    for x in range(width):
        y0 = mid - maxs[x] * half * 0.5
        y1 = mid - mins[x] * half * 0.5
        h = y1 - y0
        if h >= 1.0:
            painter.drawRect(x, int(y0), 1, max(1, int(h + 0.5)))

    # Layer 3: 1-px caps on the peak tips keep transients crisp.
    cap_c = QColor(color).lighter(150)
    cap_c.setAlpha(220)
    painter.setPen(QPen(cap_c, 1))
    for x in range(width):
        amp = maxs[x] - mins[x]
        if amp > 0.02:
            painter.drawPoint(x, int(mid - maxs[x] * half))
            painter.drawPoint(x, int(mid - mins[x] * half))

    # Center line: ties silent stretches together.
    line_c = QColor(color).lighter(140)
    line_c.setAlpha(60)
    painter.setPen(QPen(line_c, 1))
    painter.drawLine(0, int(mid), width, int(mid))

    painter.end()
    return pixmap
