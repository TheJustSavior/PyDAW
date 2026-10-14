"""Piano roll editor for one MIDI clip.

Editing:
  - LMB on empty space        create a note (length memory from last edit)
  - LMB drag on empty space   paint notes along the snap grid (same row)
  - drag a note               move it (pitch + time); moves the whole selection
  - drag its right edge       resize (whole selection)
  - Ctrl+drag note/edge       move/resize detached from the grid
  - 1 / 2                     mouse tool / selection tool
  - C                         toggle the slice tool (click cuts a note)
  - RMB on a note             delete it
  - Select tool / Ctrl+drag   rubber-band selection (Shift+click adds)
  - Arrow keys                nudge time/pitch; Ctrl+Up/Down = octave
  - Q                         quantize note starts to snap grid
  - Delete                    delete selected notes
  - Ctrl+A / C / V / D        select all / copy / paste at playhead / duplicate
  - velocity lane at the bottom: drag over the bars to set note velocity

Navigation: wheel = vertical scroll, Shift+wheel = horizontal,
Ctrl+wheel = zoom, middle-drag = pan.

The toolbar hosts a local snap toggle + resolution, the slice tool and
Help -> hotkeys reference. The snap resolution also controls grid density.

All edits are emitted as signals; MainWindow wraps them in undo commands
and mirrors them into the engine.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import (QColor, QKeySequence, QPainter, QPen, QFontMetrics,
                           QShortcut)
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox,
                               QGraphicsScene, QGraphicsView, QHBoxLayout,
                               QHeaderView, QMenu, QPushButton, QTableWidget,
                               QTableWidgetItem, QToolTip, QVBoxLayout,
                               QApplication, QWidget)

from ..model.project import MidiClip, MidiNote, Track
from . import theme
from .i18n import tr, trf
from .keys import letter_key
from .tool_icons import icon_size, mouse_icon, select_icon

KEY_WIDTH = 56
NOTE_H = 14          # px per semitone row
MIN_PITCH = 24       # C1
MAX_PITCH = 108      # C8
PITCH_COUNT = MAX_PITCH - MIN_PITCH + 1
VELOCITY_LANE_H = 64
DEFAULT_SCROLL_PITCH = 72   # C5 when clip is empty

BG = theme.COL_BG
ROW_WHITE = theme.COL_PIANO_ROW_WHITE
ROW_BLACK = theme.COL_PIANO_ROW_BLACK
BAR_ALT = QColor(theme.COL_PIANO_BAR_ALT)
BAR_ALT.setAlpha(90)  # translucent: key rows stay visible in alternate bars
GRID_BEAT = QColor(theme.PIANO_GRID_BEAT)
GRID_BAR = QColor(theme.PIANO_GRID_BAR)
NOTE_COLOR = theme.COL_NOTE_MIDI
NOTE_BORDER = theme.COL_NOTE_MIDI_BORDER
NOTE_SELECTED = theme.COL_NOTE_SELECTED
NOTE_SELECTED_BORDER = theme.COL_NOTE_SELECTED_BORDER
RUBBER_FILL = QColor(theme.COL_ACCENT)
RUBBER_FILL.setAlpha(0)
RUBBER_EDGE = QColor(theme.COL_ACCENT)
RUBBER_EDGE.setAlpha(180)
CLIP_END_COLOR = QColor(theme.AMBER)
GHOST_OVERLAY = QColor(theme.PIANO_GHOST)
GHOST_OVERLAY.setAlpha(120)  # dim, but keep rows/grid visible beyond the clip
RESIZE_EDGE_PX = 5
MIN_NOTE_LEN = 0.05
GRID_STEPS = (4.0, 1.0, 0.5, 0.25, 0.125, 0.0625, 0.03125, 0.015625)
GRID_MIN_PX = 9.0

SNAP_RESOLUTIONS: list[tuple[str, float]] = [
    ("1 bar", 4.0),
    ("1 beat", 1.0),
    ("1/2", 0.5),
    ("1/4", 0.25),
    ("1/8", 0.125),
    ("1/16", 0.0625),
]

PIANO_ROLL_SHORTCUTS: list[tuple[str, str]] = [
    ("Create / paint notes", "LMB"),
    ("Resize (right edge)", "drag edge"),
    ("Move/resize without snap", "Ctrl + drag note/edge"),
    ("Delete note", "RMB"),
    ("Mouse tool", "1"),
    ("Selection tool", "2"),
    ("Rubber-band select", "Select tool / Ctrl + drag"),
    ("Add to selection", "Shift + click"),
    ("Nudge / transpose", "Arrows"),
    ("Octave", "Ctrl + Up/Down"),
    ("Quantize", "Q"),
    ("Select all / copy / paste / duplicate", "Ctrl+A / C / V / D"),
    ("Delete selected", "Delete"),
    ("Slice tool", "C"),
    ("Exit Slice", "Esc"),
    ("Zoom", "Ctrl + wheel"),
    ("Horizontal scroll", "Shift + wheel"),
    ("Panorama", "MMB drag"),
    ("Velocity", "drag in lane below"),
]

BLACK_KEYS = {1, 3, 6, 8, 10}
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F",
              "F#", "G", "G#", "A", "A#", "B"]

# Shared clipboard between piano roll windows:
# list of (rel_start_beat, pitch, length_beats, velocity).
_clipboard: list[tuple[float, int, float, float]] = []


def pitch_name(pitch: int) -> str:
    return f"{NOTE_NAMES[pitch % 12]}{pitch // 12 - 1}"


def _closest_snap_index(beats: float) -> int:
    values = [v for _label, v in SNAP_RESOLUTIONS]
    return min(range(len(values)), key=lambda i: abs(values[i] - beats))


def _adaptive_grid_step(px_per_beat: float) -> float:
    step = GRID_STEPS[0]
    for candidate in GRID_STEPS:
        if candidate * px_per_beat >= GRID_MIN_PX:
            step = candidate
    return step


class PianoRollHelpDialog(QDialog):
    """Modal hotkey reference for the piano roll window."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Piano Roll shortcuts"))
        self.resize(520, 480)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)

        self._table = QTableWidget()
        self._table.setColumnCount(2)
        self._table.setHorizontalHeaderLabels([tr("Action"), tr("Shortcut")])
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.NoSelection)
        self._table.setFocusPolicy(Qt.NoFocus)
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(True)
        self._fill_table()
        layout.addWidget(self._table)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def _fill_table(self) -> None:
        self._table.setRowCount(len(PIANO_ROLL_SHORTCUTS))
        for row, (action, shortcut) in enumerate(PIANO_ROLL_SHORTCUTS):
            action_item = QTableWidgetItem(tr(action))
            shortcut_item = QTableWidgetItem(tr(shortcut))
            action_item.setForeground(theme.COL_TEXT)
            shortcut_item.setForeground(theme.COL_ACCENT)
            self._table.setItem(row, 0, action_item)
            self._table.setItem(row, 1, shortcut_item)

    def retranslate(self) -> None:
        self.setWindowTitle(tr("Piano Roll shortcuts"))
        self._table.setHorizontalHeaderLabels([tr("Action"), tr("Shortcut")])
        self._fill_table()


