"""Shared VST parameter slider widgets."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QScrollArea, QSlider,
                               QSizePolicy, QVBoxLayout, QWidget)

from ..engine_bridge import EngineBridge
from . import theme
from .knob import ResetSlider


class VstParamPanel(QWidget):
    """Up to `max_params` sliders for a VST slot (0..1 normalized).

    The slider set can be rebuilt at runtime (`rebuild_params`): plugins
    may change their parameter list when the user switches presets or
    the plugin GUI restructures itself (host is notified through
    restartComponent).
    """

    param_changed = Signal(int, float)  # param_index, normalized

    def __init__(self, bridge: EngineBridge, track_id: int, slot: int,
                 max_params: int = 512, parent=None) -> None:
        super().__init__(parent)
        self._bridge = bridge
        self._track_id = track_id
        self._slot = slot
        self._max_params = max_params
        self._sliders: list[QSlider] = []
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Every widget gets a parent at construction: a parentless widget
        # polished before insertion can flash as a tiny top-level window on
        # Windows (rebuild_params runs while the panel is visible).
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setStyleSheet(theme.thin_scroll_area_qss())

        self._content = QWidget(self._scroll)
        self._content.setStyleSheet("background: transparent;")
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(0, 4, 0, 0)
        self._layout.setSpacing(4)
        self._scroll.setWidget(self._content)
        outer.addWidget(self._scroll)

        self._build()

    def _clear(self) -> None:
        self._sliders.clear()
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
            elif item.layout() is not None:
                sub = item.layout()
                while sub.count():
                    child = sub.takeAt(0)
                    if child.widget() is not None:
                        child.widget().deleteLater()

    def _build(self) -> None:
        bridge, track_id, slot = self._bridge, self._track_id, self._slot
        params = bridge.vst_params(track_id, slot)[:self._max_params]
        if not params:
            empty = QLabel("No exposed parameters", self._content)
            empty.setStyleSheet(
                f"color: {theme.TEXT_FAINT}; font-size: 10px;"
                " background: transparent; border: none;")
            self._layout.addWidget(empty)
            return
        for index, (_param_id, title, normalized) in enumerate(params):
            row = QHBoxLayout()
            lab = QLabel(title or f"Param {index}", self._content)
            lab.setStyleSheet(
                f"color: {theme.TEXT_DIM}; font-size: 10px;"
                " background: transparent; border: none;")
            lab.setFixedWidth(100)
            row.addWidget(lab)
            default = bridge.vst_get_param_default(track_id, slot, index)
            slider = ResetSlider(Qt.Horizontal,
                                 int(round(default * 1000)),
                                 f"{round(default * 100)}%",
                                 parent=self._content)
            slider.setRange(0, 1000)
            slider.setValue(int(normalized * 1000))
            slider.setToolTip(f"{title or f'Param {index}'} (ПКМ — reset)")
            slider.valueChanged.connect(
                lambda v, idx=index:
                self.param_changed.emit(idx, v / 1000.0))
            row.addWidget(slider, 1)
            self._layout.addLayout(row)
            self._sliders.append(slider)

    def rebuild_params(self) -> None:
        """Re-read the plugin's parameter list and recreate the sliders
        (preset switch / dynamic plugin GUI)."""
        self._clear()
        self._build()

    def apply_param(self, param_index: int, normalized: float) -> None:
        self._bridge.vst_set_param(
            self._track_id, self._slot, param_index, normalized)

    def showEvent(self, event) -> None:
        # Values may have changed while hidden (undo/redo, automation).
        super().showEvent(event)
        self.refresh_from_engine()

    def refresh_from_engine(self) -> None:
        """Mirror current (possibly automated) values into the sliders."""
        if not self._sliders or not self.isVisible():
            return
        for index, slider in enumerate(self._sliders):
            if slider.isSliderDown():
                continue  # user is dragging this one
            value = self._bridge.vst_get_param(
                self._track_id, self._slot, index)
            ivalue = int(round(value * 1000))
            if slider.value() != ivalue:
                slider.blockSignals(True)
                slider.setValue(ivalue)
                slider.blockSignals(False)
