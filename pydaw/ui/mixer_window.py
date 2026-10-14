"""Mixer window: vertical channel strips for every track + master section.

Gain/pan/mute/solo changes are emitted with the same signatures as the
track panel, so MainWindow wires them to the same handlers. The window
is non-modal and rebuilt whenever the track list changes.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel,
                               QPushButton, QScrollArea, QSlider, QVBoxLayout,
                               QWidget)

from ..engine_bridge import MASTER_FX_ID
from ..model.project import Project, Track
from . import theme
from .i18n import tr
from .knob import ResetSlider, StereoWidthKnob
from .track_header import GROUP_CHILD_INDENT, METER_HEADROOM


def _track_accent(track: Track) -> str:
    """Per-track accent color (same source as the left track header)."""
    if track.color and QColor(track.color).isValid():
        return track.color
    if track.is_group:
        return theme.AMBER
    if track.is_midi:
        return theme.ACCENT_MIDI
    return theme.ACCENT


def _mix_hex(base: str, tint: str, amount: float) -> str:
    """Blend ``tint`` into ``base`` (0..1) for group cluster backgrounds."""
    b = QColor(base)
    t = QColor(tint)
    if not t.isValid():
        return base
    inv = 1.0 - amount
    return QColor(
        round(b.red() * inv + t.red() * amount),
        round(b.green() * inv + t.green() * amount),
        round(b.blue() * inv + t.blue() * amount),
    ).name()


class GroupStripCluster(QFrame):
    """Visual frame wrapping a group bus strip and its member channels."""

    def __init__(self, group: Track, parent=None) -> None:
        super().__init__(parent)
        self.group = group
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(4, 4, 4, 4)
        self._layout.setSpacing(4)
        self.apply_style()

    def apply_style(self) -> None:
        """Theme-aware cluster chrome tinted with the group track color."""
        tint = _track_accent(self.group)
        bg = _mix_hex(theme.SURFACE, tint, 0.10)
        self.setStyleSheet(
            f"GroupStripCluster {{ background: {bg};"
            f" border: 1px solid {theme.BORDER_SOFT};"
            f" border-left: 3px solid {tint};"
            f" border-radius: 8px; }}")

    def add_strip(self, widget: QWidget, *, is_child: bool = False) -> None:
        if is_child:
            self._layout.addSpacing(max(0, GROUP_CHILD_INDENT - 4))
        self._layout.addWidget(widget)


class VerticalMeter(QWidget):
    """Stereo peak meter drawn as two vertical bars with a 0 dBFS mark."""

    BAR_AREA_W = 14

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedWidth(24)  # bars + "0" label
        self.setMinimumHeight(80)
        self._l = 0.0
        self._r = 0.0

    def set_levels(self, l: float, r: float) -> None:
        self._l = max(l, self._l * 0.88)
        self._r = max(r, self._r * 0.88)
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(0, 0, self.BAR_AREA_W, self.height(),
                   QColor(theme.LANE_SEP))
        h = self.height()
        zero_y = h - int(h / METER_HEADROOM)
        for i, level in enumerate((self._l, self._r)):
            bar_h = int(min(1.0, level / METER_HEADROOM) * h)
            color = theme.COL_METER_OK if level < 1.0 else theme.COL_METER_HOT
            p.fillRect(i * 7 + 1, h - bar_h, 5, bar_h, color)
        # 0 dBFS reference: tick + label (theme text colors, re-read at
        # paint time so a live theme switch restyles the mark).
        p.fillRect(0, zero_y, self.BAR_AREA_W, 1, QColor(theme.TEXT_FAINT))
        font = p.font()
        font.setPixelSize(9)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor(theme.TEXT_DIM))
        p.drawText(self.BAR_AREA_W + 2, zero_y + 4, "0")
        p.end()


STRIP_WIDTH = 100
CHILD_STRIP_WIDTH = 92


class ChannelStrip(QFrame):
    gain_changed = Signal(int, float)
    pan_changed = Signal(int, float)
    width_changed = Signal(int, float)
    mute_toggled = Signal(int, bool)
    solo_toggled = Signal(int, bool)
    fx_requested = Signal(int)

    def __init__(self, track: Track, parent=None, *,
                 is_group_child: bool = False) -> None:
        super().__init__(parent)
        self.track = track
        self._is_group_child = is_group_child
        self.setFixedWidth(CHILD_STRIP_WIDTH if is_group_child else STRIP_WIDTH)
        self.apply_style()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        if track.is_group:
            badge_text = tr("GROUP")
        elif track.is_midi:
            badge_text = tr("MIDI")
        else:
            badge_text = tr("AUDIO")
        accent = _track_accent(track)
        self._badge_label = QLabel(badge_text)
        self._badge_label.setAlignment(Qt.AlignCenter)
        self._badge_label.setStyleSheet(
            f"color: {accent}; font-size: 8px; font-weight: bold;"
            " letter-spacing: 1px; background: transparent; border: none;")
        layout.addWidget(self._badge_label)

        self._name_label = QLabel(track.name)
        self._name_label.setAlignment(Qt.AlignCenter)
        self._name_label.setWordWrap(True)
        self._name_label.setMaximumHeight(28)
        self._name_label.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 10px; font-weight: bold;"
            " background: transparent; border: none;")
        layout.addWidget(self._name_label)

        pan_row = QHBoxLayout()
        pan_row.setSpacing(4)
        self.pan_slider = ResetSlider(Qt.Horizontal, 0, tr("center"))
        self.pan_slider.setRange(-100, 100)
        self.pan_slider.setValue(int(track.pan * 100))
        self.pan_slider.setToolTip(tr("Pan (RMB — reset)"))
        self.pan_slider.valueChanged.connect(
            lambda v: self.pan_changed.emit(self.track.id, v / 100.0))
        pan_row.addWidget(self.pan_slider, 1)
        self.width_knob = StereoWidthKnob(track.stereo_width)
        self.width_knob.width_changed.connect(
            lambda w: self.width_changed.emit(self.track.id, w))
        pan_row.addWidget(self.width_knob)
        layout.addLayout(pan_row)

        fader_row = QHBoxLayout()
        fader_row.setSpacing(6)
        self.gain_slider = ResetSlider(Qt.Vertical, 100, "0 dB")
        self.gain_slider.setRange(0, 150)
        self.gain_slider.setValue(int(track.gain * 100))
        self.gain_slider.setToolTip(tr("Volume (RMB — reset)"))
        self.gain_slider.valueChanged.connect(self._on_gain)
        fader_row.addWidget(self.gain_slider, 1, Qt.AlignHCenter)
        self.meter = VerticalMeter()
        fader_row.addWidget(self.meter)
        layout.addLayout(fader_row, 1)

        self.gain_label = QLabel(self._gain_text(track.gain))
        self.gain_label.setAlignment(Qt.AlignCenter)
        self.gain_label.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; font-size: 9px;"
            " background: transparent; border: none;")
        layout.addWidget(self.gain_label)

        ms_row = QHBoxLayout()
        ms_row.setSpacing(3)
        self.mute_btn = self._small_button("M", theme.AMBER, tr("Mute"))
        self.mute_btn.setChecked(track.mute)
        self.mute_btn.toggled.connect(
            lambda v: self.mute_toggled.emit(self.track.id, v))
        ms_row.addWidget(self.mute_btn, 1)
        self.solo_btn = self._small_button("S", theme.ACCENT_MIDI, tr("Solo"))
        self.solo_btn.setChecked(track.solo)
        self.solo_btn.toggled.connect(
            lambda v: self.solo_toggled.emit(self.track.id, v))
        ms_row.addWidget(self.solo_btn, 1)
        layout.addLayout(ms_row)

        self._fx_btn = self._fx_button(tr("Effect chain"))
        self._fx_btn.clicked.connect(
            lambda: self.fx_requested.emit(self.track.id))
        layout.addWidget(self._fx_btn)

    def apply_style(self) -> None:
        """Refresh strip chrome from theme tokens + ``track.color``."""
        accent = _track_accent(self.track)
        border_left = (f" border-left: 4px solid {accent};"
                       if self._is_group_child else "")
        self.setStyleSheet(
            f"ChannelStrip {{ background: {theme.SURFACE};"
            f" border: 1px solid {theme.BORDER_SOFT}; border-radius: 6px;"
            f" border-top: 3px solid {accent};{border_left} }}")

    def retranslate(self) -> None:
        if self.track.is_group:
            self._badge_label.setText(tr("GROUP"))
        elif self.track.is_midi:
            self._badge_label.setText(tr("MIDI"))
        else:
            self._badge_label.setText(tr("AUDIO"))
        self.apply_style()
        self.pan_slider._default_label = tr("center")  # noqa: SLF001
        self.pan_slider.setToolTip(tr("Pan (RMB — reset)"))
        self.gain_slider.setToolTip(tr("Volume (RMB — reset)"))
        self.mute_btn.setToolTip(tr("Mute"))
        self.solo_btn.setToolTip(tr("Solo"))
        self._fx_btn.setToolTip(tr("Effect chain"))

    def _on_gain(self, value: int) -> None:
        self.gain_changed.emit(self.track.id, value / 100.0)
        self.gain_label.setText(self._gain_text(value / 100.0))

    @staticmethod
    def _gain_text(gain: float) -> str:
        if gain <= 0.001:
            return "-inf dB"
        import math
        return f"{20.0 * math.log10(gain):+.1f} dB"

    @staticmethod
    def _fx_button(tip: str = "") -> QPushButton:
        """Full-width FX button (same role as the master strip FX)."""
        btn = QPushButton("FX")
        btn.setCheckable(False)
        btn.setFixedHeight(22)
        if tip:
            btn.setToolTip(tip)
        btn.setStyleSheet(
            f"QPushButton {{ background: {theme.RAISED};"
            f" color: {theme.TEXT_DIM}; border: none; border-radius: 3px;"
            f" font-size: 10px; font-weight: bold; padding: 0 4px; }}"
            f"QPushButton:hover {{ background: {theme.HOVER};"
            f" color: {theme.TEXT}; }}")
        return btn

    @staticmethod
    def _small_button(text: str, color: str, tip: str = "") -> QPushButton:
        btn = QPushButton(text)
        btn.setCheckable(True)
        btn.setFixedHeight(20)
        if tip:
            btn.setToolTip(tip)
        btn.setStyleSheet(
            f"QPushButton {{ background: {theme.RAISED};"
            f" color: {theme.TEXT_DIM}; border: none; border-radius: 3px;"
            f" font-size: 10px; font-weight: bold; padding: 0 6px; }}"
            f"QPushButton:hover {{ background: {theme.HOVER};"
            f" color: {theme.TEXT}; }}"
            f"QPushButton:checked {{ background: {color}; color: white; }}")
        return btn

    def sync_from_track(self) -> None:
        """Refresh controls from the model (edits made elsewhere)."""
        for widget, value in ((self.gain_slider, int(self.track.gain * 100)),
                              (self.pan_slider, int(self.track.pan * 100))):
            widget.blockSignals(True)
            widget.setValue(value)
            widget.blockSignals(False)
        for btn, state in ((self.mute_btn, self.track.mute),
                           (self.solo_btn, self.track.solo)):
            btn.blockSignals(True)
            btn.setChecked(state)
            btn.blockSignals(False)
        self.width_knob.set_width(self.track.stereo_width)
        self.gain_label.setText(self._gain_text(self.track.gain))

    def show_values(self, gain: float | None, pan: float | None,
                    width: float | None) -> None:
        """Display automated values without emitting change signals."""
        if gain is not None and not self.gain_slider.isSliderDown():
            self.gain_slider.blockSignals(True)
            self.gain_slider.setValue(int(round(gain * 100)))
            self.gain_slider.blockSignals(False)
            self.gain_label.setText(self._gain_text(gain))
        if pan is not None and not self.pan_slider.isSliderDown():
            self.pan_slider.blockSignals(True)
            self.pan_slider.setValue(int(round(pan * 100)))
            self.pan_slider.blockSignals(False)
        if width is not None:
            self.width_knob.set_width(width)


class MixerWindow(QDialog):
    gain_changed = Signal(int, float)
    pan_changed = Signal(int, float)
    width_changed = Signal(int, float)
    mute_toggled = Signal(int, bool)
    solo_toggled = Signal(int, bool)
    fx_requested = Signal(int)
    master_gain_changed = Signal(float)
    undo_requested = Signal()
    redo_requested = Signal()

    def __init__(self, project: Project, parent=None) -> None:
        super().__init__(parent)
        self.project = project
        self.strips: dict[int, ChannelStrip] = {}
        self.setWindowTitle(tr("Mixer"))
        self.resize(720, 380)

        # Undo/redo works while the mixer window has focus: the main
        # window's menu shortcuts don't fire for an active dialog.
        sc_undo = QShortcut(QKeySequence.Undo, self)
        sc_undo.setContext(Qt.WidgetWithChildrenShortcut)
        sc_undo.activated.connect(self.undo_requested.emit)
        sc_redo = QShortcut(QKeySequence.Redo, self)
        sc_redo.setContext(Qt.WidgetWithChildrenShortcut)
        sc_redo.activated.connect(self.redo_requested.emit)

        root = QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # Master section: leftmost strip, aligned with channel strips.
        master = QFrame(self)
        master.setFixedWidth(STRIP_WIDTH + 6)
        master.setStyleSheet(
            f"QFrame {{ background: {theme.SURFACE};"
            f" border: 1px solid {theme.BORDER_SOFT};"
            f" border-top: 3px solid {theme.RED};"
            f" border-radius: 6px; }}")
        mbox = QVBoxLayout(master)
        mbox.setContentsMargins(6, 6, 6, 6)
        mbox.setSpacing(4)
        mlabel = QLabel(tr("MASTER"))
        mlabel.setAlignment(Qt.AlignCenter)
        mlabel.setStyleSheet(
            f"color: {theme.RED}; font-size: 8px; font-weight: bold;"
            " letter-spacing: 1px; background: transparent; border: none;")
        mbox.addWidget(mlabel)
        mrow = QHBoxLayout()
        mrow.setSpacing(6)
        self.master_slider = ResetSlider(Qt.Vertical, 100, "0 dB")
        self.master_slider.setRange(0, 150)
        self.master_slider.setValue(int(project.master_gain * 100))
        self.master_slider.setToolTip(tr("Master volume (RMB — reset)"))
        self.master_slider.valueChanged.connect(
            lambda v: self.master_gain_changed.emit(v / 100.0))
        mrow.addWidget(self.master_slider, 1, Qt.AlignHCenter)
        self.master_meter = VerticalMeter()
        mrow.addWidget(self.master_meter)
        mbox.addLayout(mrow, 1)
        self.master_fx_btn = ChannelStrip._fx_button(tr("Master effect chain"))
        self.master_fx_btn.clicked.connect(
            lambda: self.fx_requested.emit(MASTER_FX_ID))
        mbox.addWidget(self.master_fx_btn)
        root.addWidget(master)
        root.addSpacing(6)

        # Parent at construction: a parentless widget polished before
        # insertion can flash as a tiny top-level window on Windows.
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._strips_widget = QWidget(self._scroll)
        self._strips_layout = QHBoxLayout(self._strips_widget)
        self._strips_layout.setContentsMargins(0, 0, 0, 0)
        self._strips_layout.setSpacing(6)
        self._strips_layout.addStretch(1)
        self._scroll.setWidget(self._strips_widget)
        root.addWidget(self._scroll, 1)

        self.rebuild()

    def retranslate(self) -> None:
        self.setWindowTitle(tr("Mixer"))
        self.master_slider.setToolTip(tr("Master volume (RMB — reset)"))
        self.master_fx_btn.setToolTip(tr("Master effect chain"))
        for widget in self._strips_widget.findChildren(GroupStripCluster):
            widget.apply_style()
        for strip in self.strips.values():
            strip.apply_style()
            strip.retranslate()

    def rebuild(self) -> None:
        while self._strips_layout.count() > 1:
            item = self._strips_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.strips.clear()

        insert_at = 0
        tracks = self.project.tracks
        i = 0
        while i < len(tracks):
            track = tracks[i]
            if track.is_group:
                cluster = GroupStripCluster(track, parent=self._strips_widget)
                group_strip = self._add_strip(track, cluster)
                i += 1
                while i < len(tracks) and tracks[i].group_id == track.id:
                    self._add_strip(tracks[i], cluster, is_group_child=True)
                    i += 1
                self._strips_layout.insertWidget(insert_at, cluster)
                insert_at += 1
            else:
                self._add_strip(track, self._strips_widget)
                insert_at += 1
                i += 1

    def _add_strip(self, track: Track, parent: QWidget, *,
                   is_group_child: bool = False) -> ChannelStrip:
        if isinstance(parent, GroupStripCluster):
            strip = ChannelStrip(track, parent=parent,
                                 is_group_child=is_group_child)
            parent.add_strip(strip, is_child=is_group_child)
        else:
            strip = ChannelStrip(track, parent=parent,
                                 is_group_child=is_group_child)
            pos = self._strips_layout.count() - 1  # before stretch
            self._strips_layout.insertWidget(pos, strip)
        strip.gain_changed.connect(self.gain_changed.emit)
        strip.pan_changed.connect(self.pan_changed.emit)
        strip.width_changed.connect(self.width_changed.emit)
        strip.mute_toggled.connect(self.mute_toggled.emit)
        strip.solo_toggled.connect(self.solo_toggled.emit)
        strip.fx_requested.connect(self.fx_requested.emit)
        self.strips[track.id] = strip
        return strip

    def sync_strip(self, track_id: int) -> None:
        strip = self.strips.get(track_id)
        if strip:
            strip.sync_from_track()

    def set_master(self, gain: float) -> None:
        self.master_slider.blockSignals(True)
        self.master_slider.setValue(int(gain * 100))
        self.master_slider.blockSignals(False)

    def update_meters(self, bridge) -> None:
        for track_id, strip in self.strips.items():
            l, r = bridge.track_peaks(track_id)
            strip.meter.set_levels(l, r)
        self.master_meter.set_levels(*bridge.master_peaks())

    def reset_meters(self) -> None:
        for strip in self.strips.values():
            strip.meter.reset()
        self.master_meter.reset()