class KeyboardWidget(QWidget):
    """Vertical piano keyboard; press = instrument preview."""

    note_on = Signal(int)
    note_off = Signal(int)

    def __init__(self, view: "PianoRollView", parent=None) -> None:
        super().__init__(parent)
        self.view = view
        self.setFixedWidth(KEY_WIDTH)
        self._pressed = -1

    def _pitch_at(self, y: float) -> int:
        scene_y = y + self.view.verticalScrollBar().value()
        pitch = MAX_PITCH - int(scene_y // NOTE_H)
        return max(MIN_PITCH, min(MAX_PITCH, pitch))

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._pressed = self._pitch_at(event.position().y())
            self.note_on.emit(self._pressed)
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self._pressed >= 0:
            self.note_off.emit(self._pressed)
            self._pressed = -1
            self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        offset = self.view.verticalScrollBar().value()
        p.translate(0, -offset)
        for pitch in range(MIN_PITCH, MAX_PITCH + 1):
            y = (MAX_PITCH - pitch) * NOTE_H
            black = pitch % 12 in BLACK_KEYS
            if pitch == self._pressed:
                color = QColor(theme.ACCENT_MIDI)
            elif black:
                color = QColor(38, 41, 48)
            else:
                color = QColor(228, 231, 236)
            p.fillRect(0, y, KEY_WIDTH, NOTE_H, color)
            p.setPen(QPen(QColor(18, 20, 24)))
            p.drawLine(0, y, KEY_WIDTH, y)
            if pitch % 12 == 0:
                p.setPen(QPen(QColor(90, 95, 104) if not black
                              else QColor(180, 185, 192)))
                p.drawText(QRectF(0, y, KEY_WIDTH - 6, NOTE_H),
                           Qt.AlignRight | Qt.AlignVCenter, pitch_name(pitch))
        p.end()


class VelocityLane(QWidget):
    """Bar per note; drag over the bars to paint velocities."""

    # list of (note_id, old_velocity, new_velocity)
    velocities_change_requested = Signal(object)

    def __init__(self, view: "PianoRollView", parent=None) -> None:
        super().__init__(parent)
        self.view = view
        self.setFixedHeight(VELOCITY_LANE_H)
        self._drag_changes: dict[int, tuple[float, float]] = {}
        self._dragging = False
        self._hover_note: MidiNote | None = None
        self.setMouseTracking(True)

    def _bar_rect(self, note: MidiNote) -> QRectF:
        offset = self.view.horizontalScrollBar().value()
        x = note.start_beat * self.view.px_per_beat - offset
        w = max(3.0, note.length_beats * self.view.px_per_beat - 1)
        h = max(2.0, note.velocity * (VELOCITY_LANE_H - 8))
        return QRectF(x, VELOCITY_LANE_H - 4 - h, w, h)

    def _note_at(self, pos) -> MidiNote | None:
        offset = self.view.horizontalScrollBar().value()
        beat = (pos.x() + offset) / self.view.px_per_beat
        for note in reversed(self.view.clip.notes):
            if note.start_beat <= beat < note.end_beat:
                r = self._bar_rect(note)
                if r.contains(pos):
                    return note
        return None

    def _draw_bar(self, p: QPainter, note: MidiNote, selected: bool) -> None:
        r = self._bar_rect(note)
        base = NOTE_SELECTED if selected else NOTE_COLOR
        x = r.center().x()
        bottom = VELOCITY_LANE_H - 4
        top = r.top()
        p.setPen(QPen(base, 2 if selected else 1.4))
        p.drawLine(int(x), int(bottom), int(x), int(top))
        p.setPen(QPen(base.lighter(120), 1))
        p.drawLine(int(max(r.left(), x - 4)), int(top),
                   int(min(r.right(), x + 4)), int(top))

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), theme.COL_SURFACE)
        p.setPen(QPen(theme.COL_BORDER))
        p.drawLine(0, 0, self.width(), 0)
        selected = self.view.selection
        for note in self.view.clip.notes:
            self._draw_bar(p, note, note.id in selected)
        p.setPen(QPen(theme.COL_TEXT_FAINT))
        p.drawText(4, 12, "Velocity")
        p.end()

    def _apply_at(self, pos) -> None:
        offset = self.view.horizontalScrollBar().value()
        beat = (pos.x() + offset) / self.view.px_per_beat
        velocity = 1.0 - (pos.y() - 4) / (VELOCITY_LANE_H - 8)
        velocity = max(0.05, min(1.0, velocity))
        targets = [n for n in self.view.clip.notes
                   if n.start_beat <= beat < n.end_beat]
        if not targets:
            return
        sel_targets = [n for n in targets if n.id in self.view.selection]
        for note in (sel_targets or targets):
            if note.id not in self._drag_changes:
                self._drag_changes[note.id] = (note.velocity, velocity)
            else:
                old, _ = self._drag_changes[note.id]
                self._drag_changes[note.id] = (old, velocity)
            note.velocity = velocity
        self.update()

    def _show_hover_tip(self, pos, note: MidiNote | None) -> None:
        if note is None:
            QToolTip.hideText()
            return
        pct = int(round(note.velocity * 100))
        QToolTip.showText(self.mapToGlobal(pos.toPoint()),
                          f"Velocity: {pct}% ({note.velocity:.2f})")

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._dragging = True
            self._drag_changes.clear()
            self._apply_at(event.position())

    def mouseMoveEvent(self, event) -> None:
        if self._dragging:
            self._apply_at(event.position())
            return
        note = self._note_at(event.position())
        if note is not self._hover_note:
            self._hover_note = note
            self.update()
        self._show_hover_tip(event.position(), note)

    def mouseReleaseEvent(self, event) -> None:
        if not self._dragging:
            return
        self._dragging = False
        changes = [(nid, old, new)
                   for nid, (old, new) in self._drag_changes.items()
                   if abs(old - new) > 1e-9]
        if changes:
            for nid, old, _new in changes:
                note = self.view.clip.note_by_id(nid)
                if note:
                    note.velocity = old
            self.velocities_change_requested.emit(changes)
        self._drag_changes.clear()

    def leaveEvent(self, event) -> None:
        self._hover_note = None
        QToolTip.hideText()
        super().leaveEvent(event)


