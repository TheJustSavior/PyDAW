"""Track header panel: name, mute/solo, fader, pan, meter, instrument."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QMenu, QPushButton, QScrollArea, QSlider,
                               QVBoxLayout, QWidget)

from ..model.project import EFFECT_DEFS, Project, Track
from . import theme
from .i18n import tr, trf
from .knob import ResetSlider, StereoWidthKnob

AUTO_LANE_HEIGHT = 44
AUTO_LANE_HEIGHT_SELECTED = 104

NO_INSTRUMENT_LABEL = "No instrument"
GROUP_CHILD_INDENT = 14
# Extra scrollable space under the last track: keeps the panel scroll range
# at least as large as the timeline's and leaves an always-reachable empty
# strip for the right-click "add track" menu.
PANEL_BOTTOM_PAD = 120
# Gap between the master strip and the ordinary tracks below; must
# match timeline.MASTER_GAP so the rows stay aligned.
MASTER_GAP = 12


# Top of the meter scale in linear amplitude: leaves ~2 dB of headroom
# above 0 dBFS so the "0" mark sits inside the bar, not at its very end.
METER_HEADROOM = 1.25


class LevelMeter(QWidget):
    """Tiny horizontal stereo peak meter with a 0 dBFS mark."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(9)
        self._l = 0.0
        self._r = 0.0

    def set_levels(self, l: float, r: float) -> None:
        # Peak-hold style decay.
        self._l = max(l, self._l * 0.85)
        self._r = max(r, self._r * 0.85)
        self.update()

    def reset(self) -> None:
        self._l = 0.0
        self._r = 0.0
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(theme.LANE_SEP))
        w = self.width()
        zero_x = int(w / METER_HEADROOM)
        for i, level in enumerate((self._l, self._r)):
            length = int(min(1.0, level / METER_HEADROOM) * w)
            color = theme.COL_METER_OK if level < 1.0 else theme.COL_METER_HOT
            p.fillRect(0, i * 5, length, 4, color)
        # 0 dBFS reference: tick + label (theme text colors, re-read at
        # paint time so a live theme switch restyles the mark).
        p.fillRect(zero_x, 0, 1, self.height(), QColor(theme.TEXT_FAINT))
        font = p.font()
        font.setPixelSize(9)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor(theme.TEXT_DIM))
        p.drawText(zero_x + 3, 9, "0")
        p.end()


