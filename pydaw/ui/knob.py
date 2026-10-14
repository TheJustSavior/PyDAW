"""Small rotary knob for the continuous stereo width (0..200%)
plus a QSlider subclass with a right-click "reset to default" menu."""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen, QWheelEvent
from PySide6.QtWidgets import QMenu, QSlider, QToolTip, QWidget

from . import theme

# Indicator sweep: width 0% at -135°, 200% at +135° (degrees from
# 12 o'clock, clockwise positive).
ANGLE_MIN = -135.0
ANGLE_MAX = 135.0
DRAG_RANGE_PX = 120.0   # full 0..200% over this vertical drag distance
WHEEL_STEP = 0.05       # 5% per wheel notch
DEFAULT_WIDTH = 1.0

# Global "control released" hook: MainWindow finalizes the pending
# coalesced undo gesture here, so every distinct drag becomes its own
# undo step (the 500 ms silence timer alone can merge quick successive
# drags into one step, losing intermediate values).
_gesture_end_hook = None


def set_gesture_end_hook(hook) -> None:
    global _gesture_end_hook
    _gesture_end_hook = hook


def _notify_gesture_end() -> None:
    if _gesture_end_hook is not None:
        _gesture_end_hook()


class StereoWidthKnob(QWidget):
    """Continuous stereo-width knob: drag/scroll to turn,
    double click or right-click menu resets to 100%."""

    width_changed = Signal(float)

    def __init__(self, width: float = DEFAULT_WIDTH, diameter: int = 20,
                 parent=None) -> None:
        super().__init__(parent)
        self._width = self._clamp(width)
        self._diameter = diameter
        self._press_y: float | None = None
        self._press_width = self._width
        self.setFixedSize(diameter, diameter)
        self.setCursor(Qt.PointingHandCursor)
        self._update_tip()

    # -- state -------------------------------------------------------------

    @staticmethod
    def _clamp(width: float) -> float:
        return max(0.0, min(2.0, float(width)))

    def width_value(self) -> float:
        return self._width

    def set_width(self, width: float) -> None:
        width = self._clamp(width)
        if abs(width - self._width) > 1e-6:
            self._width = width
            self._update_tip()
            self.update()

    def _apply(self, width: float, *, notify: bool) -> None:
        width = self._clamp(width)
        if abs(width - self._width) < 1e-6:
            return
        self._width = width
        self._update_tip()
        self.update()
        QToolTip.showText(self.mapToGlobal(self.rect().bottomLeft()),
                          self._tip_text(), self)
        if notify:
            self.width_changed.emit(width)

    def _tip_text(self) -> str:
        pct = round(self._width * 100)
        if pct == 0:
            extra = " (моно)"
        elif pct == 100:
            extra = " (стерео)"
        else:
            extra = ""
        return f"Stereo width: {pct}%{extra}"

    def _update_tip(self) -> None:
        self.setToolTip(self._tip_text()
                        + "\nDrag/колесо — крутить, двойной клик — 100%")

    # -- painting ----------------------------------------------------------

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = min(self.width(), self.height()) / 2.0 - 1.0
        center = QPointF(self.width() / 2.0, self.height() / 2.0)

        p.setPen(QPen(QColor(theme.BORDER_SOFT), 1))
        p.setBrush(QColor(theme.RAISED))
        p.drawEllipse(center, r, r)

        # Reference ticks: mono (left), 100% (top), 200% (right).
        p.setPen(QPen(QColor(theme.TEXT_FAINT), 1))
        for frac in (0.0, 0.5, 1.0):
            a = math.radians(ANGLE_MIN + (ANGLE_MAX - ANGLE_MIN) * frac)
            outer = QPointF(center.x() + math.sin(a) * (r + 0.5),
                            center.y() - math.cos(a) * (r + 0.5))
            inner = QPointF(center.x() + math.sin(a) * (r - 2.0),
                            center.y() - math.cos(a) * (r - 2.0))
            p.drawLine(outer, inner)

        # Indicator.
        a = math.radians(ANGLE_MIN
                         + (ANGLE_MAX - ANGLE_MIN) * (self._width / 2.0))
        tip = QPointF(center.x() + math.sin(a) * (r - 2.5),
                      center.y() - math.cos(a) * (r - 2.5))
        p.setPen(QPen(QColor(theme.ACCENT), 2))
        p.drawLine(center, tip)
        p.end()

    # -- interaction ---------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._press_y = event.position().y()
            self._press_width = self._width
            event.accept()
            return
        if event.button() == Qt.RightButton:
            self._show_context_menu(event.globalPosition().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._press_y is not None and (event.buttons() & Qt.LeftButton):
            delta = self._press_y - event.position().y()
            self._apply(self._press_width + delta / DRAG_RANGE_PX * 2.0,
                        notify=True)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton and self._press_y is not None:
            self._press_y = None
            _notify_gesture_end()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._apply(DEFAULT_WIDTH, notify=True)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if delta:
            step = WHEEL_STEP if delta > 0 else -WHEEL_STEP
            self._apply(self._width + step, notify=True)
        event.accept()

    def _show_context_menu(self, global_pos) -> None:
        menu = QMenu(self)
        pct = round(DEFAULT_WIDTH * 100)
        menu.addAction(f"Reset to default ({pct}%)",
                       lambda: self._apply(DEFAULT_WIDTH, notify=True))
        menu.exec(global_pos)


class ResetSlider(QSlider):
    """QSlider with a right-click "Reset to default" menu."""

    def __init__(self, orientation, default_value: int = 0,
                 default_label: str = "", parent=None) -> None:
        super().__init__(orientation, parent)
        self._default_value = default_value
        self._default_label = default_label
        # End of a drag closes the pending undo gesture immediately.
        self.sliderReleased.connect(_notify_gesture_end)

    def set_default_value(self, value: int) -> None:
        self._default_value = value

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        label = "Reset to default"
        if self._default_label:
            label += f" ({self._default_label})"
        menu.addAction(label, lambda: self.setValue(self._default_value))
        menu.exec(event.globalPos())
        event.accept()