class PianoRollView(QGraphicsView):
    """Note grid. Emits edit requests; the model is changed by MainWindow."""

    # (pitch, start_beat, length_beats)
    note_add_requested = Signal(int, float, float)
    # list of (rel_start, pitch, length, velocity) - paste/duplicate
    notes_add_requested = Signal(object)
    # list of (note_id, old (pitch, start, length), new (pitch, start, length))
    notes_change_requested = Signal(object)
    # list of note ids
    notes_remove_requested = Signal(object)
    # note_id, slice_beat (strictly inside the note)
    note_slice_requested = Signal(int, float)
    preview_on = Signal(int)
    preview_off = Signal(int)

    def __init__(self, clip: MidiClip, snap_beats: float, parent=None) -> None:
        super().__init__(parent)
        self.clip = clip
        self.snap_beats = snap_beats if snap_beats > 0 else 0.25
        self.snap_enabled = snap_beats > 0
        self.slice_mode = False
        self.tool = "mouse"
        self.px_per_beat = 96.0
        self.selection: set[int] = set()
        self._playhead_beat = -1.0
        self._slice_hover_beat: float | None = None
        self._slice_hover_note_id: int | None = None
        self._drag = None    # dict with drag state
        self._rubber = None  # (start QPointF, current QPointF)
        self._panning = False
        self._pan_last = None
        self._last_note_length = self.snap_beats

        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setBackgroundBrush(BG)
        self.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self._update_scene_rect()

    # -- geometry helpers ---------------------------------------------------

    def _update_scene_rect(self) -> None:
        content_end = self.clip.source_start_beat + self.clip.length_beats
        w = max(content_end, 16) * self.px_per_beat + 200
        # Never leave dead space when the window is wider than the clip.
        w = max(w, float(self.viewport().width()))
        self._scene.setSceneRect(0, 0, w, PITCH_COUNT * NOTE_H)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_scene_rect()

    def scroll_to_content(self) -> None:
        """Vertically center on the median clip pitch, or C5 if empty."""
        if self.clip.notes:
            pitches = sorted(n.pitch for n in self.clip.notes)
            pitch = pitches[len(pitches) // 2]
        else:
            pitch = DEFAULT_SCROLL_PITCH
        center_y = (MAX_PITCH - pitch) * NOTE_H
        view_h = self.viewport().height()
        self.verticalScrollBar().setValue(
            max(0, int(center_y - view_h * 0.45)))

    def beat_at_x(self, x: float) -> float:
        return max(0.0, x / self.px_per_beat)

    def pitch_at_y(self, y: float) -> int:
        pitch = MAX_PITCH - int(y // NOTE_H)
        return max(MIN_PITCH, min(MAX_PITCH, pitch))

    def snap(self, beat: float) -> float:
        if not self.snap_enabled:
            return max(0.0, beat)
        return max(0.0, round(beat / self.snap_beats) * self.snap_beats)

    def snap_floor(self, beat: float) -> float:
        if not self.snap_enabled:
            return max(0.0, beat)
        return max(0.0, math.floor(beat / self.snap_beats) * self.snap_beats)

    def note_rect(self, note: MidiNote) -> QRectF:
        x = note.start_beat * self.px_per_beat
        y = (MAX_PITCH - note.pitch) * NOTE_H
        w = max(3.0, note.length_beats * self.px_per_beat)
        return QRectF(x, y + 1, w, NOTE_H - 2)

    def note_at(self, pos) -> MidiNote | None:
        for note in reversed(self.clip.notes):
            if self.note_rect(note).contains(pos):
                return note
        return None

    def _note_at_pitch_beat(self, pitch: int, beat: float) -> MidiNote | None:
        for note in self.clip.notes:
            if note.pitch == pitch and abs(note.start_beat - beat) < 1e-9:
                return note
        return None

    def set_playhead_beat(self, beat: float) -> None:
        if abs(beat - self._playhead_beat) > 1e-9:
            self._playhead_beat = beat
            self.viewport().update()

    def selected_notes(self) -> list[MidiNote]:
        return [n for n in self.clip.notes if n.id in self.selection]

    def _remember_length(self, length: float) -> None:
        self._last_note_length = max(self.snap_beats, length)

    def _emit_note_changes(self, changes: list) -> None:
        if not changes:
            return
        for nid, old, _new in changes:
            target = self.clip.note_by_id(nid)
            if target:
                (target.pitch, target.start_beat,
                 target.length_beats) = old
        self.notes_change_requested.emit(changes)

    def _nudge_selection(self, d_pitch: int = 0, d_beats: float = 0) -> None:
        notes = self.selected_notes()
        if not notes:
            return
        changes = []
        for n in notes:
            old = (n.pitch, n.start_beat, n.length_beats)
            new_pitch = max(MIN_PITCH, min(MAX_PITCH, n.pitch + d_pitch))
            new_start = max(0.0, n.start_beat + d_beats)
            new = (new_pitch, new_start, n.length_beats)
            if new != old:
                n.pitch, n.start_beat = new_pitch, new_start
                changes.append((n.id, old, new))
        self._emit_note_changes(changes)
        self._refresh()

    def _quantize_selection(self) -> None:
        notes = self.selected_notes() or list(self.clip.notes)
        if not notes:
            return
        changes = []
        for n in notes:
            old = (n.pitch, n.start_beat, n.length_beats)
            new_start = self.snap(n.start_beat)
            new = (n.pitch, new_start, n.length_beats)
            if new != old:
                n.start_beat = new_start
                changes.append((n.id, old, new))
        self._emit_note_changes(changes)
        self._refresh()

    def _paint_cell(self, pitch: int, beat: float) -> None:
        """Create one note in a paint stroke if the cell is empty."""
        if self._note_at_pitch_beat(pitch, beat) is not None:
            return
        painted = self._drag.get("painted_beats", set())
        if beat in painted:
            return
        painted.add(beat)
        self._drag["painted_beats"] = painted
        length = self._last_note_length
        self.note_add_requested.emit(pitch, beat, length)
        self._remember_length(length)

    # -- painting -------------------------------------------------------------

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        super().drawBackground(painter, rect)
        # Pitch rows (black / white keys).
        first = max(0, int(rect.top() // NOTE_H))
        last = min(PITCH_COUNT - 1, int(rect.bottom() // NOTE_H) + 1)
        for row in range(first, last + 1):
            pitch = MAX_PITCH - row
            color = ROW_BLACK if pitch % 12 in BLACK_KEYS else ROW_WHITE
            painter.fillRect(QRectF(rect.left(), row * NOTE_H,
                                    rect.width(), NOTE_H), color)

        # Alternate bar shading (every second 4-beat bar).
        start_b = max(0.0, rect.left() / self.px_per_beat)
        end_b = rect.right() / self.px_per_beat + 1
        bar = math.floor(start_b / 4.0) * 4.0
        while bar < end_b:
            if int(bar / 4.0) % 2 == 1:
                x0 = bar * self.px_per_beat
                x1 = (bar + 4.0) * self.px_per_beat
                painter.fillRect(QRectF(x0, rect.top(), x1 - x0,
                                        rect.height()), BAR_ALT)
            bar += 4.0

        # C-note separators (drawn over bar shading).
        for row in range(first, last + 1):
            pitch = MAX_PITCH - row
            if pitch % 12 == 0:
                painter.setPen(QPen(GRID_BAR, 1.2))
                painter.drawLine(int(rect.left()), (row + 1) * NOTE_H,
                                 int(rect.right()), (row + 1) * NOTE_H)

        # Beat/bar grid follows snap resolution. If snap is Off, fall back
        # to adaptive density so zoom still reveals finer subdivisions.
        step = self.snap_beats if self.snap_beats > 0 else \
            _adaptive_grid_step(self.px_per_beat)
        b = math.floor(start_b / step) * step
        while b < end_b:
            x = b * self.px_per_beat
            if abs(b % 4.0) < 1e-9:
                painter.setPen(QPen(GRID_BAR, 1.4))
            elif abs(b % 1.0) < 1e-9:
                painter.setPen(QPen(GRID_BEAT))
            else:
                painter.setPen(QPen(GRID_BEAT.darker(130)))
            painter.drawLine(int(x), int(rect.top()),
                             int(x), int(rect.bottom()))
            b += step

        # Ghost region beyond clip length.
        end_x = (self.clip.source_start_beat + self.clip.length_beats) \
            * self.px_per_beat
        if rect.right() > end_x:
            ghost = QRectF(end_x, rect.top(),
                           rect.right() - end_x, rect.height())
            painter.fillRect(ghost, GHOST_OVERLAY)

        # Clip end marker.
        painter.setPen(QPen(CLIP_END_COLOR, 1.5, Qt.DashLine))
        painter.drawLine(int(end_x), int(rect.top()),
                         int(end_x), int(rect.bottom()))

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        show_names = self.px_per_beat >= 72
        fm = QFontMetrics(painter.font()) if show_names else None
        clip_start_x = self.clip.source_start_beat * self.px_per_beat
        clip_end_x = (self.clip.source_start_beat + self.clip.length_beats) \
            * self.px_per_beat

        for note in self.clip.notes:
            r = self.note_rect(note)
            if not r.intersects(rect):
                continue
            selected = note.id in self.selection
            fill = QColor(NOTE_SELECTED if selected else NOTE_COLOR)
            fill.setAlphaF(0.45 + 0.55 * note.velocity)
            if r.right() > clip_end_x:
                fill.setAlphaF(fill.alphaF() * 0.45)
            painter.setBrush(fill)
            border = NOTE_SELECTED_BORDER if selected else NOTE_BORDER
            if r.right() > clip_end_x:
                border = border.darker(140)
            painter.setPen(QPen(border, 1))
            painter.drawRoundedRect(r, 3, 3)
            if show_names and r.width() > fm.horizontalAdvance("C#88") + 6:
                label = pitch_name(note.pitch)
                painter.setPen(QPen(QColor(12, 28, 18) if not selected
                                    else QColor(48, 32, 8)))
                painter.drawText(r.adjusted(4, 0, -2, 0),
                                 Qt.AlignLeft | Qt.AlignVCenter, label)

        if self._rubber is not None:
            r = QRectF(self._rubber[0], self._rubber[1]).normalized()
            painter.setBrush(Qt.NoBrush)
            pen = QPen(RUBBER_EDGE, 1, Qt.DashLine)
            pen.setDashPattern([4, 3])
            painter.setPen(pen)
            painter.drawRect(r)

        if self.slice_mode and self._slice_hover_beat is not None:
            note = self.clip.note_by_id(self._slice_hover_note_id or -1)
            x = self._slice_hover_beat * self.px_per_beat
            if note is not None and note.start_beat <= self._slice_hover_beat <= note.end_beat:
                r = self.note_rect(note)
                painter.setPen(QPen(QColor(255, 255, 255), 1))
                painter.drawLine(int(x), int(r.top()), int(x), int(r.bottom()))

        if self._playhead_beat >= 0:
            x = self._playhead_beat * self.px_per_beat
            painter.setPen(QPen(theme.COL_PLAYHEAD, 1.5))
            painter.drawLine(int(x), int(rect.top()), int(x), int(rect.bottom()))

    # -- selection / clipboard -------------------------------------------------

    def select_all(self) -> None:
        self.selection = {n.id for n in self.clip.notes}
        self._refresh()

    def copy_selection(self) -> None:
        notes = self.selected_notes()
        if not notes:
            return
        base = min(n.start_beat for n in notes)
        _clipboard.clear()
        _clipboard.extend((n.start_beat - base, n.pitch, n.length_beats,
                           n.velocity) for n in notes)

    def paste(self) -> None:
        if not _clipboard:
            return
        base = self.snap_floor(self._playhead_beat) \
            if self._playhead_beat >= 0 else 0.0
        self.notes_add_requested.emit(
            [(base + rel, pitch, length, velocity)
             for rel, pitch, length, velocity in _clipboard])

    def duplicate_selection(self) -> None:
        notes = self.selected_notes()
        if not notes:
            return
        start = min(n.start_beat for n in notes)
        end = max(n.end_beat for n in notes)
        span = max(self.snap_beats, end - start)
        self.notes_add_requested.emit(
            [(n.start_beat + span, n.pitch, n.length_beats, n.velocity)
             for n in notes])

    def delete_selection(self) -> None:
        if self.selection:
            self.notes_remove_requested.emit(sorted(self.selection))

    def _refresh(self) -> None:
        self.viewport().update()
        parent = self.parentWidget()
        while parent is not None and not isinstance(parent, PianoRollDialog):
            parent = parent.parentWidget()
        if parent is not None:
            parent.velocity_lane.update()

    # -- keyboard ----------------------------------------------------------------

    def keyPressEvent(self, event) -> None:
        # Layout-independent letters (Ctrl+C/V/D, Q work in any layout).
        key = letter_key(event)
        ctrl = event.modifiers() & Qt.ControlModifier
        if key == Qt.Key_Delete:
            self.delete_selection()
        elif ctrl and key == Qt.Key_A:
            self.select_all()
        elif ctrl and key == Qt.Key_C:
            self.copy_selection()
        elif ctrl and key == Qt.Key_V:
            self.paste()
        elif ctrl and key == Qt.Key_D:
            self.duplicate_selection()
        elif key == Qt.Key_Q:
            self._quantize_selection()
        elif key == Qt.Key_Up:
            self._nudge_selection(12 if ctrl else 1)
        elif key == Qt.Key_Down:
            self._nudge_selection(-12 if ctrl else -1)
        elif key == Qt.Key_Left:
            self._nudge_selection(d_beats=-self.snap_beats)
        elif key == Qt.Key_Right:
            self._nudge_selection(d_beats=self.snap_beats)
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    # -- mouse ---------------------------------------------------------------

    def _update_hover_cursor(self, pos) -> None:
        if self.slice_mode:
            self.viewport().setCursor(Qt.CrossCursor)
            return
        if self.tool == "select":
            self.viewport().setCursor(Qt.CrossCursor)
            return
        note = self.note_at(pos)
        if note and pos.x() >= self.note_rect(note).right() - RESIZE_EDGE_PX:
            self.viewport().setCursor(Qt.SizeHorCursor)
        elif note:
            self.viewport().setCursor(Qt.OpenHandCursor)
        else:
            self.viewport().setCursor(Qt.CrossCursor)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MiddleButton:
            self._panning = True
            self._pan_last = event.position()
            self.viewport().setCursor(Qt.ClosedHandCursor)
            event.accept()
            return

        pos = self.mapToScene(event.position().toPoint())
        note = self.note_at(pos)

        if event.button() == Qt.RightButton:
            if note:
                if note.id in self.selection and len(self.selection) > 1:
                    self.delete_selection()
                else:
                    self.notes_remove_requested.emit([note.id])
            return

        if event.button() != Qt.LeftButton:
            return

        if self.slice_mode:
            if note is not None:
                beat = self.beat_at_x(pos.x())
                if not (event.modifiers() & Qt.AltModifier):
                    beat = self.snap(beat)
                if note.start_beat < beat < note.end_beat:
                    self.note_slice_requested.emit(note.id, beat)
            return

        if note is None:
            if self.tool == "select" or event.modifiers() & Qt.ControlModifier:
                self._rubber = (pos, pos)
                if not event.modifiers() & Qt.ShiftModifier:
                    self.selection.clear()
                self._refresh()
                return
            beat = self.snap_floor(self.beat_at_x(pos.x()))
            pitch = self.pitch_at_y(pos.y())
            length = self._last_note_length
            self.selection.clear()
            self.note_add_requested.emit(pitch, beat, length)
            self._remember_length(length)
            self.preview_on.emit(pitch)
            self._drag = {
                "mode": "paint",
                "pitch": pitch,
                "painted_beats": {beat},
                "start_pos": pos,
            }
            return

        if event.modifiers() & Qt.ShiftModifier:
            if note.id in self.selection:
                self.selection.discard(note.id)
            else:
                self.selection.add(note.id)
            self._refresh()
            return
        if note.id not in self.selection:
            self.selection = {note.id}

        r = self.note_rect(note)
        resize = pos.x() >= r.right() - RESIZE_EDGE_PX
        group = self.selected_notes()
        self._drag = {
            "mode": "resize" if resize else "move",
            "note": note,
            "start_pos": pos,
            "orig": {n.id: (n.pitch, n.start_beat, n.length_beats)
                     for n in group},
        }
        if not resize:
            self.viewport().setCursor(Qt.ClosedHandCursor)
        self.preview_on.emit(note.pitch)
        self._refresh()

    def mouseMoveEvent(self, event) -> None:
        if self._panning and self._pan_last is not None:
            delta = event.position() - self._pan_last
            self._pan_last = event.position()
            self.horizontalScrollBar().setValue(
                int(self.horizontalScrollBar().value() - delta.x()))
            self.verticalScrollBar().setValue(
                int(self.verticalScrollBar().value() - delta.y()))
            event.accept()
            return

        pos = self.mapToScene(event.position().toPoint())

        if self.slice_mode:
            beat = self.beat_at_x(pos.x())
            if self.snap_enabled:
                beat = self.snap(beat)
            note = self.note_at(pos)
            if note is not None and note.start_beat <= beat <= note.end_beat:
                self._slice_hover_beat = beat
                self._slice_hover_note_id = note.id
            else:
                self._slice_hover_beat = None
                self._slice_hover_note_id = None
            self.viewport().update()
            if note is None:
                return

        if self._rubber is not None:
            self._rubber = (self._rubber[0], pos)
            r = QRectF(self._rubber[0], self._rubber[1]).normalized()
            hit = {n.id for n in self.clip.notes
                   if self.note_rect(n).intersects(r)}
            if event.modifiers() & Qt.ShiftModifier:
                self.selection |= hit
            else:
                self.selection = hit
            self._refresh()
            return

        if self._drag is None:
            self._update_hover_cursor(pos)
            return

        mode = self._drag.get("mode")
        if mode == "paint":
            dx = abs(pos.x() - self._drag["start_pos"].x())
            if dx >= self.snap_beats * self.px_per_beat * 0.4:
                beat = self.snap_floor(self.beat_at_x(pos.x()))
                self._paint_cell(self._drag["pitch"], beat)
            return

        note: MidiNote = self._drag["note"]
        orig = self._drag["orig"]
        orig_pitch, orig_start, orig_len = orig[note.id]
        d_beats = (pos.x() - self._drag["start_pos"].x()) / self.px_per_beat

        if mode == "move":
            free = ((event.modifiers() & Qt.ControlModifier)
                    or not self.snap_enabled)
            new_start = max(0.0, orig_start + d_beats) if free \
                else self.snap(orig_start + d_beats)
            d_start = new_start - orig_start
            new_pitch = self.pitch_at_y(pos.y())
            d_pitch = new_pitch - orig_pitch
            if new_pitch != note.pitch:
                self.preview_off.emit(note.pitch)
                self.preview_on.emit(new_pitch)
            for n in self.selected_notes():
                o_pitch, o_start, _o_len = orig[n.id]
                n.start_beat = max(0.0, o_start + d_start)
                n.pitch = max(MIN_PITCH, min(MAX_PITCH, o_pitch + d_pitch))
        elif mode == "resize":
            free = ((event.modifiers() & Qt.ControlModifier)
                    or not self.snap_enabled)
            if free:
                new_len = max(MIN_NOTE_LEN, orig_len + d_beats)
            else:
                new_len = max(self.snap_beats, self.snap(orig_len + d_beats))
            d_len = new_len - orig_len
            min_len = MIN_NOTE_LEN if free else self.snap_beats
            for n in self.selected_notes():
                _o_pitch, _o_start, o_len = orig[n.id]
                n.length_beats = max(min_len, o_len + d_len)
        self._refresh()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MiddleButton and self._panning:
            self._panning = False
            self._pan_last = None
            self.viewport().unsetCursor()
            event.accept()
            return
        if self._rubber is not None:
            self._rubber = None
            self._refresh()
            return
        if self._drag is None:
            return
        mode = self._drag.get("mode")
        if mode == "paint":
            self.preview_off.emit(self._drag["pitch"])
        elif mode == "move":
            note: MidiNote = self._drag["note"]
            self.preview_off.emit(note.pitch)
            orig = self._drag["orig"]
            changes = []
            for n in self.selected_notes():
                old = orig.get(n.id)
                new = (n.pitch, n.start_beat, n.length_beats)
                if old is not None and new != old:
                    changes.append((n.id, old, new))
            if changes:
                for nid, old, _new in changes:
                    target = self.clip.note_by_id(nid)
                    if target:
                        (target.pitch, target.start_beat,
                         target.length_beats) = old
                self.notes_change_requested.emit(changes)
            self.viewport().setCursor(Qt.OpenHandCursor)
        elif mode == "resize":
            note = self._drag["note"]
            self.preview_off.emit(note.pitch)
            orig = self._drag["orig"]
            changes = []
            for n in self.selected_notes():
                old = orig.get(n.id)
                new = (n.pitch, n.start_beat, n.length_beats)
                if old is not None and new != old:
                    changes.append((n.id, old, new))
            if changes:
                for nid, old, _new in changes:
                    target = self.clip.note_by_id(nid)
                    if target:
                        (target.pitch, target.start_beat,
                         target.length_beats) = old
                self.notes_change_requested.emit(changes)
                self._remember_length(note.length_beats)
        self._drag = None
        self._refresh()

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y() or event.angleDelta().x()
        mods = event.modifiers()
        if mods & Qt.ControlModifier:
            factor = 1.25 if delta > 0 else 0.8
            scene_x = self.mapToScene(event.position().toPoint()).x()
            beat_at_mouse = scene_x / self.px_per_beat
            self.px_per_beat = min(max(self.px_per_beat * factor, 24.0), 480.0)
            self._update_scene_rect()
            new_x = beat_at_mouse * self.px_per_beat
            self.horizontalScrollBar().setValue(
                int(self.horizontalScrollBar().value() + (new_x - scene_x)))
            self.viewport().update()
        elif mods & Qt.ShiftModifier:
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - delta)
        else:
            super().wheelEvent(event)
            return
        event.accept()

    def leaveEvent(self, event) -> None:
        if self._slice_hover_beat is not None:
            self._slice_hover_beat = None
            self._slice_hover_note_id = None
            self.viewport().update()
        super().leaveEvent(event)


class PianoRollDialog(QDialog):
    """Non-modal piano roll window for one MIDI clip."""

    # Same signatures as PianoRollView, re-emitted with track/clip context.
    note_add_requested = Signal(int, int, int, float, float)  # track, clip, pitch, start, len
    notes_add_requested = Signal(int, int, object)     # track, clip, list
    notes_change_requested = Signal(int, int, object)  # track, clip, changes
    notes_remove_requested = Signal(int, int, object)  # track, clip, ids
    note_slice_requested = Signal(int, int, int, float)  # track, clip, note_id, beat
    velocities_change_requested = Signal(int, int, object)
    preview_on = Signal(int, int)   # track_id, pitch
    preview_off = Signal(int)       # pitch
    undo_requested = Signal()
    redo_requested = Signal()

    def __init__(self, track: Track, clip: MidiClip, snap_beats: float,
                 parent=None) -> None:
        super().__init__(parent)
        self.track = track
        self.clip = clip
        self.setWindowTitle(trf("Piano Roll — {track} / {clip}",
                                track=track.name, clip=clip.name))
        self.resize(960, 600)

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)

        initial_beats = snap_beats if snap_beats > 0 else 0.25
        initial_enabled = snap_beats > 0

        self.snap_btn = QPushButton(tr("Snap"))
        self.snap_btn.setCheckable(True)
        self.snap_btn.setChecked(initial_enabled)
        self.snap_btn.setToolTip(tr("Snap to grid"))
        toolbar.addWidget(self.snap_btn)

        self.snap_combo = QComboBox()
        self._fill_snap_combo(initial_beats)
        toolbar.addWidget(self.snap_combo)

        self.mouse_btn = QPushButton()
        self.mouse_btn.setCheckable(True)
        self.mouse_btn.setChecked(True)
        self.mouse_btn.setFixedSize(30, 30)
        self.mouse_btn.setIcon(mouse_icon(theme.TEXT_DIM))
        self.mouse_btn.setIconSize(icon_size())
        self.mouse_btn.setToolTip(tr("Mouse tool (1)"))
        toolbar.addWidget(self.mouse_btn)

        self.select_btn = QPushButton()
        self.select_btn.setCheckable(True)
        self.select_btn.setFixedSize(30, 30)
        self.select_btn.setIcon(select_icon(theme.TEXT_DIM))
        self.select_btn.setIconSize(icon_size())
        self.select_btn.setToolTip(tr("Selection tool (2)"))
        toolbar.addWidget(self.select_btn)

        self.slice_btn = QPushButton(tr("\u2702 Slice"))
        self.slice_btn.setCheckable(True)
        self.slice_btn.setToolTip(tr("Slice notes (C — toggle, Esc — exit)"))
        toolbar.addWidget(self.slice_btn)

        toolbar.addStretch(1)

        self.help_btn = QPushButton(tr("Help ▾"))
        self._help_menu = QMenu(self)
        self._help_hotkeys_action = self._help_menu.addAction(tr("Hotkeys"))
        self.help_btn.setMenu(self._help_menu)
        toolbar.addWidget(self.help_btn)

        root.addLayout(toolbar)

        body = QHBoxLayout()
        body.setSpacing(0)
        self.view = PianoRollView(clip, initial_beats)
        self.view.snap_enabled = initial_enabled
        self.keyboard = KeyboardWidget(self.view)
        body.addWidget(self.keyboard)
        body.addWidget(self.view, 1)
        root.addLayout(body, 1)

        vel_row = QHBoxLayout()
        vel_row.setSpacing(0)
        spacer = QWidget()
        spacer.setFixedWidth(KEY_WIDTH)
        vel_row.addWidget(spacer)
        self.velocity_lane = VelocityLane(self.view)
        vel_row.addWidget(self.velocity_lane, 1)
        root.addLayout(vel_row)

        self.view.verticalScrollBar().valueChanged.connect(
            lambda _: self.keyboard.update())
        self.view.horizontalScrollBar().valueChanged.connect(
            lambda _: self.velocity_lane.update())

        self.view.scroll_to_content()

        self.view.note_add_requested.connect(
            lambda p, s, l: self.note_add_requested.emit(
                track.id, clip.id, p, s, l))
        self.view.notes_add_requested.connect(
            lambda notes: self.notes_add_requested.emit(
                track.id, clip.id, notes))
        self.view.notes_change_requested.connect(
            lambda changes: self.notes_change_requested.emit(
                track.id, clip.id, changes))
        self.view.notes_remove_requested.connect(
            lambda ids: self.notes_remove_requested.emit(
                track.id, clip.id, ids))
        self.view.note_slice_requested.connect(
            lambda nid, beat: self.note_slice_requested.emit(
                track.id, clip.id, nid, beat))
        self.velocity_lane.velocities_change_requested.connect(
            lambda changes: self.velocities_change_requested.emit(
                track.id, clip.id, changes))
        self.view.preview_on.connect(
            lambda p: self.preview_on.emit(track.id, p))
        self.view.preview_off.connect(self.preview_off.emit)
        self.keyboard.note_on.connect(
            lambda p: self.preview_on.emit(track.id, p))
        self.keyboard.note_off.connect(self.preview_off.emit)

        self.snap_btn.toggled.connect(self._on_snap_toggled)
        self.snap_combo.currentIndexChanged.connect(self._on_snap_resolution)
        self.mouse_btn.toggled.connect(
            lambda on: on and self._set_tool("mouse"))
        self.select_btn.toggled.connect(
            lambda on: on and self._set_tool("select"))
        self.slice_btn.toggled.connect(self._on_slice_toggled)
        self._help_hotkeys_action.triggered.connect(self._show_help)

        sc_slice = QShortcut(QKeySequence(Qt.Key_C), self)
        sc_slice.setContext(Qt.WidgetWithChildrenShortcut)
        sc_slice.activated.connect(self._shortcut_toggle_slice)
        sc_esc = QShortcut(QKeySequence(Qt.Key_Escape), self)
        sc_esc.setContext(Qt.WidgetWithChildrenShortcut)
        sc_esc.activated.connect(self._exit_slice_mode)
        sc_mouse = QShortcut(QKeySequence("1"), self)
        sc_mouse.setContext(Qt.WidgetWithChildrenShortcut)
        sc_mouse.activated.connect(lambda: self._set_tool("mouse"))
        sc_select = QShortcut(QKeySequence("2"), self)
        sc_select.setContext(Qt.WidgetWithChildrenShortcut)
        sc_select.activated.connect(lambda: self._set_tool("select"))
        # Undo/redo works inside the piano roll window too: the main
        # window's menu shortcuts don't fire while this dialog is active.
        sc_undo = QShortcut(QKeySequence.Undo, self)
        sc_undo.setContext(Qt.WidgetWithChildrenShortcut)
        sc_undo.activated.connect(self.undo_requested.emit)
        sc_redo = QShortcut(QKeySequence.Redo, self)
        sc_redo.setContext(Qt.WidgetWithChildrenShortcut)
        sc_redo.activated.connect(self.redo_requested.emit)

    def _fill_snap_combo(self, initial_beats: float | None = None) -> None:
        if initial_beats is not None:
            idx = _closest_snap_index(initial_beats)
        elif hasattr(self, "snap_combo") and self.snap_combo.count():
            idx = self.snap_combo.currentIndex()
        else:
            idx = 0
        self.snap_combo.blockSignals(True)
        self.snap_combo.clear()
        for label, _value in SNAP_RESOLUTIONS:
            self.snap_combo.addItem(tr(label))
        if 0 <= idx < self.snap_combo.count():
            self.snap_combo.setCurrentIndex(idx)
        self.snap_combo.blockSignals(False)

    def retranslate(self) -> None:
        self.setWindowTitle(trf("Piano Roll — {track} / {clip}",
                                track=self.track.name, clip=self.clip.name))
        self.snap_btn.setText(tr("Snap"))
        self.snap_btn.setToolTip(tr("Snap to grid"))
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
        self.slice_btn.setToolTip(tr("Slice notes (C — toggle, Esc — exit)"))
        self.help_btn.setText(tr("Help ▾"))
        self._help_hotkeys_action.setText(tr("Hotkeys"))

    def _on_snap_toggled(self, enabled: bool) -> None:
        self.view.snap_enabled = enabled
        self.view.viewport().update()

    def _on_snap_resolution(self, index: int) -> None:
        if index < 0:
            return
        self.view.snap_beats = SNAP_RESOLUTIONS[index][1]
        self.view.viewport().update()

    def _on_slice_toggled(self, on: bool) -> None:
        self.view.slice_mode = on
        if on:
            self.view.tool = "mouse"
            self._sync_tool_buttons(slice_on=True)
        else:
            self._sync_tool_buttons()
        if not on:
            self.view._slice_hover_beat = None
            self.view._slice_hover_note_id = None
        self.view.viewport().unsetCursor()
        self.view.viewport().update()

    def _sync_tool_buttons(self, slice_on: bool = False) -> None:
        is_select = self.view.tool == "select" and not slice_on
        states = ((self.mouse_btn, not is_select and not slice_on),
                  (self.select_btn, is_select),
                  (self.slice_btn, slice_on))
        for btn, checked in states:
            btn.blockSignals(True)
            btn.setChecked(checked)
            btn.blockSignals(False)

    def _set_tool(self, tool: str) -> None:
        self.view.tool = "select" if tool == "select" else "mouse"
        self.view.slice_mode = False
        self.view._slice_hover_beat = None
        self.view._slice_hover_note_id = None
        self.view.viewport().unsetCursor()
        self._sync_tool_buttons()
        self.view.viewport().update()

    def _shortcut_toggle_slice(self) -> None:
        mods = QApplication.keyboardModifiers()
        if mods & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
            return
        self.slice_btn.setChecked(not self.slice_btn.isChecked())

    def _exit_slice_mode(self) -> None:
        if self.slice_btn.isChecked():
            self.slice_btn.setChecked(False)
        else:
            # The shortcut swallows the key event, so keep the standard
            # "Esc closes the dialog" behaviour when slice is not active.
            self.reject()

    def _show_help(self) -> None:
        PianoRollHelpDialog(self).exec()

    def refresh(self) -> None:
        alive = {n.id for n in self.clip.notes}
        self.view.selection &= alive
        self.view._update_scene_rect()
        self.view.viewport().update()
        self.velocity_lane.update()

    def set_snap(self, snap_beats: float) -> None:
        enabled = snap_beats > 0
        beats = snap_beats if enabled else 0.25
        self.snap_btn.setChecked(enabled)
        self.view.snap_enabled = enabled
        self.view.snap_beats = beats
        self.snap_combo.setCurrentIndex(_closest_snap_index(beats))
        self.view.viewport().update()
