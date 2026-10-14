"""Transport bar: play/stop, time display, BPM, metronome, loop, snap,
follow, mixer button."""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QFontMetrics, QMouseEvent
from PySide6.QtWidgets import (QApplication, QComboBox, QDoubleSpinBox, QFrame,
                               QHBoxLayout, QLabel, QPushButton, QWidget)

from . import theme
from .i18n import tr
from .tool_icons import icon_size, mouse_icon, select_icon

# Snap choices: label -> beats. Disabling the snap is done with the
# Snap toggle button, so there is no "Off" resolution entry.
SNAP_CHOICES: list[tuple[str, float]] = [
    ("1 bar", 4.0),
    ("1 beat", 1.0),
    ("1/2", 0.5),
    ("1/4", 0.25),
    ("1/8", 0.125),
    ("1/16", 0.0625),
]

PPQ = 96
BEATS_PER_BAR = 4


def _fit_text_button(btn: QPushButton, min_width: int, height: int,
                     h_pad: int = 14) -> None:
    """Size a toolbar button so the current label fits (RU/EN safe)."""
    fm = QFontMetrics(btn.font())
    width = max(min_width, fm.horizontalAdvance(btn.text()) + h_pad)
    btn.setFixedSize(width, height)


class TimeDisplayLabel(QLabel):
    """Clickable transport clock: MM:SS.mmm or BAR:BEAT:TICK."""

    clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(128)
        self.setAlignment(Qt.AlignCenter)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(tr("Click to toggle time / bars display"))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class _StopButton(QPushButton):
    """Stop button: single click stops in place, double click rewinds to 0."""

    double_clicked = Signal()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self.double_clicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class TransportBar(QWidget):
    play_clicked = Signal()
    stop_clicked = Signal()
    stop_double_clicked = Signal()
    bpm_changed = Signal(float)
    metronome_toggled = Signal(bool)
    loop_toggled = Signal(bool)
    snap_changed = Signal(float)  # grid resolution in beats
    snap_toggled = Signal(bool)
    follow_toggled = Signal(bool)
    mixer_clicked = Signal()
    tool_selected = Signal(str)
    slice_mode_toggled = Signal(bool)
    tweak_toggled = Signal(bool)
    time_display_mode_changed = Signal(bool)  # True = bars mode

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(50)
        self.setStyleSheet(
            f"TransportBar {{ background: {theme.PANEL};"
            f" border-bottom: 1px solid {theme.BORDER_SOFT}; }}")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)

        self.play_btn = QPushButton("\u25b6")  # play triangle
        self.play_btn.setFixedSize(40, 32)
        self.play_btn.setToolTip(tr("Play/Pause (Space)"))
        self.play_btn.setStyleSheet(self._btn_style(theme.ACCENT_MIDI))
        self.play_btn.clicked.connect(self.play_clicked.emit)
        layout.addWidget(self.play_btn)

        self.stop_btn = _StopButton("\u25a0")  # stop square
        self.stop_btn.setFixedSize(40, 32)
        self.stop_btn.setToolTip(tr(
            "Stop (Ctrl+.) — double-click rewinds to start"))
        self.stop_btn.setStyleSheet(self._btn_style(theme.RED))
        self.stop_btn.clicked.connect(self.stop_clicked.emit)
        self.stop_btn.double_clicked.connect(self.stop_double_clicked.emit)
        layout.addWidget(self.stop_btn)

        self._bars_mode = False
        self._last_seconds = 0.0

        self.time_label = TimeDisplayLabel("00:00.000")
        # Scoped to the class: a selector-less rule would also style the
        # tooltip (QTipLabel inherits the widget's stylesheet), blowing it
        # up to the clock's 20px monospace font.
        self.time_label.setStyleSheet(
            f"TimeDisplayLabel {{ color: {theme.TEXT};"
            f" font-family: Consolas, monospace; font-size: 20px;"
            f" padding: 0 10px; background: transparent; }}")
        self.time_label.clicked.connect(self._toggle_time_display_mode)
        layout.addWidget(self.time_label)

        layout.addWidget(self._separator())

        # BPM ------------------------------------------------------------
        layout.addWidget(self._dim_label(tr("BPM")))
        self.bpm_spin = QDoubleSpinBox()
        self.bpm_spin.setRange(20.0, 300.0)
        self.bpm_spin.setDecimals(1)
        self.bpm_spin.setSingleStep(1.0)
        self.bpm_spin.setValue(120.0)
        self.bpm_spin.setFixedWidth(76)
        self.bpm_spin.setButtonSymbols(QDoubleSpinBox.UpDownArrows)
        self.bpm_spin.valueChanged.connect(self.bpm_changed.emit)
        self.bpm_spin.valueChanged.connect(self._on_bpm_spin_changed)
        layout.addWidget(self.bpm_spin)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

        # Playback toggles: metronome / loop / follow ----------------------
        self.metro_btn = QPushButton(tr("Metro"))
        self.metro_btn.setCheckable(True)
        _fit_text_button(self.metro_btn, 56, 30)
        self.metro_btn.setToolTip(tr("Metronome (Ctrl+M)"))
        self.metro_btn.setStyleSheet(self._toggle_style(theme.AMBER))
        self.metro_btn.toggled.connect(self.metronome_toggled.emit)
        layout.addWidget(self.metro_btn)

        self.loop_btn = QPushButton(tr("Loop"))
        self.loop_btn.setCheckable(True)
        _fit_text_button(self.loop_btn, 56, 30)
        self.loop_btn.setToolTip(tr(
            "Loop (Ctrl+L). Drag on the ruler to set the region"))
        self.loop_btn.setStyleSheet(self._toggle_style(theme.ACCENT))
        self.loop_btn.toggled.connect(self.loop_toggled.emit)
        layout.addWidget(self.loop_btn)

        self.follow_btn = QPushButton(tr("Follow"))
        self.follow_btn.setCheckable(True)
        self.follow_btn.setChecked(True)
        _fit_text_button(self.follow_btn, 60, 30)
        self.follow_btn.setToolTip(tr(
            "Auto-scroll the timeline with the playhead (Follow)"))
        self.follow_btn.setStyleSheet(self._toggle_style(theme.ACCENT))
        self.follow_btn.toggled.connect(self.follow_toggled.emit)
        layout.addWidget(self.follow_btn)

        layout.addWidget(self._separator())

        # Editing tools: snap (button + resolution), mouse/select and slice -
        self.snap_btn = QPushButton(tr("\U0001f9f2 Snap"))
        self.snap_btn.setCheckable(True)
        self.snap_btn.setChecked(True)
        _fit_text_button(self.snap_btn, 72, 30)
        self.snap_btn.setToolTip(tr(
            "Toggle snap while dragging clips and notes"))
        self.snap_btn.setStyleSheet(self._toggle_style(theme.ACCENT))
        self.snap_btn.toggled.connect(self.snap_toggled.emit)
        layout.addWidget(self.snap_btn)

        self.snap_combo = QComboBox()
        self._fill_snap_combo()
        self.snap_combo.setCurrentIndex(1)  # 1 beat
        self.snap_combo.setFixedWidth(74)
        self.snap_combo.setToolTip(tr("Snap grid step"))
        self.snap_combo.currentIndexChanged.connect(
            lambda i: self.snap_changed.emit(SNAP_CHOICES[i][1]))
        layout.addWidget(self.snap_combo)

        self.mouse_btn = QPushButton()
        self.mouse_btn.setCheckable(True)
        self.mouse_btn.setChecked(True)
        self.mouse_btn.setFixedSize(30, 30)
        self.mouse_btn.setIcon(mouse_icon(theme.TEXT_DIM))
        self.mouse_btn.setIconSize(icon_size())
        self.mouse_btn.setToolTip(tr("Mouse tool (1)"))
        self.mouse_btn.setStyleSheet(self._toggle_style(theme.ACCENT))
        self.mouse_btn.toggled.connect(
            lambda on: on and self.tool_selected.emit("mouse"))
        layout.addWidget(self.mouse_btn)

        self.select_btn = QPushButton()
        self.select_btn.setCheckable(True)
        self.select_btn.setFixedSize(30, 30)
        self.select_btn.setIcon(select_icon(theme.TEXT_DIM))
        self.select_btn.setIconSize(icon_size())
        self.select_btn.setToolTip(tr("Selection tool (2)"))
        self.select_btn.setStyleSheet(self._toggle_style(theme.ACCENT))
        self.select_btn.toggled.connect(
            lambda on: on and self.tool_selected.emit("select"))
        layout.addWidget(self.select_btn)

        self.slice_btn = QPushButton(tr("\u2702 Slice"))
        self.slice_btn.setCheckable(True)
        _fit_text_button(self.slice_btn, 72, 30)
        self.slice_btn.setToolTip(tr("Slice tool — click to cut a clip (C)"))
        self.slice_btn.setStyleSheet(self._toggle_style(theme.RED))
        self.slice_btn.toggled.connect(self.slice_mode_toggled.emit)
        layout.addWidget(self.slice_btn)

        self.tweak_btn = QPushButton(tr("Tweak"))
        self.tweak_btn.setCheckable(True)
        _fit_text_button(self.tweak_btn, 64, 30)
        self.tweak_btn.setToolTip(tr(
            "TWEAK mode: adjust any knob or slider\n"
            "(built-in or VST3) — PyDAW offers to create\n"
            "an automation lane for that parameter"))
        self.tweak_btn.setStyleSheet(self._toggle_style(theme.AMBER))
        self.tweak_btn.toggled.connect(self.tweak_toggled.emit)
        layout.addWidget(self.tweak_btn)

        self.mixer_btn = QPushButton(tr("Mixer"))
        _fit_text_button(self.mixer_btn, 60, 30)
        self.mixer_btn.setToolTip(tr("Mixer window (Ctrl+Shift+M)"))
        self.mixer_btn.setStyleSheet(self._toggle_style(theme.ACCENT))
        self.mixer_btn.clicked.connect(self.mixer_clicked.emit)
        layout.addWidget(self.mixer_btn)

        layout.addStretch(1)

    def _fill_snap_combo(self) -> None:
        idx = self.snap_combo.currentIndex() if hasattr(self, "snap_combo") \
            else 1
        self.snap_combo.blockSignals(True)
        self.snap_combo.clear()
        for label, _ in SNAP_CHOICES:
            self.snap_combo.addItem(tr(label))
        if 0 <= idx < self.snap_combo.count():
            self.snap_combo.setCurrentIndex(idx)
        self.snap_combo.blockSignals(False)
        fm = QFontMetrics(self.snap_combo.font())
        max_w = max(fm.horizontalAdvance(self.snap_combo.itemText(i))
                    for i in range(self.snap_combo.count()))
        self.snap_combo.setFixedWidth(max(74, max_w + 28))

    def retranslate(self) -> None:
        """Refresh labels after a live language switch."""
        self.play_btn.setToolTip(tr("Play/Pause (Space)"))
        self.stop_btn.setToolTip(tr(
            "Stop (Ctrl+.) — double-click rewinds to start"))
        self.time_label.setToolTip(tr("Click to toggle time / bars display"))
        self.metro_btn.setText(tr("Metro"))
        _fit_text_button(self.metro_btn, 56, 30)
        self.metro_btn.setToolTip(tr("Metronome (Ctrl+M)"))
        self.loop_btn.setText(tr("Loop"))
        _fit_text_button(self.loop_btn, 56, 30)
        self.loop_btn.setToolTip(tr(
            "Loop (Ctrl+L). Drag on the ruler to set the region"))
        self.follow_btn.setText(tr("Follow"))
        _fit_text_button(self.follow_btn, 60, 30)
        self.follow_btn.setToolTip(tr(
            "Auto-scroll the timeline with the playhead (Follow)"))
        self.snap_btn.setText(tr("\U0001f9f2 Snap"))
        _fit_text_button(self.snap_btn, 72, 30)
        self.snap_btn.setToolTip(tr(
            "Toggle snap while dragging clips and notes"))
        self.snap_combo.setToolTip(tr("Snap grid step"))
        self._fill_snap_combo()
        self.mouse_btn.setText("")
        self.mouse_btn.setIcon(mouse_icon(theme.TEXT_DIM))
        self.mouse_btn.setIconSize(icon_size())
        self.mouse_btn.setToolTip(tr("Mouse tool (1)"))
        self.select_btn.setText("")
        self.select_btn.setIcon(select_icon(theme.TEXT_DIM))
        self.select_btn.setIconSize(icon_size())
        self.select_btn.setToolTip(tr("Selection tool (2)"))
        self.slice_btn.setText(tr("\u2702 Slice"))
        _fit_text_button(self.slice_btn, 72, 30)
        self.slice_btn.setToolTip(tr("Slice tool — click to cut a clip (C)"))
        self.tweak_btn.setText(tr("Tweak"))
        _fit_text_button(self.tweak_btn, 64, 30)
        self.tweak_btn.setToolTip(tr(
            "TWEAK mode: adjust any knob or slider\n"
            "(built-in or VST3) — PyDAW offers to create\n"
            "an automation lane for that parameter"))
        self.mixer_btn.setText(tr("Mixer"))
        _fit_text_button(self.mixer_btn, 60, 30)
        self.mixer_btn.setToolTip(tr("Mixer window (Ctrl+Shift+M)"))

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.MouseButtonPress \
                and self._bpm_editor_has_focus() \
                and not self._is_bpm_editor_widget(obj):
            self.finish_bpm_edit()
        return super().eventFilter(obj, event)

    def _bpm_editor_has_focus(self) -> bool:
        editor = self.bpm_spin.lineEdit()
        return self.bpm_spin.hasFocus() or editor.hasFocus()

    def _is_bpm_editor_widget(self, obj) -> bool:
        if not isinstance(obj, QWidget):
            return False
        widget = obj
        while widget is not None:
            if widget is self.bpm_spin:
                return True
            widget = widget.parentWidget()
        return False

    def finish_bpm_edit(self) -> None:
        """Commit manual BPM text and leave spinbox edit mode."""
        text = self.bpm_spin.lineEdit().text().strip()
        value = None
        parsed, ok = self.bpm_spin.locale().toDouble(text)
        if ok:
            value = parsed
        else:
            try:
                value = float(text.replace(",", "."))
            except ValueError:
                pass
        if value is not None:
            value = max(self.bpm_spin.minimum(),
                        min(self.bpm_spin.maximum(), value))
            self.bpm_spin.setValue(value)
        else:
            self.bpm_spin.interpretText()
        self.bpm_spin.clearFocus()
        self.bpm_spin.lineEdit().clearFocus()

    @staticmethod
    def _dim_label(text: str) -> QLabel:
        return QLabel(text, styleSheet=f"color: {theme.TEXT_DIM};"
                      " font-size: 11px; background: transparent;")

    @staticmethod
    def _separator() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.VLine)
        line.setStyleSheet(f"color: {theme.BORDER_SOFT};")
        return line

    @staticmethod
    def _btn_style(accent: str) -> str:
        return (f"QPushButton {{ background: {theme.RAISED}; color: {accent};"
                f" border: 1px solid {theme.BORDER_SOFT};"
                f" border-radius: 5px; font-size: 15px; }}"
                f"QPushButton:hover {{ background: {theme.HOVER}; }}"
                f"QPushButton:pressed {{ background: {accent};"
                f" color: white; }}")

    @staticmethod
    def _toggle_style(accent: str) -> str:
        return (f"QPushButton {{ background: {theme.RAISED};"
                f" color: {theme.TEXT_DIM};"
                f" border: 1px solid {theme.BORDER_SOFT};"
                f" border-radius: 5px; font-size: 11px; }}"
                f"QPushButton:hover {{ background: {theme.HOVER};"
                f" color: {theme.TEXT}; }}"
                f"QPushButton:checked {{ background: {accent};"
                f" color: white; border-color: {accent}; }}")

    def set_playing(self, playing: bool) -> None:
        self.play_btn.setText("\u23f8" if playing else "\u25b6")

    def set_time(self, seconds: float) -> None:
        self._last_seconds = seconds
        self._refresh_time_label()

    def bars_mode(self) -> bool:
        return self._bars_mode

    def set_bars_mode(self, on: bool) -> None:
        if self._bars_mode == on:
            return
        self._bars_mode = on
        self._refresh_time_label()

    def toggle_time_display_mode(self) -> None:
        self._toggle_time_display_mode()

    def set_slice_mode(self, on: bool) -> None:
        self.mouse_btn.blockSignals(True)
        self.select_btn.blockSignals(True)
        self.slice_btn.blockSignals(True)
        if on:
            self.mouse_btn.setChecked(False)
            self.select_btn.setChecked(False)
            self.slice_btn.setChecked(True)
        else:
            self.mouse_btn.setChecked(True)
            self.select_btn.setChecked(False)
            self.slice_btn.setChecked(False)
        self.mouse_btn.blockSignals(False)
        self.select_btn.blockSignals(False)
        self.slice_btn.blockSignals(False)

    def set_tool(self, tool: str) -> None:
        is_select = tool == "select"
        self.mouse_btn.blockSignals(True)
        self.select_btn.blockSignals(True)
        self.slice_btn.blockSignals(True)
        self.mouse_btn.setChecked(not is_select)
        self.select_btn.setChecked(is_select)
        self.slice_btn.setChecked(False)
        self.mouse_btn.blockSignals(False)
        self.select_btn.blockSignals(False)
        self.slice_btn.blockSignals(False)

    def set_tweak_mode(self, on: bool) -> None:
        self.tweak_btn.blockSignals(True)
        self.tweak_btn.setChecked(on)
        self.tweak_btn.blockSignals(False)

    def _toggle_time_display_mode(self) -> None:
        self._bars_mode = not self._bars_mode
        self._refresh_time_label()
        self.time_display_mode_changed.emit(self._bars_mode)

    def _refresh_time_label(self) -> None:
        if self._bars_mode:
            self.time_label.setText(
                self._seconds_to_bars(self._last_seconds,
                                      self.bpm_spin.value()))
        else:
            seconds = self._last_seconds
            mm = int(seconds // 60)
            ss = seconds - mm * 60
            self.time_label.setText(f"{mm:02d}:{ss:06.3f}")

    @staticmethod
    def _seconds_to_bars(seconds: float, bpm: float) -> str:
        beats = seconds * bpm / 60.0
        beats_floor = int(beats)
        bar = beats_floor // BEATS_PER_BAR + 1
        beat_in_bar = beats_floor % BEATS_PER_BAR + 1
        tick = int(round((beats - beats_floor) * PPQ))
        if tick >= PPQ:
            tick = 0
            beat_in_bar += 1
            if beat_in_bar > BEATS_PER_BAR:
                beat_in_bar = 1
                bar += 1
        return f"{bar:03d}:{beat_in_bar:02d}:{tick:02d}"

    def set_bpm(self, bpm: float) -> None:
        self.bpm_spin.blockSignals(True)
        self.bpm_spin.setValue(bpm)
        self.bpm_spin.blockSignals(False)
        if self._bars_mode:
            self._refresh_time_label()

    def _on_bpm_spin_changed(self, _bpm: float) -> None:
        if self._bars_mode:
            self._refresh_time_label()

    def set_metronome(self, on: bool) -> None:
        self.metro_btn.blockSignals(True)
        self.metro_btn.setChecked(on)
        self.metro_btn.blockSignals(False)

    def set_loop(self, on: bool) -> None:
        self.loop_btn.blockSignals(True)
        self.loop_btn.setChecked(on)
        self.loop_btn.blockSignals(False)