class TrackHeader(QFrame):
    gain_changed = Signal(int, float)
    pan_changed = Signal(int, float)
    width_changed = Signal(int, float)
    mute_toggled = Signal(int, bool)
    solo_toggled = Signal(int, bool)
    name_changed = Signal(int, str)
    remove_requested = Signal(int)
    vst_plugin_requested = Signal(int)     # track_id
    vst_editor_requested = Signal(int)     # track_id (instrument slot -1)
    clicked = Signal(int)                  # track selected (header click)
    fold_toggled = Signal(int)             # group track_id
    move_to_group = Signal(int, int)       # track_id, group_id
    remove_from_group = Signal(int)        # track_id
    drag_started = Signal(int)             # track_id
    drag_moved = Signal(int, QPoint)       # track_id, global pos
    drag_dropped = Signal(int, QPoint)     # track_id, global pos
    add_automation = Signal(int, str, int, int, str)
    add_automation_vst_pick = Signal(int, int)  # track_id, fx_slot (-1 = inst)
    auto_fold_toggled = Signal(int)            # track_id (collapse lanes)
    properties_requested = Signal(int)         # track_id
    rename_requested = Signal(int)             # track_id (rename dialog)

    def __init__(self, track: Track, height: int, *,
                 is_child: bool = False, folded: bool = False,
                 auto_folded: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.track = track
        self._folded = folded
        self._auto_folded = auto_folded
        self.setFixedHeight(height)

        self._accent = track.color
        self._is_child = is_child
        self._selected = False
        self._drop_target = False
        self._press_pos: QPoint | None = None
        self._dragging = False
        self._apply_frame_style()

        layout = QVBoxLayout(self)
        margin_left = GROUP_CHILD_INDENT if is_child else 8
        layout.setContentsMargins(margin_left, 5, 8, 5)
        layout.setSpacing(3)

        top = QHBoxLayout()
        top.setSpacing(3)

        if track.is_group:
            badge_text = "GROUP"
        elif track.is_midi:
            badge_text = "MIDI"
        else:
            badge_text = "AUDIO"
        self.badge = QLabel(badge_text, self)
        self.badge.setStyleSheet(
            f"color: {self._accent}; background: transparent;"
            " font-size: 9px; font-weight: bold; letter-spacing: 1px;")
        top.addWidget(self.badge)

        if track.is_group:
            self.fold_btn = QPushButton(
                "\u25be" if not folded else "\u25b8", self)
            self.fold_btn.setFixedSize(18, 19)
            self.fold_btn.setToolTip(tr("Fold/unfold child tracks"))
            self.fold_btn.setCheckable(False)
            self.fold_btn.setStyleSheet(
                f"QPushButton {{ background: {theme.RAISED};"
                f" color: {theme.TEXT_DIM}; border: none; border-radius: 3px;"
                f" font-size: 11px; padding: 0; }}"
                f"QPushButton:hover {{ background: {theme.HOVER};"
                f" color: {theme.TEXT}; }}")
            self.fold_btn.clicked.connect(
                lambda: self.fold_toggled.emit(self.track.id))
            top.addWidget(self.fold_btn)

        self.name_edit = QLineEdit(track.name, self)
        self.name_edit.setStyleSheet(
            f"QLineEdit {{ background: transparent; border: none;"
            f" color: {theme.TEXT}; font-size: 12px; font-weight: bold;"
            f" padding: 0; }}"
            f"QLineEdit:focus {{ background: {theme.RAISED};"
            f" border-radius: 3px; }}")
        self.name_edit.setToolTip(tr("Ctrl+Shift+R — rename"))
        # Keep the name legible next to the buttons: never collapse below
        # a readable width and always show the beginning of the name.
        self.name_edit.setMinimumWidth(48)
        self.name_edit.setCursorPosition(0)
        # Rename only via double click: clicks pass through to the header.
        self.name_edit.setReadOnly(True)
        self.name_edit.setFocusPolicy(Qt.NoFocus)
        self.name_edit.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.name_edit.editingFinished.connect(self._finish_rename)
        top.addWidget(self.name_edit, 1)

        if track.automation_lanes:
            self.auto_fold_btn = QPushButton(
                "\u25b8A" if auto_folded else "\u25beA", self)
            self.auto_fold_btn.setFixedSize(19, 19)
            self.auto_fold_btn.setToolTip(
                tr("Fold/unfold automation lanes"))
            self.auto_fold_btn.setStyleSheet(
                f"QPushButton {{ background: {theme.RAISED};"
                f" color: {theme.TEXT_DIM}; border: none; border-radius: 3px;"
                f" font-size: 9px; padding: 0; }}"
                f"QPushButton:hover {{ background: {theme.HOVER};"
                f" color: {theme.TEXT}; }}")
            self.auto_fold_btn.clicked.connect(
                lambda: self.auto_fold_toggled.emit(self.track.id))
            top.addWidget(self.auto_fold_btn)

        self.mute_btn = self._small_button("M", theme.AMBER, tr("Mute"))
        self.mute_btn.setChecked(track.mute)
        self.mute_btn.toggled.connect(
            lambda v: self.mute_toggled.emit(self.track.id, v))
        top.addWidget(self.mute_btn)

        self.solo_btn = self._small_button("S", theme.ACCENT_MIDI, tr("Solo"))
        self.solo_btn.setChecked(track.solo)
        self.solo_btn.toggled.connect(
            lambda v: self.solo_toggled.emit(self.track.id, v))
        top.addWidget(self.solo_btn)

        close_btn = self._small_button("\u00d7", theme.RED,
                                         tr("Delete track"))
        close_btn.setCheckable(False)
        close_btn.clicked.connect(
            lambda: self.remove_requested.emit(self.track.id))
        top.addWidget(close_btn)
        layout.addLayout(top)

        mid = QHBoxLayout()
        mid.setSpacing(5)
        mid.addWidget(self._dim_label(tr("Vol")))
        self.gain_slider = ResetSlider(Qt.Horizontal, 100, "0 dB")
        self.gain_slider.setRange(0, 150)  # 0..1.5 linear
        self.gain_slider.setValue(int(track.gain * 100))
        self.gain_slider.setToolTip(tr("Volume (RMB — reset)"))
        self.gain_slider.valueChanged.connect(
            lambda v: self.gain_changed.emit(self.track.id, v / 100.0))
        mid.addWidget(self.gain_slider, 1)

        mid.addWidget(self._dim_label(tr("Pan")))
        self.pan_slider = ResetSlider(Qt.Horizontal, 0, tr("center"))
        self.pan_slider.setRange(-100, 100)
        self.pan_slider.setValue(int(track.pan * 100))
        self.pan_slider.setFixedWidth(52)
        self.pan_slider.setToolTip(tr("Pan (RMB — reset)"))
        self.pan_slider.valueChanged.connect(
            lambda v: self.pan_changed.emit(self.track.id, v / 100.0))
        mid.addWidget(self.pan_slider)

        self.width_knob = StereoWidthKnob(track.stereo_width)
        self.width_knob.width_changed.connect(
            lambda w: self.width_changed.emit(self.track.id, w))
        mid.addWidget(self.width_knob)
        layout.addLayout(mid)

        # Instrument selector (MIDI tracks only).
        if track.is_midi:
            bottom = QHBoxLayout()
            bottom.setSpacing(4)
            self.inst_label = QLabel(NO_INSTRUMENT_LABEL, self)
            self.inst_label.setObjectName("instLabel")
            self.inst_label.setStyleSheet(
                f"#instLabel {{ color: {theme.TEXT_DIM}; font-size: 10px;"
                " background: transparent; }}")
            self.inst_label.setToolTip(tr("VST instrument on this MIDI track"))
            bottom.addWidget(self.inst_label, 1)

            self.vst_select_btn = QPushButton(tr("Select\u2026"), self)
            self.vst_select_btn.setFixedHeight(20)
            self.vst_select_btn.setToolTip(tr("Choose a VST instrument"))
            self.vst_select_btn.clicked.connect(
                lambda: self.vst_plugin_requested.emit(self.track.id))
            bottom.addWidget(self.vst_select_btn)

            self.vst_editor_btn = QPushButton(tr("Editor"), self)
            self.vst_editor_btn.setFixedHeight(20)
            self.vst_editor_btn.setToolTip(tr("Open VST instrument editor"))
            self.vst_editor_btn.clicked.connect(
                lambda: self.vst_editor_requested.emit(self.track.id))
            self.vst_editor_btn.setVisible(track.instrument.uses_vst)
            bottom.addWidget(self.vst_editor_btn)
            layout.addLayout(bottom)
            self._sync_instrument_ui()

        self.meter = LevelMeter(self)
        layout.addWidget(self.meter)

        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    def set_folded(self, folded: bool) -> None:
        self._folded = folded
        if hasattr(self, "fold_btn"):
            self.fold_btn.setText("\u25b8" if folded else "\u25be")

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self._apply_frame_style()

    def set_drop_target(self, on: bool) -> None:
        """Highlight this (group) header as the drop target during a drag."""
        if self._drop_target != on:
            self._drop_target = on
            self._apply_frame_style()

    def set_accent_color(self, color: str) -> None:
        """Update the header accent (border, badge) after a color change."""
        self._accent = color
        self.track.color = color
        self.badge.setStyleSheet(
            f"color: {color}; background: transparent;"
            " font-size: 9px; font-weight: bold; letter-spacing: 1px;")
        self._apply_frame_style()

    def set_track_name(self, name: str) -> None:
        self.name_edit.setText(name)
        self.name_edit.setCursorPosition(0)

    def _apply_frame_style(self) -> None:
        # Children keep their own color on the (wider) strip: membership
        # in a group is shown by the indent, not by recoloring the track.
        if self._is_child:
            left_border = f"{GROUP_CHILD_INDENT}px solid {self._accent}"
        else:
            left_border = f"6px solid {self._accent}"
        if self._drop_target:
            bg = theme.HOVER
            right = f"2px solid {theme.AMBER}"
        elif self._selected:
            # Brighter than RAISED so the selected header clearly pops.
            bg = theme.HOVER
            right = f"3px solid {self._accent}"
        else:
            bg = theme.SURFACE
            right = "none"
        self.setStyleSheet(
            f"TrackHeader {{ background: {bg};"
            f" border: none;"
            f" border-left: {left_border};"
            f" border-right: {right};"
            f" border-bottom: 1px solid {theme.BG}; }}")

    # -- selection / drag with the left mouse button -----------------------

    def _is_grab_area(self, pos) -> bool:
        """True when the press lands on a non-interactive part of the header."""
        child = self.childAt(pos)
        if child is self.name_edit:
            return self.name_edit.isReadOnly()
        return child is None or isinstance(child, (QLabel, LevelMeter))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton and \
                self._is_grab_area(event.position().toPoint()):
            self._press_pos = event.globalPosition().toPoint()
            self._dragging = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._press_pos is not None and (event.buttons() & Qt.LeftButton):
            gp = event.globalPosition().toPoint()
            if not self._dragging and (gp - self._press_pos).manhattanLength() \
                    >= QApplication.startDragDistance():
                self._dragging = True
                self.drag_started.emit(self.track.id)
            if self._dragging:
                self.drag_moved.emit(self.track.id, gp)
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton and self._press_pos is not None:
            gp = event.globalPosition().toPoint()
            self._press_pos = None
            if self._dragging:
                self._dragging = False
                self.drag_dropped.emit(self.track.id, gp)
            else:
                self.clicked.emit(self.track.id)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # -- rename (Ctrl+R or Properties dialog) ------------------------------

    def begin_rename(self) -> None:
        self.name_edit.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.name_edit.setReadOnly(False)
        self.name_edit.setFocusPolicy(Qt.ClickFocus)
        self.name_edit.setFocus(Qt.MouseFocusReason)
        self.name_edit.selectAll()
        # Escape cancels the rename (see eventFilter).
        self.name_edit.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        if obj is self.name_edit and event.type() == QEvent.KeyPress \
                and event.key() == Qt.Key_Escape \
                and not self.name_edit.isReadOnly():
            self._cancel_rename()
            return True
        return super().eventFilter(obj, event)

    def _cancel_rename(self) -> None:
        """Escape: leave edit mode without applying the new name."""
        self.name_edit.setText(self.track.name)
        # ReadOnly first: the focus-out below fires editingFinished,
        # and _finish_rename must see the edit as already closed.
        self.name_edit.setReadOnly(True)
        self.name_edit.setFocusPolicy(Qt.NoFocus)
        self.name_edit.clearFocus()
        self.name_edit.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.name_edit.setCursorPosition(0)

    def _finish_rename(self) -> None:
        if self.name_edit.isReadOnly():
            return
        self.name_edit.setReadOnly(True)
        self.name_edit.setFocusPolicy(Qt.NoFocus)
        self.name_edit.clearFocus()
        self.name_edit.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.name_changed.emit(self.track.id, self.name_edit.text())

    def _show_context_menu(self, pos) -> None:
        menu = QMenu(self)
        if not self.track.is_group:
            groups = [t for t in self._project_tracks()
                      if t.is_group and t.id != self.track.id]
            if groups:
                submenu = menu.addMenu(tr("Move to group \u25b8"))
                for g in groups:
                    submenu.addAction(
                        g.name, lambda gid=g.id: self.move_to_group.emit(
                            self.track.id, gid))
            if self.track.group_id != 0:
                menu.addAction(
                    tr("Remove from group"),
                    lambda: self.remove_from_group.emit(self.track.id))
        auto_menu = menu.addMenu(tr("Add automation \u25b8"))
        auto_menu.addAction(tr("Volume"), lambda: self.add_automation.emit(
            self.track.id, "volume", -1, 0, tr("Volume")))
        auto_menu.addAction(tr("Pan"), lambda: self.add_automation.emit(
            self.track.id, "pan", -1, 0, tr("Pan")))
        auto_menu.addAction(tr("Stereo width"), lambda: self.add_automation.emit(
            self.track.id, "width", -1, 0, tr("Stereo width")))
        for slot, fx in enumerate(self.track.effects):
            if fx.type == "vst":
                label = fx.display_name or "VST"
                auto_menu.addAction(
                    trf("{label}: parameters\u2026", label=label),
                    lambda s=slot: self.add_automation_vst_pick.emit(
                        self.track.id, s))
            elif fx.type in EFFECT_DEFS:
                fx_name, params = EFFECT_DEFS[fx.type]
                for pi, (plabel, *_rest) in enumerate(params):
                    auto_menu.addAction(
                        f"{fx_name}: {plabel}",
                        lambda s=slot, p=pi, n=f"{fx_name}: {plabel}":
                        self.add_automation.emit(
                            self.track.id, "fx", s, p, n))
        if self.track.is_midi and self.track.instrument.uses_vst:
            inst_name = self.track.instrument.vst_name or tr("Instrument")
            auto_menu.addAction(
                trf("{name}: parameters\u2026", name=inst_name),
                lambda: self.add_automation_vst_pick.emit(self.track.id, -1))
        menu.addAction(
            tr("Rename\tCtrl+Shift+R"),
            lambda: self.rename_requested.emit(self.track.id))
        menu.addAction(
            tr("Properties\u2026"),
            lambda: self.properties_requested.emit(self.track.id))
        if not menu.isEmpty():
            menu.exec(self.mapToGlobal(pos))

    def _project_tracks(self) -> list[Track]:
        panel = self.parent()
        while panel is not None and not isinstance(panel, TrackPanel):
            panel = panel.parent()
        if isinstance(panel, TrackPanel):
            return panel.project.tracks
        return []

    def _sync_instrument_ui(self) -> None:
        if not hasattr(self, "inst_label"):
            return
        inst = self.track.instrument
        if inst.uses_vst:
            self.inst_label.setText(inst.vst_name or inst.vst_path or "VST")
            self.inst_label.setStyleSheet(
                f"color: {theme.TEXT}; font-size: 10px;"
                " background: transparent;")
            self.vst_editor_btn.setVisible(True)
        else:
            self.inst_label.setText(NO_INSTRUMENT_LABEL)
            self.inst_label.setStyleSheet(
                f"color: {theme.TEXT_DIM}; font-size: 10px;"
                " background: transparent;")
            self.vst_editor_btn.setVisible(False)

    def retranslate(self) -> None:
        """Refresh labels after a live language switch."""
        if hasattr(self, "fold_btn"):
            self.fold_btn.setToolTip(tr("Fold/unfold child tracks"))
        self.name_edit.setToolTip(tr("Ctrl+Shift+R — rename"))
        if hasattr(self, "auto_fold_btn"):
            self.auto_fold_btn.setToolTip(tr("Fold/unfold automation lanes"))
        self.mute_btn.setToolTip(tr("Mute"))
        self.solo_btn.setToolTip(tr("Solo"))
        self.gain_slider.setToolTip(tr("Volume (RMB — reset)"))
        self.pan_slider.setToolTip(tr("Pan (RMB — reset)"))
        if hasattr(self.pan_slider, "_default_label"):
            self.pan_slider._default_label = tr("center")
        if hasattr(self, "inst_label"):
            self.inst_label.setToolTip(tr("VST instrument on this MIDI track"))
            self.vst_select_btn.setText(tr("Select\u2026"))
            self.vst_select_btn.setToolTip(tr("Choose a VST instrument"))
            self.vst_editor_btn.setText(tr("Editor"))
            self.vst_editor_btn.setToolTip(tr("Open VST instrument editor"))

    @staticmethod
    def _dim_label(text: str) -> QLabel:
        return QLabel(text, styleSheet=f"color: {theme.TEXT_DIM};"
                      " font-size: 10px; background: transparent;")

    @staticmethod
    def _small_button(text: str, color: str, tip: str = "") -> QPushButton:
        btn = QPushButton(text)
        btn.setCheckable(True)
        btn.setFixedSize(18, 19)
        if tip:
            btn.setToolTip(tip)
        btn.setStyleSheet(
            f"QPushButton {{ background: {theme.RAISED};"
            f" color: {theme.TEXT_DIM}; border: none; border-radius: 3px;"
            f" font-size: 10px; font-weight: bold; padding: 0; }}"
            f"QPushButton:hover {{ background: {theme.HOVER};"
            f" color: {theme.TEXT}; }}"
            f"QPushButton:checked {{ background: {color}; color: white; }}")
        return btn


class AutomationLaneHeader(QFrame):
    """Slim header aligned with an automation sub-lane on the timeline."""

    remove_requested = Signal(int, int)  # track_id, lane_id
    clicked = Signal(int, int)           # track_id, lane_id (toggle height)

    def __init__(self, track_id: int, lane, *, selected: bool = False,
                 parent=None) -> None:
        super().__init__(parent)
        self.track_id = track_id
        self.lane = lane
        self._selected = selected
        self.setFixedHeight(AUTO_LANE_HEIGHT_SELECTED if selected
                            else AUTO_LANE_HEIGHT)
        self._apply_style()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(GROUP_CHILD_INDENT + 4, 2, 6, 2)
        layout.setSpacing(4)

        marker = QLabel("\u25be", self)
        marker.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; font-size: 10px;"
            " background: transparent;")
        layout.addWidget(marker)

        auto_lbl = QLabel("AUTO", self)
        auto_lbl.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; font-size: 9px; font-weight: bold;"
            " background: transparent; letter-spacing: 1px;")
        layout.addWidget(auto_lbl)

        name = lane.name or lane.target
        self.name_label = QLabel(name, self)
        self.name_label.setObjectName("autoLaneName")
        self.name_label.setStyleSheet(
            f"#autoLaneName {{ color: {theme.TEXT_DIM}; font-size: 10px;"
            " background: transparent; }}")
        self.name_label.setToolTip(name)
        layout.addWidget(self.name_label, 1)

        self.close_btn = QPushButton("\u00d7", self)
        self.close_btn.setFixedSize(18, 16)
        self.close_btn.setToolTip(tr("Remove automation"))
        self.close_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {theme.TEXT_FAINT};"
            f" border: none; font-size: 12px; padding: 0; }}"
            f"QPushButton:hover {{ color: {theme.RED}; }}")
        self.close_btn.clicked.connect(
            lambda: self.remove_requested.emit(track_id, lane.id))
        layout.addWidget(self.close_btn)

    def _apply_style(self) -> None:
        accent = theme.ACCENT if self._selected else theme.TEXT_FAINT
        bg = theme.RAISED if self._selected else theme.LANE_AUTO
        self.setStyleSheet(
            f"AutomationLaneHeader {{ background: {bg};"
            f" border: none;"
            f" border-left: 3px solid {accent};"
            f" border-bottom: 1px solid {theme.BG}; }}")

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.track_id, self.lane.id)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


    def retranslate(self) -> None:
        self.close_btn.setToolTip(tr("Remove automation"))


