"""Timeline: QGraphicsScene with track lanes, clips, playhead and ruler.

The grid is musical (beats/bars from project BPM). Clips can be moved
in time, dragged to another track of the same type, trimmed and faded
(audio) or resized (MIDI). Empty MIDI lane double-click creates a clip.

Navigation:
  - Ctrl + mouse wheel       zoom in/out around the cursor
  - Shift + wheel            horizontal scroll
  - plain wheel              vertical scroll
  - Alt + wheel              vertical scroll
  - middle-button drag       pan the workspace
  - drag on the ruler      set the loop region (click = seek)
  - Delete / Ctrl+D        delete / duplicate selected clips (as a group)
  - Ctrl+C / Ctrl+V        copy clips / paste at the playhead
  - Ctrl+A                 select all clips
  - Shift+click            range-select on a track / additive across tracks
  - Ctrl+E                 split selected clips at playhead
  - Ctrl+J                 join selected clips on one lane into one clip
  - Ctrl+R                 rename the selected clip (audio or MIDI);
                           track rename lives on Ctrl+Shift+R
  - Ctrl+drag clip edge    time-stretch an audio clip (warp method
                           from clip Properties)
  - 1 / 2                  mouse tool / selection tool
  - C                      toggle slice (scissors) tool
  - Home / End             jump to start / end of the project
"""

from __future__ import annotations

from PySide6.QtCore import QMimeData, QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QPainter, QPainterPath, QPen,
                           QDragEnterEvent, QDropEvent)
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView, QMenu

from ..model.project import Project
from . import theme
from .automation_item import AutomationLaneItem
from .clip_item import AudioClipItem, MidiClipItem
from .keys import letter_key

AUDIO_EXTENSIONS = {".wav", ".wave", ".flac", ".mp3", ".aif", ".aiff"}

BG_COLOR = theme.COL_BG
LANE_COLOR = QColor(theme.LANE)
LANE_ALT_COLOR = QColor(theme.LANE_ALT)
LANE_MIDI_COLOR = QColor(theme.LANE_MIDI)
LANE_MIDI_ALT_COLOR = QColor(theme.LANE_MIDI_ALT)
LANE_GROUP_COLOR = QColor(theme.LANE_GROUP)
LANE_GROUP_ALT_COLOR = QColor(theme.LANE_GROUP).lighter(106)
LANE_AUTO_COLOR = QColor(theme.LANE_AUTO)
GRID_BEAT_COLOR = QColor(theme.GRID_BEAT)
GRID_BAR_COLOR = QColor(theme.GRID_BAR)
GRID_SUBBEAT_COLOR = GRID_BEAT_COLOR.darker(135)
LANE_SEPARATOR = QColor(theme.LANE_SEP)
RULER_COLOR = theme.COL_PANEL
RULER_TEXT = theme.COL_TEXT_DIM
RULER_TEXT_DIM = theme.COL_TEXT_FAINT
LOOP_EDGE_HIT_PX = 6
RUBBER_EDGE = QColor(theme.COL_ACCENT)
RUBBER_EDGE.setAlpha(180)


def _loop_fill() -> QColor:
    """Loop-region tint from the theme's dedicated loop color (read at
    call time: follows light/dark theme switches without a restart)."""
    c = QColor(theme.LOOP_REGION)
    c.setAlpha(46)
    return c


def _loop_edge() -> QColor:
    c = QColor(theme.LOOP_REGION)
    c.setAlpha(215)
    return c

RULER_HEIGHT = 30
LEFT_GUTTER = 18       # px before beat 0 so clip starts stay visible
AUTO_LANE_HEIGHT = 44
AUTO_LANE_HEIGHT_SELECTED = 104
MASTER_ROW_HEIGHT = 64  # matches MasterTrackHeader's fixed height
# Visual gap between the master row and the ordinary tracks below it.
# Mirrored by a spacer widget in the track panel (track_header.MASTER_GAP).
MASTER_GAP = 12
# Empty strip below the last row: right-click target for adding tracks.
# Equals the track panel's PANEL_BOTTOM_PAD so the timeline scroll range
# always covers the panel's (the master strip is a scrollable row now).
BOTTOM_PAD = 120
MIN_SPP = 8.0        # max zoom in (samples per pixel)
MAX_SPP = 48000.0    # max zoom out
GRID_SUBDIVISIONS = (4.0, 2.0, 1.0, 0.5, 0.25, 0.125, 0.0625,
                     0.03125, 0.015625)
GRID_MIN_PX = 10.0


def _mix(base: QColor, tint: QColor, amount: float) -> QColor:
    """Blend `tint` into `base` by `amount` (0..1)."""
    inv = 1.0 - amount
    return QColor(round(base.red() * inv + tint.red() * amount),
                  round(base.green() * inv + tint.green() * amount),
                  round(base.blue() * inv + tint.blue() * amount))


def lane_fill_color(track, row_idx: int) -> QColor:
    """Background fill for a track lane row (not automation/master).

    Audio/MIDI lanes are neutral; group lanes are tinted with the group
    track's own color (matching the header strip).
    """
    base = LANE_COLOR if row_idx % 2 == 0 else LANE_ALT_COLOR
    if track.is_group:
        tint = QColor(getattr(track, "color", "") or theme.AMBER)
        if tint.isValid():
            return _mix(base, tint, 0.16)
        return LANE_GROUP_COLOR if row_idx % 2 == 0 else LANE_GROUP_ALT_COLOR
    return base