class VstParamPickerDialog(QDialog):
    """Pick one VST parameter from a filterable list."""

    def __init__(self, params: list, title: str = "VST parameter",
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self._params = list(enumerate(params))
        self._selected: tuple[int, str] | None = None

        layout = QVBoxLayout(self)
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter\u2026")
        self._filter.textChanged.connect(self._apply_filter)
        layout.addWidget(self._filter)

        self._list = QListWidget()
        layout.addWidget(self._list, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._populate(self._params)

    def _populate(self, params: list) -> None:
        # Items are (position, (param_id, name, value)); automation lanes
        # address VST parameters by LIST POSITION (the engine maps position
        # -> ParamID), not by the plugin's own ParamID.
        self._list.clear()
        for pos, (_param_id, name, _value) in params:
            self._list.addItem(f"{name}  (#{pos})")
            row = self._list.count() - 1
            self._list.item(row).setData(Qt.UserRole, (pos, name))

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        if not needle:
            self._populate(self._params)
            return
        filtered = [p for p in self._params if needle in p[1][1].lower()]
        self._populate(filtered)

    def _on_accept(self) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        self._selected = item.data(Qt.UserRole)
        self.accept()

    def selected(self) -> tuple[int, str] | None:
        return self._selected


class MasterTrackHeader(QFrame):
    """Pinned master strip at the top of the track column.

    The master cannot be moved or deleted; clicking it only selects it
    (to show its FX chain in the FX panel).
    """

    gain_changed = Signal(float)
    clicked = Signal()
    auto_fold_toggled = Signal()  # collapse/expand master automation lanes
    # Global position for the master context menu (built by MainWindow,
    # which knows the master FX chain).
    context_menu_requested = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(64)
        self._selected = False
        self._apply_style()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(3)

        top = QHBoxLayout()
        top.setSpacing(4)

        badge = QLabel("MASTER", self)
        badge.setStyleSheet(
            f"color: {theme.RED}; background: transparent;"
            " font-size: 9px; font-weight: bold; letter-spacing: 1px;")
        top.addWidget(badge, 1)

        self.auto_fold_btn = QPushButton("\u25beA", self)
        self.auto_fold_btn.setFixedSize(22, 19)
        self.auto_fold_btn.setToolTip(tr("Fold/unfold automation lanes"))
        self.auto_fold_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.RAISED};"
            f" color: {theme.TEXT_DIM}; border: none; border-radius: 3px;"
            f" font-size: 9px; padding: 0; }}"
            f"QPushButton:hover {{ background: {theme.HOVER};"
            f" color: {theme.TEXT}; }}")
        self.auto_fold_btn.clicked.connect(self.auto_fold_toggled.emit)
        self.auto_fold_btn.setVisible(False)
        top.addWidget(self.auto_fold_btn)
        layout.addLayout(top)

        mid = QHBoxLayout()
        mid.setSpacing(5)
        mid.addWidget(TrackHeader._dim_label(tr("Vol")))
        self.gain_slider = ResetSlider(Qt.Horizontal, 100, "0 dB")
        self.gain_slider.setRange(0, 150)
        self.gain_slider.setValue(100)
        self.gain_slider.setToolTip(tr("Master volume (RMB — reset)"))
        self.gain_slider.valueChanged.connect(
            lambda v: self.gain_changed.emit(v / 100.0))
        mid.addWidget(self.gain_slider, 1)
        layout.addLayout(mid)

        self.meter = LevelMeter(self)
        layout.addWidget(self.meter)

    def _apply_style(self) -> None:
        bg = theme.RAISED if self._selected else theme.SURFACE
        right = f"2px solid {theme.RED}" if self._selected else "none"
        self.setStyleSheet(
            f"MasterTrackHeader {{ background: {bg};"
            f" border: none;"
            f" border-left: 3px solid {theme.RED};"
            f" border-right: {right};"
            f" border-bottom: 1px solid {theme.BG}; }}")

    def set_selected(self, selected: bool) -> None:
        if self._selected != selected:
            self._selected = selected
            self._apply_style()

    def set_auto_fold(self, visible: bool, folded: bool) -> None:
        """Show the lane-fold button only when master has automation."""
        self.auto_fold_btn.setVisible(visible)
        self.auto_fold_btn.setText("\u25b8A" if folded else "\u25beA")

    def retranslate(self) -> None:
        self.auto_fold_btn.setToolTip(tr("Fold/unfold automation lanes"))
        self.gain_slider.setToolTip(tr("Master volume (RMB — reset)"))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton and \
                not isinstance(self.childAt(event.position().toPoint()),
                               QSlider):
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def contextMenuEvent(self, event) -> None:
        if isinstance(self.childAt(event.pos()), QSlider):
            super().contextMenuEvent(event)
            return
        self.context_menu_requested.emit(event.globalPos())
        event.accept()


class _InsertLine(QWidget):
    """Horizontal drop indicator shown while reordering tracks."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(2)
        self.hide()

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(theme.ACCENT))
        p.end()


class TrackPanel(QScrollArea):
    """Vertical list of track headers, kept in sync with the timeline rows."""

    gain_changed = Signal(int, float)
    pan_changed = Signal(int, float)
    width_changed = Signal(int, float)
    mute_toggled = Signal(int, bool)
    solo_toggled = Signal(int, bool)
    name_changed = Signal(int, str)
    remove_requested = Signal(int)
    vst_plugin_requested = Signal(int)
    vst_editor_requested = Signal(int)
    track_clicked = Signal(int)
    add_track_requested = Signal()
    add_midi_track_requested = Signal()
    add_group_track_requested = Signal()
    track_reordered = Signal(int, int)   # track_id, new_index in project.tracks
    group_fold_toggled = Signal(int)       # group track_id
    move_to_group = Signal(int, int)
    remove_from_group = Signal(int)
    add_automation = Signal(int, str, int, int, str)
    add_automation_vst_pick = Signal(int, int)
    auto_fold_toggled = Signal(int)  # track_id (master = MASTER_FX_ID)
    properties_requested = Signal(int)
    rename_requested = Signal(int)
    automation_remove_requested = Signal(int, int)
    automation_lane_clicked = Signal(int, int)  # track_id, lane_id
    scrolled = Signal(int)  # user-driven scroll (wheel/scrollbar)
    delete_selected_requested = Signal(object)  # list[int]

    def __init__(self, project: Project, track_height: int, parent=None) -> None:
        super().__init__(parent)
        self.project = project
        self.track_height = track_height
        # Proxy track for the master bus (id = -1); set by MainWindow so
        # master automation lanes get header rows too.
        self.master_track: Track | None = None
        # The master strip widget itself is created (and wired) by
        # MainWindow; the panel hosts it as the row after the tracks.
        self.master_header: MasterTrackHeader | None = None
        self.headers: dict[int, TrackHeader] = {}
        self.auto_headers: dict[tuple[int, int], AutomationLaneHeader] = {}
        self._folded_groups: set[int] = set()
        self._folded_auto: set[int] = set()
        self._selected_track_id: int | None = None
        self._selected_track_ids: set[int] = set()
        self._selected_lane: tuple[int, int] | None = None
        self._drag_track_id: int | None = None
        self._drop_index: int | None = None
        self._drop_group_id: int | None = None
        self._container: QWidget | None = None
        self._syncing_scroll = False

        self.setWidgetResizable(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFixedWidth(230)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setStyleSheet(
            f"QScrollArea {{ background: {theme.PANEL}; border: none;"
            f" border-right: 1px solid {theme.BORDER_SOFT}; }}"
            f"{theme.thin_scrollbar_qss(width=6)}")

        self._container = QWidget()
        self._container.setStyleSheet(f"background: {theme.PANEL};")
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(0, 30, 0, 0)  # ruler offset
        self._layout.setSpacing(0)

        self._insert_line = _InsertLine(self._container)
        self._insert_line.setFixedWidth(self.width())
        self._insert_line.raise_()

        # Guaranteed empty strip below the last track row: keeps space for
        # the right-click "add track" menu and matches the timeline's bottom
        # padding so both scroll the same distance.
        self._bottom_pad = QWidget()
        self._bottom_pad.setFixedHeight(PANEL_BOTTOM_PAD)
        self._bottom_pad.setStyleSheet("background: transparent;")

        # Spacer separating the master strip from the ordinary tracks below
        # (same height as the timeline's MASTER_GAP).
        self._master_gap = QWidget()
        self._master_gap.setFixedHeight(MASTER_GAP)
        self._master_gap.setStyleSheet("background: transparent;")
        self._master_gap.hide()

        self._layout.addStretch(1)
        self.verticalScrollBar().valueChanged.connect(self._on_own_scroll)
        self.setWidget(self._container)
        self._container.setContextMenuPolicy(Qt.CustomContextMenu)
        self._container.customContextMenuRequested.connect(
            self._on_container_context_menu)
        self.rebuild()

    def build_add_track_menu(self, parent=None) -> QMenu:
        """Context menu for creating audio, MIDI and group tracks."""
        menu = QMenu(parent or self)
        menu.addAction(tr("Add audio track (Ctrl+T)"),
                       self.add_track_requested.emit)
        menu.addAction(tr("Add MIDI track (Ctrl+Shift+T)"),
                       self.add_midi_track_requested.emit)
        menu.addAction(tr("Add group track (Ctrl+Shift+G)"),
                       self.add_group_track_requested.emit)
        return menu

    def show_add_track_context_menu(self, global_pos) -> None:
        """Show the add-track menu (also used from tests)."""
        self.build_add_track_menu().exec(global_pos)

    def _on_container_context_menu(self, pos) -> None:
        widget = self._container.childAt(pos)
        while widget is not None:
            if isinstance(widget, TrackHeader):
                return
            widget = widget.parentWidget()
        self.show_add_track_context_menu(self._container.mapToGlobal(pos))

    def set_master_header(self, header: MasterTrackHeader) -> None:
        """Adopt the master strip built by MainWindow as a panel row."""
        self.master_header = header
        header.auto_fold_toggled.connect(self._on_master_auto_fold)
        self.rebuild()

    def _on_master_auto_fold(self) -> None:
        if self.master_track is not None:
            self.auto_fold_toggled.emit(self.master_track.id)

    def set_folded_groups(self, folded: set[int]) -> None:
        self._folded_groups = set(folded)
        self.rebuild()

    def set_folded_auto(self, folded: set[int]) -> None:
        self._folded_auto = set(folded)
        self.rebuild()

    def visible_tracks(self) -> list[Track]:
        return [t for t in self.project.tracks
                if not (t.group_id != 0 and t.group_id in self._folded_groups)]

    def rebuild(self) -> None:
        for header in self.headers.values():
            self._layout.removeWidget(header)
            header.deleteLater()
        for header in self.auto_headers.values():
            self._layout.removeWidget(header)
            header.deleteLater()
        self.headers.clear()
        self.auto_headers.clear()
        self._layout.removeWidget(self._bottom_pad)
        self._layout.removeWidget(self._master_gap)
        if self.master_header is not None:
            self._layout.removeWidget(self.master_header)
        insert_at = 0
        if self.master_header is not None:
            self._layout.insertWidget(insert_at, self.master_header)
            insert_at += 1
        if self.master_track is not None:
            master_folded = self.master_track.id in self._folded_auto
            if self.master_header is not None:
                self.master_header.set_auto_fold(
                    bool(self.master_track.automation_lanes), master_folded)
            if not master_folded:
                for lane in self.master_track.automation_lanes:
                    key = (self.master_track.id, lane.id)
                    ah = AutomationLaneHeader(
                        self.master_track.id, lane,
                        selected=self._selected_lane == key,
                        parent=self._container)
                    ah.remove_requested.connect(
                        self.automation_remove_requested.emit)
                    ah.clicked.connect(self.automation_lane_clicked.emit)
                    self._layout.insertWidget(insert_at, ah)
                    insert_at += 1
                    self.auto_headers[key] = ah
        if self.master_header is not None:
            self._layout.insertWidget(insert_at, self._master_gap)
            self._master_gap.show()
            insert_at += 1
        for track in self.visible_tracks():
            is_child = track.group_id != 0
            folded = track.is_group and track.id in self._folded_groups
            # Parent at construction: a parentless widget polished before
            # insertWidget can flash as a tiny top-level window on Windows.
            header = TrackHeader(track, self.track_height,
                                 is_child=is_child, folded=folded,
                                 auto_folded=track.id in self._folded_auto,
                                 parent=self._container)
            header.drag_started.connect(self._start_drag)
            header.drag_moved.connect(self._on_drag_moved)
            header.drag_dropped.connect(self._on_drag_dropped)
            header.gain_changed.connect(self.gain_changed.emit)
            header.pan_changed.connect(self.pan_changed.emit)
            header.width_changed.connect(self.width_changed.emit)
            header.mute_toggled.connect(self.mute_toggled.emit)
            header.solo_toggled.connect(self.solo_toggled.emit)
            header.name_changed.connect(self.name_changed.emit)
            header.remove_requested.connect(self.remove_requested.emit)
            header.vst_plugin_requested.connect(self.vst_plugin_requested.emit)
            header.vst_editor_requested.connect(self.vst_editor_requested.emit)
            header.clicked.connect(self._on_header_clicked)
            header.fold_toggled.connect(self.group_fold_toggled.emit)
            header.move_to_group.connect(self.move_to_group.emit)
            header.remove_from_group.connect(self.remove_from_group.emit)
            header.add_automation.connect(self.add_automation.emit)
            header.add_automation_vst_pick.connect(
                self.add_automation_vst_pick.emit)
            header.auto_fold_toggled.connect(self.auto_fold_toggled.emit)
            header.properties_requested.connect(self.properties_requested.emit)
            header.rename_requested.connect(self.rename_requested.emit)
            self._layout.insertWidget(insert_at, header)
            insert_at += 1
            header.set_selected(track.id in self._selected_track_ids)
            self.headers[track.id] = header

            if track.id in self._folded_auto:
                continue
            for lane in track.automation_lanes:
                selected = self._selected_lane == (track.id, lane.id)
                ah = AutomationLaneHeader(track.id, lane, selected=selected,
                                          parent=self._container)
                ah.remove_requested.connect(
                    self.automation_remove_requested.emit)
                ah.clicked.connect(self.automation_lane_clicked.emit)
                self._layout.insertWidget(insert_at, ah)
                insert_at += 1
                self.auto_headers[(track.id, lane.id)] = ah
        self._layout.insertWidget(insert_at, self._bottom_pad)
        # Apply geometry immediately: scroll sync with the timeline reads
        # widget positions right after a rebuild.
        self._layout.activate()

    def set_extra_bottom_pad(self, extra: int) -> None:
        """Grow the bottom pad so the panel scroll range exactly matches
        the timeline's (whose viewport is shorter by the height of its
        horizontal scrollbar)."""
        self._bottom_pad.setFixedHeight(PANEL_BOTTOM_PAD + max(0, extra))

    def retranslate(self) -> None:
        """Refresh track header labels after a live language switch."""
        if self.master_header is not None:
            self.master_header.retranslate()
        for header in self.headers.values():
            header.retranslate()
        for header in self.auto_headers.values():
            header.retranslate()

    def set_selected_track(self, track_id: int | None) -> None:
        self._selected_track_id = track_id
        self._selected_track_ids = {track_id} if track_id is not None else set()
        self.setFocus(Qt.MouseFocusReason)
        for tid, header in self.headers.items():
            header.set_selected(tid in self._selected_track_ids)

    def set_selected_tracks(self, track_ids) -> None:
        ids = {int(tid) for tid in track_ids
               if self.project.track_by_id(int(tid)) is not None}
        self._selected_track_ids = ids
        self._selected_track_id = next(iter(ids), None)
        self.setFocus(Qt.MouseFocusReason)
        for tid, header in self.headers.items():
            header.set_selected(tid in ids)

    def selected_track_ids(self) -> list[int]:
        order = {t.id: i for i, t in enumerate(self.project.tracks)}
        return sorted(self._selected_track_ids, key=lambda tid: order.get(tid, 0))

    def _on_header_clicked(self, track_id: int) -> None:
        mods = QApplication.keyboardModifiers()
        if mods & Qt.ControlModifier:
            if track_id in self._selected_track_ids:
                self._selected_track_ids.discard(track_id)
            else:
                self._selected_track_ids.add(track_id)
            if not self._selected_track_ids:
                self._selected_track_ids.add(track_id)
        elif mods & Qt.ShiftModifier and self._selected_track_ids:
            visible = [t.id for t in self.visible_tracks()]
            anchors = [visible.index(tid) for tid in self._selected_track_ids
                       if tid in visible]
            if anchors and track_id in visible:
                a = anchors[-1]
                b = visible.index(track_id)
                lo, hi = sorted((a, b))
                self._selected_track_ids |= set(visible[lo:hi + 1])
        else:
            self._selected_track_ids = {track_id}
        self._selected_track_id = track_id
        self.setFocus(Qt.MouseFocusReason)
        for tid, header in self.headers.items():
            header.set_selected(tid in self._selected_track_ids)
        self.track_clicked.emit(track_id)

    def selected_lane(self) -> tuple[int, int] | None:
        return self._selected_lane

    def set_selected_lane(self, key: tuple[int, int] | None) -> None:
        if self._selected_lane == key:
            return
        self._selected_lane = key
        self.rebuild()

    def _header_widgets(self) -> list[TrackHeader]:
        return [self._layout.itemAt(i).widget()
                for i in range(self._layout.count())
                if isinstance(self._layout.itemAt(i).widget(), TrackHeader)]

    def _index_at_y(self, y: float) -> int:
        """Map a y coordinate to a project.tracks insertion index."""
        headers = self._header_widgets()
        if not headers:
            return 0
        for i, header in enumerate(headers):
            top = header.y()
            mid = top + header.height() / 2
            if y < mid:
                visible = self.visible_tracks()
                return self.project.tracks.index(visible[i])
        visible = self.visible_tracks()
        last = visible[-1]
        return self.project.tracks.index(last) + 1

    def _show_insert_line(self, index: int | None) -> None:
        if index is None:
            self._insert_line.hide()
            return
        headers = self._header_widgets()
        if not headers:
            self._insert_line.hide()
            return
        visible = self.visible_tracks()
        try:
            vis_idx = next(i for i, t in enumerate(visible)
                           if self.project.tracks.index(t) >= index)
        except StopIteration:
            vis_idx = len(headers)
        if vis_idx >= len(headers):
            y = headers[-1].y() + headers[-1].height()
        elif self.project.tracks.index(visible[vis_idx]) == index:
            y = headers[vis_idx].y()
        else:
            y = headers[vis_idx - 1].y() + headers[vis_idx - 1].height() \
                if vis_idx > 0 else 0
        self._insert_line.setGeometry(0, int(y), self._container.width(), 2)
        self._insert_line.show()
        self._insert_line.raise_()

    def _start_drag(self, track_id: int) -> None:
        self._drag_track_id = track_id
        self._drop_index = None
        self._drop_group_id = None
        self.setCursor(Qt.SizeVerCursor)

    def _header_group_at(self, track_id: int, local_pos) -> int | None:
        """Group id when hovering over a group header (drop-into target)."""
        track = next((t for t in self.project.tracks if t.id == track_id),
                     None)
        if track is None or track.is_group:
            return None  # groups cannot be nested
        for tid, header in self.headers.items():
            if not header.track.is_group or tid == track.group_id:
                continue
            geo = header.geometry()
            # Middle band of the header counts as "into the group";
            # edges keep working as reorder positions.
            band = geo.adjusted(0, geo.height() // 4, 0, -geo.height() // 4)
            if band.contains(local_pos):
                return tid
        return None

    def _set_drop_group(self, group_id: int | None) -> None:
        self._drop_group_id = group_id
        for tid, header in self.headers.items():
            header.set_drop_target(tid == group_id)

    def _on_drag_moved(self, track_id: int, global_pos: QPoint) -> None:
        if self._drag_track_id != track_id:
            return
        local = self._container.mapFromGlobal(global_pos)
        group_id = self._header_group_at(track_id, local)
        if group_id is not None:
            self._set_drop_group(group_id)
            self._drop_index = None
            self._insert_line.hide()
        else:
            self._set_drop_group(None)
            self._drop_index = self._index_at_y(local.y())
            self._show_insert_line(self._drop_index)

    def _on_drag_dropped(self, track_id: int, global_pos: QPoint) -> None:
        if self._drag_track_id != track_id:
            return
        new_index = self._drop_index
        group_id = self._drop_group_id
        self._drag_track_id = None
        self._drop_index = None
        self._set_drop_group(None)
        self._insert_line.hide()
        self.unsetCursor()
        if group_id is not None:
            self.move_to_group.emit(track_id, group_id)
            return
        if new_index is not None:
            old_index = next(
                i for i, t in enumerate(self.project.tracks)
                if t.id == track_id)
            if new_index != old_index and new_index != old_index + 1:
                self.track_reordered.emit(track_id, new_index)

    def sync_scroll(self, value: int) -> None:
        # Driven by the timeline: don't echo the (possibly clamped) value
        # back, otherwise the two scrollbars fight near the bottom edge.
        self._syncing_scroll = True
        try:
            self.verticalScrollBar().setValue(value)
        finally:
            self._syncing_scroll = False

    def _on_own_scroll(self, value: int) -> None:
        if not self._syncing_scroll:
            self.scrolled.emit(value)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Delete and self._selected_track_ids:
            self.delete_selected_requested.emit(self.selected_track_ids())
            event.accept()
            return
        super().keyPressEvent(event)

    def update_meters(self, bridge) -> None:
        for track_id, header in self.headers.items():
            l, r = bridge.track_peaks(track_id)
            header.meter.set_levels(l, r)

    def reset_meters(self) -> None:
        for header in self.headers.values():
            header.meter.reset()