class TimelineView(QGraphicsView):
    """Scrollable, zoomable multitrack timeline."""

    AUTO_LANE_HEIGHT = AUTO_LANE_HEIGHT

    # (path, track_id, frame)
    file_dropped = Signal(str, int, int)
    # (track_id, clip_id, old_frame, new_frame)
    clip_moved = Signal(int, int, int, int)
    # (from_track, to_track, clip_id, old_frame, new_frame, is_midi)
    clip_track_changed = Signal(int, int, int, int, int, bool)
    # (track_id, clip_id, old (start, offset, length), new (start, offset, length))
    clip_trimmed = Signal(int, int, object, object)
    # (track_id, clip_id, old (in, out), new (in, out))
    clip_fades_changed = Signal(int, int, object, object)
    # (track_id, clip_id, is_midi) — open the clip Properties dialog
    clip_properties_requested = Signal(int, int, bool)
    # (track_id, clip_id, is_midi) — rename the clip (Ctrl+R)
    clip_rename_requested = Signal(int, int, bool)
    # (track_id, clip_id,
    #  old/new (start, source_offset, length, fade_in, fade_out, ratio))
    clip_stretched = Signal(int, int, object, object)
    # (track_id, clip_id, frame)
    clip_split_requested = Signal(int, int, int)
    # (track_id, clip_id, position, is_midi) — frame or absolute beat
    clip_slice_requested = Signal(int, int, object, bool)
    # (track_id, clip_id, is_midi)
    clip_remove_requested = Signal(int, int, bool)
    # list of (track_id, clip_id, is_midi) — delete selection as one command
    clips_remove_requested = Signal(object)
    # list of (track_id, clip_id, is_midi) — duplicate selection as a group
    clips_duplicate_requested = Signal(object)
    # list of (track_id, clip_id, is_midi) — join selection into one clip
    clips_join_requested = Signal(object)
    # list of (track_id, clip_id, is_midi) — copy selection to clipboard
    clips_copy_requested = Signal(object)
    # paste clipboard contents at the playhead
    clips_paste_requested = Signal()
    # (track_id, clip_id, old_beat, new_beat)
    midi_clip_moved = Signal(int, int, float, float)
    # (track_id, clip_id, old_len_beats, new_len_beats)
    midi_clip_resized = Signal(int, int, float, float)
    # (track_id, clip_id, old/new (start_beat, source_start_beat, length))
    midi_clip_trimmed = Signal(int, int, object, object)
    # (track_id, clip_id)
    midi_clip_open_requested = Signal(int, int)
    # (track_id, start_beat)
    midi_clip_create_requested = Signal(int, float)
    # Point tuples: (beat, value[, shape[, curve_amount]]).
    automation_points_changed = Signal(int, int, object, object)
    # frame
    seek_requested = Signal(int)
    # (start_beat, end_beat) - dragged on the ruler
    loop_region_changed = Signal(float, float)
    # (track_id, lane_id) — automation lane clicked (select it)
    automation_lane_clicked = Signal(int, int)
    add_track_requested = Signal()
    add_midi_track_requested = Signal()
    add_group_track_requested = Signal()

    def __init__(self, project: Project, bridge, parent=None) -> None:
        super().__init__(parent)
        self.project = project
        self.bridge = bridge
        # Proxy track for the master bus (id = -1); set by MainWindow so
        # master automation lanes appear as rows below the master strip.
        self.master_track = None
        self.samples_per_px = 480.0  # 100 px/s at 48k
        self.track_height = 96
        self.snap_beats = 1.0        # 0 = snap off
        self.snap_enabled = True
        self.follow_playhead = True
        self._slice_hover: tuple[int, float, float] | None = None
        self._slice_mode = False
        self._tool = "mouse"
        self._selection_rubber: tuple[QPointF, QPointF] | None = None
        self._selection_rubber_base: set[int] = set()
        self._playhead_frame = 0
        self._playhead_dragging = False
        self._clip_items: dict[int, object] = {}
        self._auto_items: dict[tuple[int, int], AutomationLaneItem] = {}
        self._panning = False
        self._pan_last = None
        self._loop_drag_mode: str | None = None      # create | left | right
        self._loop_drag_anchor: float | None = None  # fixed edge (or create anchor)
        self._loop_drag_beat: float | None = None    # moving edge during drag
        self._loop_press_x = 0.0
        self._loop_drag_moved = False  # any >3px move during the gesture
        self._ruler_cursor_active = False
        self._folded_groups: set[int] = set()
        self._folded_auto: set[int] = set()  # track ids with lanes hidden
        self._selected_lane: tuple[int, int] | None = None

        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setBackgroundBrush(QBrush(BG_COLOR))
        self.setRenderHint(QPainter.Antialiasing, False)
        self.setRenderHint(QPainter.TextAntialiasing, True)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        self.setFocusPolicy(Qt.StrongFocus)
        # Buttonless mouse moves must reach mouseMoveEvent: the resize
        # arrows over the loop-region edges appear on hover, not only
        # while a button is pressed. (QGraphicsView only enables viewport
        # tracking itself when the scene happens to contain hover items.)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.rebuild()

    # -- coordinate helpers ------------------------------------------------

    def set_folded_groups(self, folded: set[int]) -> None:
        self._folded_groups = set(folded)
        self.refresh_visibility()

    def set_folded_auto(self, folded: set[int]) -> None:
        """Track ids whose automation lanes are collapsed (hidden)."""
        self._folded_auto = set(folded)
        self.refresh_layout()

    def visible_tracks(self) -> list:
        return [t for t in self.project.tracks
                if not (t.group_id != 0 and t.group_id in self._folded_groups)]

    def visible_rows(self) -> list[dict]:
        """Rows aligned with the track panel: master, then tracks + lanes."""
        rows: list[dict] = []
        y = 0.0
        if self.master_track is not None:
            rows.append({"kind": "master", "track": self.master_track,
                         "lane": None, "y": y,
                         "height": float(MASTER_ROW_HEIGHT)})
            y += MASTER_ROW_HEIGHT
            if self.master_track.id not in self._folded_auto:
                for lane in self.master_track.automation_lanes:
                    key = (self.master_track.id, lane.id)
                    h = (AUTO_LANE_HEIGHT_SELECTED
                         if self._selected_lane == key
                         else AUTO_LANE_HEIGHT)
                    rows.append({"kind": "auto", "track": self.master_track,
                                 "lane": lane, "y": y, "height": float(h)})
                    y += h
            y += MASTER_GAP
        for track in self.visible_tracks():
            rows.append({"kind": "track", "track": track, "lane": None,
                         "y": y, "height": float(self.track_height)})
            y += self.track_height
            if track.id in self._folded_auto:
                continue
            for lane in track.automation_lanes:
                h = (AUTO_LANE_HEIGHT_SELECTED
                     if self._selected_lane == (track.id, lane.id)
                     else AUTO_LANE_HEIGHT)
                rows.append({"kind": "auto", "track": track, "lane": lane,
                             "y": y, "height": float(h)})
                y += h
        return rows

    def is_automation_editable(self, track_id: int,
                               lane_id: int | None = None) -> bool:
        """Points are editable only in the expanded (selected) lane:
        folded (`▸A`) and compact lanes are read-only."""
        if track_id in self._folded_auto:
            return False
        if lane_id is None:
            return True
        return self._selected_lane == (track_id, lane_id)

    def selected_lane(self) -> tuple[int, int] | None:
        return self._selected_lane

    def set_selected_lane(self, key: tuple[int, int] | None) -> None:
        """Select an automation lane; the selected lane is drawn taller."""
        if self._selected_lane == key:
            return
        self._selected_lane = key
        self.refresh_layout()

    def track_y(self, track_id: int) -> float:
        for row in self.visible_rows():
            if row["kind"] == "track" and row["track"].id == track_id:
                return row["y"]
        return 0.0

    def row_info_at_y(self, y: float) -> dict | None:
        for row in self.visible_rows():
            if row["y"] <= y < row["y"] + row["height"]:
                return row
        return None

    def row_info_for_lane(self, track_id: int, lane_id: int) -> dict | None:
        for row in self.visible_rows():
            if row["kind"] == "auto" and row["track"].id == track_id \
                    and row["lane"] is not None and row["lane"].id == lane_id:
                return row
        return None

    def track_at_row(self, row: int):
        rows = self.visible_rows()
        if 0 <= row < len(rows) and rows[row]["kind"] == "track":
            return rows[row]["track"]
        return None

    def track_row(self, track_id: int) -> int:
        for i, row in enumerate(self.visible_rows()):
            if row["kind"] == "track" and row["track"].id == track_id:
                return i
        return 0

    def row_at_y(self, y: float) -> int:
        rows = self.visible_rows()
        if not rows:
            return -1
        for i, row in enumerate(rows):
            if row["y"] <= y < row["y"] + row["height"]:
                return i
        return len(rows) - 1

    def frame_at_x(self, x: float) -> int:
        return max(0, int(x * self.samples_per_px))

    def x_at_frame(self, frame: float) -> float:
        return frame / self.samples_per_px

    def snap_frame(self, frame: int) -> int:
        """Snap a frame position to the current grid (in beats)."""
        frame = max(0, frame)
        if not self.snap_enabled or self.snap_beats <= 0:
            return frame
        step = self.project.frames_per_beat * self.snap_beats
        return max(0, int(round(frame / step) * step))

    def snap_beat(self, beat: float) -> float:
        beat = max(0.0, beat)
        if not self.snap_enabled or self.snap_beats <= 0:
            return beat
        return max(0.0, round(beat / self.snap_beats) * self.snap_beats)

    def _grid_step_beats(self, px_per_beat: float) -> float:
        if self.snap_beats > 0:
            return self.snap_beats
        step = GRID_SUBDIVISIONS[0]
        for candidate in GRID_SUBDIVISIONS:
            if px_per_beat * candidate >= GRID_MIN_PX:
                step = candidate
        return step

    def playhead_frame(self) -> int:
        return self._playhead_frame

    def _playhead_hit(self, viewport_x: float) -> bool:
        scene_x = self.x_at_frame(self._playhead_frame)
        playhead_x = self.mapFromScene(QPointF(scene_x, 0.0)).x()
        return abs(viewport_x - playhead_x) <= 6.0

    def _automation_edit_hit(self, scene_pos: QPointF) -> bool:
        """Let automation handles/segments win over the playhead line."""
        for item in self.items(self.mapFromScene(scene_pos)):
            if not isinstance(item, AutomationLaneItem) or not item._editable():
                continue
            local_pos = item.mapFromScene(scene_pos)
            if item._handle_at(local_pos) is not None \
                    or item._segment_at(local_pos) is not None:
                return True
        return False

    def _seek_from_viewport_x(self, viewport_x: float,
                              modifiers: Qt.KeyboardModifiers) -> None:
        scene_x = self.mapToScene(QPoint(int(viewport_x), 0)).x()
        frame = self.frame_at_x(scene_x)
        if not modifiers & Qt.AltModifier:
            frame = self.snap_frame(frame)
        self.seek_requested.emit(frame)

    @property
    def slice_mode(self) -> bool:
        return self._slice_mode

    def set_slice_mode(self, on: bool) -> None:
        if self._slice_mode == on:
            return
        self._slice_mode = on
        if on:
            self.viewport().setCursor(Qt.CrossCursor)
        else:
            self._slice_hover = None
            self.viewport().unsetCursor()
        self.viewport().update()

    def toggle_slice_mode(self) -> None:
        self.set_slice_mode(not self._slice_mode)

    @property
    def tool(self) -> str:
        return self._tool

    def set_tool(self, tool: str) -> None:
        tool = "select" if tool == "select" else "mouse"
        if self._tool == tool:
            return
        self._tool = tool
        self._selection_rubber = None
        self._selection_rubber_base.clear()
        if self._slice_mode:
            return
        if tool == "select":
            self.viewport().setCursor(Qt.CrossCursor)
        else:
            self.viewport().unsetCursor()
        self.viewport().update()

    def _selected_clip_ids(self) -> set[int]:
        return {item.clip.id for item in self._scene.selectedItems()
                if isinstance(item, (AudioClipItem, MidiClipItem))}

    def _set_selected_clip_ids(self, clip_ids: set[int]) -> None:
        self._scene.clearSelection()
        for cid in clip_ids:
            item = self._clip_items.get(cid)
            if item is not None:
                item.setSelected(True)

    def _update_selection_rubber(self, scene_pos: QPointF,
                                 additive: bool = False) -> None:
        if self._selection_rubber is None:
            return
        start, _current = self._selection_rubber
        self._selection_rubber = (start, scene_pos)
        rect = QRectF(start, scene_pos).normalized()
        hits = {item.clip.id for item in self._clip_items.values()
                if item.isVisible() and item.sceneBoundingRect().intersects(rect)}
        self._set_selected_clip_ids(
            self._selection_rubber_base | hits if additive else hits)
        self.viewport().update()

    def update_slice_hover(self, scene_pos) -> None:
        if not self._slice_mode:
            return
        hover: tuple[int, float, float] | None = None
        item = self._scene.itemAt(scene_pos, self.transform())
        if isinstance(item, AudioClipItem):
            frame = self.frame_at_x(scene_pos.x())
            if self.snap_enabled:
                frame = self.snap_frame(frame)
            if item.clip.start_frame <= frame <= item.clip.end_frame:
                r = item.sceneBoundingRect()
                hover = (frame, r.top(), r.bottom())
        elif isinstance(item, MidiClipItem):
            beat = self.project.frames_to_beats(self.frame_at_x(scene_pos.x()))
            if self.snap_enabled:
                beat = self.snap_beat(beat)
            if item.clip.start_beat <= beat <= item.clip.end_beat:
                frame = self.project.beats_to_frames(beat)
                r = item.sceneBoundingRect()
                hover = (frame, r.top(), r.bottom())
        self._slice_hover = hover
        self.viewport().update()

    # -- scene management -----------------------------------------------------

    def rebuild(self) -> None:
        """Recreate all clip and automation items from the model.

        The clip selection survives the rebuild: rebuilds happen after
        every undoable edit (move, trim, ...), and features that act on
        the selection afterwards (Ctrl+J join, Ctrl+T/Ctrl+Shift+T
        "insert track below the selected clip") must still see it."""
        selected_ids = {cid for cid, item in self._clip_items.items()
                        if item.isSelected()}
        self._scene.clear()
        self._clip_items.clear()
        self._auto_items.clear()
        self._update_scene_rect()
        for track in self.project.tracks:
            for clip in track.clips:
                item = AudioClipItem(clip, track.id, self)
                self._scene.addItem(item)
                self._clip_items[clip.id] = item
                item.refresh_geometry()
            for mclip in track.midi_clips:
                item = MidiClipItem(mclip, track.id, self)
                self._scene.addItem(item)
                self._clip_items[mclip.id] = item
                item.refresh_geometry()
            for lane in track.automation_lanes:
                item = AutomationLaneItem(lane, track.id, self)
                self._scene.addItem(item)
                self._auto_items[(track.id, lane.id)] = item
                item.refresh_geometry()
        if self.master_track is not None:
            for lane in self.master_track.automation_lanes:
                item = AutomationLaneItem(lane, self.master_track.id, self)
                self._scene.addItem(item)
                self._auto_items[(self.master_track.id, lane.id)] = item
                item.refresh_geometry()
        for cid in selected_ids:
            item = self._clip_items.get(cid)
            if item is not None:
                item.setSelected(True)
        self.refresh_visibility()
        self.viewport().update()

    def select_clip(self, clip_id: int) -> None:
        """Select a single clip item (e.g. the copy after Ctrl+D)."""
        self._scene.clearSelection()
        item = self._clip_items.get(clip_id)
        if item is not None:
            item.setSelected(True)

    def select_clips(self, clip_ids) -> None:
        """Select several clip items (e.g. a pasted/duplicated group)."""
        self._scene.clearSelection()
        for cid in clip_ids:
            item = self._clip_items.get(cid)
            if item is not None:
                item.setSelected(True)

    def select_all_clips(self) -> None:
        """Ctrl+A: select every audio and MIDI clip on the timeline;
        pressing it again (everything already selected) deselects all."""
        items = list(self._clip_items.values())
        if items and all(item.isSelected() for item in items):
            self._scene.clearSelection()
            return
        for item in items:
            item.setSelected(True)

    def deselect_all_clips(self) -> None:
        """Select menu: clear the clip selection."""
        self._scene.clearSelection()

    def invert_clip_selection(self) -> None:
        """Select menu: selected clips become deselected and vice versa."""
        for item in self._clip_items.values():
            item.setSelected(not item.isSelected())

    def selected_clip_track_ids(self) -> set[int]:
        """Track ids of the currently selected clips."""
        return {item.track_id for item in self._scene.selectedItems()
                if isinstance(item, (AudioClipItem, MidiClipItem))}

    def select_clips_on_tracks(self, track_ids) -> None:
        """Select every clip lying on any of the given tracks."""
        ids = set(track_ids)
        for item in self._clip_items.values():
            if item.track_id in ids:
                item.setSelected(True)

    def extend_selection_to(self, item) -> None:
        """Shift+click on a clip.

        If the current selection has clips on the same track, the whole
        row between them and the clicked clip becomes selected (range
        select). Clips selected on other tracks stay selected. If there
        is nothing selected on that track, the click is an additive
        toggle — the same behaviour as Ctrl+click.
        """
        same_track = [it for it in self._scene.selectedItems()
                      if isinstance(it, (AudioClipItem, MidiClipItem))
                      and it.track_id == item.track_id
                      and it is not item]
        if not same_track:
            item.setSelected(not item.isSelected())
            return
        lo = min(min(it.start_frame() for it in same_track),
                 item.start_frame())
        hi = max(max(it.start_frame() + it.length_frames()
                     for it in same_track),
                 item.start_frame() + item.length_frames())
        for it in self._clip_items.values():
            if it.track_id != item.track_id:
                continue
            start = it.start_frame()
            end = start + it.length_frames()
            if start < hi and end > lo:
                it.setSelected(True)

    def refresh_layout(self) -> None:
        self._update_scene_rect()
        self.refresh_visibility()

    def refresh_automation_lane(self, track_id: int, lane_id: int) -> None:
        """Repaint one automation lane without recreating scene items.

        Safe to call from inside the lane item's own mouse handlers
        (a full rebuild would delete the item mid-event).
        """
        item = self._auto_items.get((track_id, lane_id))
        if item is not None:
            item.update()
            self.viewport().update()
        else:
            self.rebuild()

    def add_clip_item(self, track_id: int, clip, is_midi: bool) -> None:
        """Add one clip item incrementally (e.g. Ctrl+D duplicate) —
        much faster than a full rebuild()."""
        item = (MidiClipItem(clip, track_id, self) if is_midi
                else AudioClipItem(clip, track_id, self))
        self._scene.addItem(item)
        self._clip_items[clip.id] = item
        self._update_scene_rect()
        item.refresh_geometry()
        self.viewport().update()

    def remove_clip_item(self, clip_id: int) -> None:
        """Remove one clip item incrementally (undo of a duplicate)."""
        item = self._clip_items.pop(clip_id, None)
        if item is not None:
            self._scene.removeItem(item)
            self._update_scene_rect()
            self.viewport().update()

    def refresh_visibility(self) -> None:
        """Re-layout items and hide rows for folded group children."""
        visible_ids = {t.id for t in self.visible_tracks()}
        if self.master_track is not None:
            visible_ids.add(self.master_track.id)
        for item in self._clip_items.values():
            item.setVisible(item.track_id in visible_ids)
            if item.track_id in visible_ids:
                item.refresh_geometry()
        for (track_id, lane_id), item in self._auto_items.items():
            editable = track_id in visible_ids \
                and track_id not in self._folded_auto
            item.setVisible(editable)
            if editable:
                item.setAcceptedMouseButtons(
                    Qt.LeftButton | Qt.RightButton)
                item.refresh_geometry()
            else:
                item.setAcceptedMouseButtons(Qt.NoButton)
        self._update_scene_rect()
        self.viewport().update()

    def _total_rows_height(self) -> float:
        rows = self.visible_rows()
        if not rows:
            return float(self.track_height)
        last = rows[-1]
        return last["y"] + last["height"]

    def _update_scene_rect(self) -> None:
        total_frames = max(self.project.total_frames,
                           int(60 * self.bridge.sample_rate))
        width = total_frames / self.samples_per_px + 400 + LEFT_GUTTER
        height = RULER_HEIGHT + self._total_rows_height() + BOTTOM_PAD
        self._scene.setSceneRect(QRectF(-LEFT_GUTTER, -RULER_HEIGHT, width, height))

    # -- painting ---------------------------------------------------------------

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        super().drawBackground(painter, rect)
        # Pre-project gutter (x < 0): slightly darker than the lanes.
        if rect.left() < 0:
            gutter_w = min(-rect.left(), rect.width())
            if gutter_w > 0:
                painter.fillRect(QRectF(rect.left(), rect.top(), gutter_w,
                                        rect.height()),
                                 BG_COLOR.darker(108))
        # Track lanes (group lanes tinted; audio/MIDI share neutral lanes).
        for row in self.visible_rows():
            y = row["y"]
            h = row["height"]
            lane = QRectF(rect.left(), y, rect.width(), h)
            track = row["track"]
            if row["kind"] == "auto":
                painter.fillRect(lane, LANE_AUTO_COLOR)
            elif row["kind"] == "master":
                # Master lane is neutral: no tint of its own.
                painter.fillRect(lane, LANE_COLOR)
            else:
                idx = self.track_row(track.id)
                painter.fillRect(lane, lane_fill_color(track, idx))

        # Vertical grid: bars, beats and adaptive subdivisions.
        px_per_beat = self.project.frames_per_beat / self.samples_per_px
        if px_per_beat > 1e-6:
            step = self._grid_step_beats(px_per_beat)
            start_b = max(0.0, rect.left() / px_per_beat)
            end_b = rect.right() / px_per_beat + step
            b = int(start_b / step) * step
            if b < start_b:
                b += step
            eps = 1e-6
            while b <= end_b + eps:
                if b >= -eps:
                    x = b * px_per_beat
                    rem4 = b % 4.0
                    if rem4 < eps or rem4 > 4.0 - eps:
                        color = GRID_BAR_COLOR
                    elif abs(b - round(b)) < eps:
                        color = GRID_BEAT_COLOR
                    else:
                        color = GRID_SUBBEAT_COLOR
                    painter.setPen(QPen(color, 1))
                    painter.drawLine(int(x), int(rect.top()),
                                     int(x), int(rect.bottom()))
                b += step

        # Horizontal separators between rows.
        painter.setPen(QPen(LANE_SEPARATOR, 1))
        rows = self.visible_rows()
        for row in rows:
            y = row["y"]
            painter.drawLine(int(rect.left()), int(y),
                             int(rect.right()), int(y))
        if rows:
            bottom = rows[-1]["y"] + rows[-1]["height"]
            painter.drawLine(int(rect.left()), int(bottom),
                             int(rect.right()), int(bottom))

    def _loop_region_beats(self) -> tuple[float, float] | None:
        """Current loop region: drag preview takes priority over the model."""
        if self._loop_drag_anchor is not None \
                and self._loop_drag_beat is not None:
            a, b = self._loop_drag_anchor, self._loop_drag_beat
            if abs(a - b) < 1e-9:
                return None
            return (min(a, b), max(a, b))
        if self.project.loop_enabled \
                and self.project.loop_end_beat > self.project.loop_start_beat:
            return (self.project.loop_start_beat, self.project.loop_end_beat)
        return None

    def _ruler_loop_edge_hit(self, view_x: float) -> str | None:
        """'left' or 'right' when view_x is near an existing loop edge.

        Float-precise mapping (mapFromScene rounds to whole pixels, which
        made the grab zone flicker by a pixel at high zoom); when both
        edges are within the tolerance (narrow region) the nearer one wins.
        """
        # A switched-off loop is not drawn, so its edges must not grab
        # the mouse either.
        if not self.project.loop_enabled \
                or self.project.loop_end_beat <= self.project.loop_start_beat:
            return None
        px_per_beat = self.project.frames_per_beat / self.samples_per_px
        tr = self.viewportTransform()
        v0 = tr.map(QPointF(self.project.loop_start_beat * px_per_beat,
                            0.0)).x()
        v1 = tr.map(QPointF(self.project.loop_end_beat * px_per_beat,
                            0.0)).x()
        d0 = abs(view_x - v0)
        d1 = abs(view_x - v1)
        if d0 > LOOP_EDGE_HIT_PX and d1 > LOOP_EDGE_HIT_PX:
            return None
        return "left" if d0 <= d1 else "right"

    def _update_ruler_cursor(self, view_x: float, scene_y: float) -> None:
        """Show a resize cursor over a loop edge on the ruler.

        Only touches the viewport cursor when this method set it: clips set
        their own item cursors (open hand / resize) which QGraphicsView
        applies to the same viewport — an unconditional unsetCursor() here
        wiped them on every mouse move (the "flickering hand" bug).
        """
        if self._slice_mode or self._panning or self._loop_drag_mode is not None:
            return
        over_edge = scene_y < 0 \
            and self._ruler_loop_edge_hit(view_x) is not None
        if over_edge:
            self.viewport().setCursor(Qt.SizeHorCursor)
            self._ruler_cursor_active = True
        elif self._ruler_cursor_active:
            self.viewport().unsetCursor()
            self._ruler_cursor_active = False

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        px_per_beat = self.project.frames_per_beat / self.samples_per_px

        # Ruler strip: bar numbers + time.
        ruler = QRectF(rect.left(), -RULER_HEIGHT, rect.width(), RULER_HEIGHT)
        painter.fillRect(ruler, RULER_COLOR)
        painter.setPen(QPen(theme.COL_BORDER, 1))
        painter.drawLine(int(rect.left()), 0, int(rect.right()), 0)

        if px_per_beat > 1e-6:
            px_per_bar = px_per_beat * 4
            bar_step = 1
            while px_per_bar * bar_step < 64:
                bar_step *= 2
            start_bar = max(0, int(rect.left() / px_per_bar))
            end_bar = int(rect.right() / px_per_bar) + 1
            for bar in range(start_bar - start_bar % bar_step, end_bar,
                             bar_step):
                x = bar * px_per_bar
                painter.setPen(QPen(RULER_TEXT_DIM, 1))
                painter.drawLine(int(x), -9, int(x), 0)
                secs = self.frame_at_x(x) / max(1, self.bridge.sample_rate)
                mm, ss = divmod(int(secs), 60)
                painter.setPen(QPen(RULER_TEXT))
                painter.drawText(int(x) + 5, -17, f"{bar + 1}")
                painter.setPen(QPen(RULER_TEXT_DIM))
                painter.drawText(int(x) + 5, -5, f"{mm}:{ss:02d}")

        # Loop region: band on the ruler + tint over the tracks. Only an
        # enabled loop (or a live drag preview) is drawn: switching the
        # loop off removes the region from the timeline entirely.
        region = self._loop_region_beats()
        if region and px_per_beat > 1e-6:
            loop_fill = _loop_fill()
            loop_edge = _loop_edge()
            x0 = region[0] * px_per_beat
            x1 = region[1] * px_per_beat
            painter.fillRect(QRectF(x0, -RULER_HEIGHT, x1 - x0, 7),
                             loop_edge)
            painter.fillRect(QRectF(x0, -RULER_HEIGHT + 7, x1 - x0,
                                    RULER_HEIGHT - 7), loop_fill)
            painter.fillRect(QRectF(x0, 0, x1 - x0, rect.bottom()),
                             loop_fill)
            painter.setPen(QPen(loop_edge, 1))
            painter.drawLine(int(x0), -RULER_HEIGHT, int(x0),
                             int(rect.bottom()))
            painter.drawLine(int(x1), -RULER_HEIGHT, int(x1),
                             int(rect.bottom()))

        # Playhead with a small triangle grip on the ruler. Drawn down to
        # the bottom edge of whatever is visible (the exposed rect can be
        # taller than the scene when there are few tracks).
        x = self._playhead_frame / self.samples_per_px
        bottom = max(self._scene.sceneRect().bottom(), rect.bottom())
        # Cosmetic pen: exactly one device pixel at any zoom/DPI scale.
        # A 1-logical-px fillRect lands on fractional device pixels at
        # 125%/150% scaling and rendered as a broken/dashed line.
        # Read the theme color at paint time so a light/dark switch
        # recolors the playhead without a restart.
        playhead_pen = QPen(theme.COL_PLAYHEAD, 1)
        playhead_pen.setCosmetic(True)
        painter.setPen(playhead_pen)
        painter.drawLine(QPointF(x, -RULER_HEIGHT), QPointF(x, bottom))
        grip = QPainterPath()
        grip.moveTo(x - 5, -RULER_HEIGHT)
        grip.lineTo(x + 5, -RULER_HEIGHT)
        grip.lineTo(x, -RULER_HEIGHT + 7)
        grip.closeSubpath()
        painter.fillPath(grip, theme.COL_PLAYHEAD)

        if self._slice_mode and self._slice_hover is not None:
            frame, y0, y1 = self._slice_hover
            sx = frame / self.samples_per_px
            painter.setPen(QPen(QColor(255, 255, 255), 1))
            painter.drawLine(int(sx), int(y0), int(sx), int(y1))

        if self._selection_rubber is not None:
            r = QRectF(self._selection_rubber[0],
                       self._selection_rubber[1]).normalized()
            painter.setBrush(Qt.NoBrush)
            pen = QPen(RUBBER_EDGE, 1, Qt.DashLine)
            pen.setDashPattern([4, 3])
            painter.setPen(pen)
            painter.drawRect(r)

    def set_playhead(self, frame: int, playing: bool = False) -> None:
        if frame != self._playhead_frame:
            old = self._playhead_frame
            self._playhead_frame = frame
            if playing and self.follow_playhead:
                self._ensure_playhead_visible()
            # Repaint only the old and new playhead strips (the triangle
            # grip is ~12 px wide): a full-viewport repaint at 60 Hz makes
            # the whole timeline (waveforms included) redraw every frame,
            # which is what made the playhead look jerky on big projects.
            for f in (old, frame):
                x = self.mapFromScene(QPointF(f / self.samples_per_px, 0)).x()
                self.viewport().update(x - 8, 0, 16,
                                       self.viewport().height())

    def _ensure_playhead_visible(self) -> None:
        """Page-scroll horizontally so the moving playhead stays in view."""
        x = self._playhead_frame / self.samples_per_px
        hbar = self.horizontalScrollBar()
        view_w = self.viewport().width()
        left = hbar.value()
        if x < left or x > left + view_w - 40:
            hbar.setValue(int(max(0.0, x - view_w * 0.15)))

    # -- zoom / pan / seek -------------------------------------------------------

    def zoom(self, factor: float, anchor_view_x: float | None = None) -> None:
        """Scale the time axis, keeping `anchor_view_x` (viewport px) fixed."""
        if anchor_view_x is None:
            anchor_view_x = self.viewport().width() / 2
        scene_x = self.mapToScene(int(anchor_view_x), 0).x()
        frame_at_anchor = scene_x * self.samples_per_px
        new_spp = min(max(self.samples_per_px / factor, MIN_SPP), MAX_SPP)
        if abs(new_spp - self.samples_per_px) < 1e-9:
            return
        self.samples_per_px = new_spp
        self.refresh_layout()
        new_x = frame_at_anchor / self.samples_per_px
        self.horizontalScrollBar().setValue(
            int(self.horizontalScrollBar().value() + (new_x - scene_x)))

    def zoom_to_fit(self) -> None:
        total = max(self.project.total_frames, self.bridge.sample_rate)
        view_w = max(100, self.viewport().width() - 60)
        self.samples_per_px = min(max(total / view_w, MIN_SPP), MAX_SPP)
        self.refresh_layout()
        self.horizontalScrollBar().setValue(0)

    def _selection_frame_span(self) -> tuple[int, int] | None:
        """(start, end) frames covering every selected clip, or None."""
        start = end = None
        for item in self._scene.selectedItems():
            if not isinstance(item, (AudioClipItem, MidiClipItem)):
                continue
            c_start = item.start_frame()
            c_end = c_start + item.length_frames()
            start = c_start if start is None else min(start, c_start)
            end = c_end if end is None else max(end, c_end)
        if start is None or end is None or end <= start:
            return None
        return start, end

    def zoom_to_fit_selection(self) -> None:
        """F key: fit the selected clip(s); with no selection fit the
        whole project."""
        span = self._selection_frame_span()
        if span is None:
            self.zoom_to_fit()
            return
        start, end = span
        view_w = max(100, self.viewport().width() - 60)
        self.samples_per_px = min(
            max((end - start) / view_w, MIN_SPP), MAX_SPP)
        self.refresh_layout()
        # Center the selection in the viewport.
        span_px = (end - start) / self.samples_per_px
        margin = max(0.0, (self.viewport().width() - span_px) / 2)
        self.horizontalScrollBar().setValue(
            int(start / self.samples_per_px - margin))

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y() or event.angleDelta().x()
        mods = event.modifiers()
        if mods & Qt.ControlModifier:
            scene_pos = self.mapToScene(event.position().toPoint())
            for item in self.items(event.position().toPoint()):
                if isinstance(item, AutomationLaneItem):
                    local_pos = item.mapFromScene(scene_pos)
                    if item.cycle_segment_shape(local_pos, delta):
                        event.accept()
                        return
            factor = 1.25 if delta > 0 else 0.8
            self.zoom(factor, event.position().x())
        elif mods & Qt.ShiftModifier:
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - delta)
        elif mods & Qt.AltModifier:
            # Alt swaps angleDelta axes on some platforms; use either.
            d = event.angleDelta().x() or event.angleDelta().y()
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - d)
        else:
            super().wheelEvent(event)
        event.accept()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MiddleButton:
            self._panning = True
            self._pan_last = event.position()
            self.viewport().setCursor(Qt.ClosedHandCursor)
            event.accept()
            return

        scene_pos = self.mapToScene(event.position().toPoint())
        if event.button() == Qt.LeftButton and scene_pos.y() >= 0 \
                and self._playhead_hit(event.position().x()) \
                and not self._automation_edit_hit(scene_pos):
            self._playhead_dragging = True
            self.viewport().setCursor(Qt.SizeHorCursor)
            self._seek_from_viewport_x(event.position().x(), event.modifiers())
            event.accept()
            return
        if self._tool == "select" and not self._slice_mode \
                and event.button() == Qt.LeftButton and scene_pos.y() >= 0:
            self._selection_rubber = (scene_pos, scene_pos)
            self._selection_rubber_base = self._selected_clip_ids() \
                if event.modifiers() & Qt.ShiftModifier else set()
            if not event.modifiers() & Qt.ShiftModifier:
                self._scene.clearSelection()
            item = self._scene.itemAt(scene_pos, self.transform())
            if isinstance(item, (AudioClipItem, MidiClipItem)):
                ids = set(self._selection_rubber_base)
                if event.modifiers() & Qt.ShiftModifier:
                    if item.clip.id in ids:
                        ids.discard(item.clip.id)
                    else:
                        ids.add(item.clip.id)
                else:
                    ids.add(item.clip.id)
                self._set_selected_clip_ids(ids)
            self.viewport().update()
            event.accept()
            return
        if event.button() == Qt.LeftButton and scene_pos.y() < 0:
            # Ruler: resize an existing edge, or start a new loop-region drag.
            beat = self.snap_beat(self.project.frames_to_beats(
                self.frame_at_x(scene_pos.x())))
            edge = self._ruler_loop_edge_hit(event.position().x())
            if edge == "left":
                self._loop_drag_mode = "left"
                self._loop_drag_anchor = self.project.loop_end_beat
                self._loop_drag_beat = self.project.loop_start_beat
                self.viewport().setCursor(Qt.SizeHorCursor)
                # Mark the cursor as ruler-owned so the hover bookkeeping
                # can reset it after the drag (otherwise the resize arrows
                # stay stuck and "flicker" on later mouse moves).
                self._ruler_cursor_active = True
            elif edge == "right":
                self._loop_drag_mode = "right"
                self._loop_drag_anchor = self.project.loop_start_beat
                self._loop_drag_beat = self.project.loop_end_beat
                self.viewport().setCursor(Qt.SizeHorCursor)
                self._ruler_cursor_active = True
            else:
                self._loop_drag_mode = "create"
                self._loop_drag_anchor = beat
                self._loop_drag_beat = None
            self._loop_press_x = event.position().x()
            self._loop_drag_moved = False
            event.accept()
            return

        # Automation lanes take the click themselves: a single click on an
        # empty spot adds a point (see AutomationLaneItem.mousePressEvent).
        item = self._scene.itemAt(scene_pos, self.transform())
        if item is None and event.button() == Qt.LeftButton:
            # A single click on empty space drops the clip selection
            # right away (no second click needed).
            self._scene.clearSelection()
            if not self._slice_mode:
                frame = self.frame_at_x(scene_pos.x())
                if not event.modifiers() & Qt.AltModifier:
                    frame = self.snap_frame(frame)
                self.seek_requested.emit(frame)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._playhead_dragging:
            self._seek_from_viewport_x(event.position().x(), event.modifiers())
            event.accept()
            return
        if self._panning and self._pan_last is not None:
            delta = event.position() - self._pan_last
            self._pan_last = event.position()
            self.horizontalScrollBar().setValue(
                int(self.horizontalScrollBar().value() - delta.x()))
            self.verticalScrollBar().setValue(
                int(self.verticalScrollBar().value() - delta.y()))
            event.accept()
            return
        if self._loop_drag_mode is not None:
            moved = abs(event.position().x() - self._loop_press_x) > 3
            if moved:
                self._loop_drag_moved = True
            if moved or self._loop_drag_moved \
                    or self._loop_drag_mode != "create":
                scene_pos = self.mapToScene(event.position().toPoint())
                beat = self.snap_beat(self.project.frames_to_beats(
                    self.frame_at_x(scene_pos.x())))
                self._loop_drag_beat = beat
                self.viewport().update()
            event.accept()
            return
        if self._slice_mode:
            scene_pos = self.mapToScene(event.position().toPoint())
            self.update_slice_hover(scene_pos)
        if self._selection_rubber is not None:
            scene_pos = self.mapToScene(event.position().toPoint())
            self._update_selection_rubber(
                scene_pos, bool(event.modifiers() & Qt.ShiftModifier))
            event.accept()
            return
        # The base class manages the viewport cursor for item hover; run it
        # first so the ruler resize cursor set below is not overridden.
        super().mouseMoveEvent(event)
        scene_pos = self.mapToScene(event.position().toPoint())
        self._update_ruler_cursor(event.position().x(), scene_pos.y())

    def leaveEvent(self, event) -> None:
        if self._slice_hover is not None:
            self._slice_hover = None
            self.viewport().update()
        if not self._slice_mode and not self._panning:
            self.viewport().unsetCursor()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._playhead_dragging:
            self._playhead_dragging = False
            self._seek_from_viewport_x(event.position().x(), event.modifiers())
            self.viewport().unsetCursor()
            event.accept()
            return
        if event.button() == Qt.MiddleButton and self._panning:
            self._panning = False
            self.viewport().unsetCursor()
            event.accept()
            return
        if event.button() == Qt.LeftButton \
                and self._selection_rubber is not None:
            self._selection_rubber = None
            self._selection_rubber_base.clear()
            self.viewport().update()
            event.accept()
            return
        if event.button() == Qt.LeftButton \
                and self._loop_drag_mode is not None:
            mode = self._loop_drag_mode
            anchor = self._loop_drag_anchor
            current = self._loop_drag_beat
            self._loop_drag_mode = None
            self._loop_drag_anchor = None
            self._loop_drag_beat = None
            # "Moved" is sticky over the whole gesture: dragging a region
            # out and releasing back at the press point is still a drag
            # (a collapse), not a click.
            moved = self._loop_drag_moved \
                or abs(event.position().x() - self._loop_press_x) > 3
            self._loop_drag_moved = False
            if mode == "create" and (current is None or not moved):
                # No drag: plain click on the ruler = seek (snapped).
                scene_pos = self.mapToScene(event.position().toPoint())
                frame = self.frame_at_x(scene_pos.x())
                if not event.modifiers() & Qt.AltModifier:
                    frame = self.snap_frame(frame)
                self.seek_requested.emit(frame)
            elif not moved:
                # Click on a loop edge without dragging: the resize cursor
                # promised a resize, so leave the region (and playhead) alone.
                pass
            else:
                start = min(anchor, current)
                end = max(anchor, current)
                self.loop_region_changed.emit(start, end)
            self._update_ruler_cursor(event.position().x(),
                                    self.mapToScene(event.position().toPoint()).y())
            self.viewport().update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:
        scene_pos = self.mapToScene(event.pos())
        if scene_pos.y() < 0:
            super().contextMenuEvent(event)
            return
        n_rows = len(self.visible_tracks())
        track_bottom = self._total_rows_height()
        if scene_pos.y() < track_bottom:
            super().contextMenuEvent(event)
            return
        from .i18n import tr
        menu = QMenu(self)
        menu.addAction(tr("Add audio track (Ctrl+T)"),
                       self.add_track_requested.emit)
        menu.addAction(tr("Add MIDI track (Ctrl+Shift+T)"),
                       self.add_midi_track_requested.emit)
        menu.addAction(tr("Add group track (Ctrl+Shift+G)"),
                       self.add_group_track_requested.emit)
        menu.exec(event.globalPos())
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        scene_pos = self.mapToScene(event.position().toPoint())
        item = self._scene.itemAt(scene_pos, self.transform())
        if item is None and event.button() == Qt.LeftButton \
                and scene_pos.y() >= 0:
            info = self.row_info_at_y(scene_pos.y())
            if info and info["kind"] == "track":
                track = info["track"]
                if track.is_midi:
                    beat = self.snap_beat(self.project.frames_to_beats(
                        self.frame_at_x(scene_pos.x())))
                    self.midi_clip_create_requested.emit(track.id, beat)
                    return
        super().mouseDoubleClickEvent(event)

    # -- keyboard ---------------------------------------------------------------

    def _selected_clip_refs(self) -> list[tuple[int, int, bool]]:
        """(track_id, clip_id, is_midi) for every selected clip item."""
        refs = []
        for item in self._scene.selectedItems():
            if isinstance(item, (AudioClipItem, MidiClipItem)):
                refs.append((item.track_id, item.clip.id,
                             isinstance(item, MidiClipItem)))
        return refs

    def keyPressEvent(self, event) -> None:
        # Layout-independent letters: Ctrl+J etc. must work with a
        # Cyrillic (or any non-Latin) keyboard layout too.
        key = letter_key(event)
        if key == Qt.Key_Delete:
            refs = self._selected_clip_refs()
            if refs:
                # One signal for the whole selection: a single undo entry.
                self.clips_remove_requested.emit(refs)
            event.accept()
        elif key == Qt.Key_D and event.modifiers() & Qt.ControlModifier:
            refs = self._selected_clip_refs()
            if refs:
                self.clips_duplicate_requested.emit(refs)
            event.accept()
        elif key == Qt.Key_J and event.modifiers() & Qt.ControlModifier:
            refs = self._selected_clip_refs()
            if refs:
                self.clips_join_requested.emit(refs)
            event.accept()
        elif key == Qt.Key_C and event.modifiers() & Qt.ControlModifier:
            refs = self._selected_clip_refs()
            if refs:
                self.clips_copy_requested.emit(refs)
            event.accept()
        elif key == Qt.Key_V and event.modifiers() & Qt.ControlModifier:
            self.clips_paste_requested.emit()
            event.accept()
        elif key == Qt.Key_A and event.modifiers() & Qt.ControlModifier:
            self.select_all_clips()
            event.accept()
        elif key == Qt.Key_R and event.modifiers() == Qt.ControlModifier:
            refs = self._selected_clip_refs()
            if len(refs) == 1:
                self.request_rename(*refs[0])
            event.accept()
        elif key == Qt.Key_Home:
            self.seek_requested.emit(0)
            self.horizontalScrollBar().setValue(0)
            event.accept()
        elif key == Qt.Key_End:
            end = self.project.total_frames
            self.seek_requested.emit(end)
            self.horizontalScrollBar().setValue(
                int(self.x_at_frame(end) - self.viewport().width() * 0.8))
            event.accept()
        elif key == Qt.Key_F and not event.modifiers():
            self.zoom_to_fit_selection()
            event.accept()
        elif key in (Qt.Key_Plus, Qt.Key_Equal):
            self.zoom(1.25)
            event.accept()
        elif key == Qt.Key_Minus:
            self.zoom(0.8)
            event.accept()
        else:
            super().keyPressEvent(event)

    # -- drag & drop -------------------------------------------------------------

    def _drag_ok(self, mime: QMimeData) -> bool:
        if not mime.hasUrls():
            return False
        for url in mime.urls():
            path = url.toLocalFile().lower()
            if any(path.endswith(ext) for ext in AUDIO_EXTENSIONS):
                return True
        return False

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._drag_ok(event.mimeData()):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:
        if self._drag_ok(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        scene_pos = self.mapToScene(event.position().toPoint())
        frame = self.snap_frame(self.frame_at_x(scene_pos.x()))
        info = self.row_info_at_y(scene_pos.y())
        track_id = -1
        if info and info["kind"] == "track":
            track = info["track"]
            if track and not track.is_midi and not track.is_group:
                track_id = track.id
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if any(path.lower().endswith(ext) for ext in AUDIO_EXTENSIONS):
                self.file_dropped.emit(path, track_id, frame)
        event.acceptProposedAction()

    # -- callbacks from clip items --------------------------------------------

    def request_move_clip(self, track_id: int, clip_id: int,
                          old_frame: int, new_frame: int) -> None:
        self.clip_moved.emit(track_id, clip_id, old_frame, new_frame)

    def request_track_change(self, from_track: int, to_track: int,
                             clip_id: int, old_frame: int, new_frame: int,
                             is_midi: bool) -> None:
        # Deferred: the handler does a full scene rebuild, which would
        # delete the clip item from inside its own mouseReleaseEvent
        # (use-after-free / access violation). Emit on the next event
        # loop turn, after Qt has released the item.
        QTimer.singleShot(0, lambda: self.clip_track_changed.emit(
            from_track, to_track, clip_id, old_frame, new_frame, is_midi))

    def request_trim(self, track_id: int, clip_id: int,
                     old_tuple, new_tuple) -> None:
        self.clip_trimmed.emit(track_id, clip_id, old_tuple, new_tuple)

    def request_fades(self, track_id: int, clip_id: int,
                      old_pair, new_pair) -> None:
        self.clip_fades_changed.emit(track_id, clip_id, old_pair, new_pair)

    def request_properties(self, track_id: int, clip_id: int,
                           is_midi: bool) -> None:
        # Deferred: the dialog may rename/re-render the clip and rebuild
        # the scene; called from the clip item's own mouse handler.
        QTimer.singleShot(0, lambda: self.clip_properties_requested.emit(
            track_id, clip_id, is_midi))

    def request_rename(self, track_id: int, clip_id: int,
                       is_midi: bool) -> None:
        QTimer.singleShot(0, lambda: self.clip_rename_requested.emit(
            track_id, clip_id, is_midi))

    def request_stretch(self, track_id: int, clip_id: int,
                        old_tuple, new_tuple) -> None:
        # Deferred: the handler re-renders the audio and rebuilds the
        # scene; called from the clip item's own mouseReleaseEvent.
        QTimer.singleShot(0, lambda: self.clip_stretched.emit(
            track_id, clip_id, old_tuple, new_tuple))

    def request_split(self, track_id: int, clip_id: int, frame: int) -> None:
        # Deferred for the same reason as request_track_change: the handler
        # rebuilds the scene, and this is called from the clip item's own
        # contextMenuEvent.
        QTimer.singleShot(0, lambda: self.clip_split_requested.emit(
            track_id, clip_id, frame))

    def request_slice(self, track_id: int, clip_id: int,
                      position, is_midi: bool) -> None:
        self.clip_slice_requested.emit(track_id, clip_id, position, is_midi)

    def request_remove(self, track_id: int, clip_id: int, is_midi: bool) -> None:
        # Deferred for the same reason as request_track_change: the handler
        # rebuilds the scene, and this is called from the clip item's own
        # contextMenuEvent / key handling.
        QTimer.singleShot(0, lambda: self.clip_remove_requested.emit(
            track_id, clip_id, is_midi))

    def request_duplicate(self, track_id: int, clip_id: int,
                          is_midi: bool) -> None:
        self.clips_duplicate_requested.emit([(track_id, clip_id, is_midi)])

    def request_move_midi_clip(self, track_id: int, clip_id: int,
                               old_beat: float, new_beat: float) -> None:
        self.midi_clip_moved.emit(track_id, clip_id, old_beat, new_beat)

    def request_resize_midi_clip(self, track_id: int, clip_id: int,
                                 old_len: float, new_len: float) -> None:
        self.midi_clip_resized.emit(track_id, clip_id, old_len, new_len)

    def request_trim_midi_clip(self, track_id: int, clip_id: int,
                               old_tuple, new_tuple) -> None:
        """Left-edge trim: (start_beat, source_start_beat, length_beats)."""
        self.midi_clip_trimmed.emit(track_id, clip_id, old_tuple, new_tuple)


    def request_open_midi_clip(self, track_id: int, clip_id: int) -> None:
        self.midi_clip_open_requested.emit(track_id, clip_id)
