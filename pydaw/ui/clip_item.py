"""Timeline clip items.

AudioClipItem: waveform, drag in time and across audio tracks, edge
trimming, fade in/out handles, split via context menu.
MidiClipItem: note preview, drag in time and across MIDI tracks, resize
by the right edge, double-click opens the piano roll.
"""

from __future__ import annotations

import math
import os

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QGraphicsItem, QGraphicsSceneMouseEvent,
                               QMenu, QStyleOptionGraphicsItem)

from ..model.project import Clip, MidiClip
from .waveform import render_waveform

CLIP_COLOR = QColor(52, 100, 158)
CLIP_BORDER = QColor(107, 163, 227)
CLIP_SELECTED_BORDER = QColor(255, 255, 255, 220)
WAVE_COLOR = QColor(196, 224, 252)
CLIP_LABEL_BG = QColor(10, 20, 34, 120)
CLIP_LABEL_TEXT = QColor(225, 238, 252)
FADE_COLOR = QColor(10, 12, 16, 160)
HANDLE_COLOR = QColor(255, 255, 255, 200)

MIDI_CLIP_COLOR = QColor(55, 125, 88)
MIDI_CLIP_BORDER = QColor(190, 230, 205)
MIDI_NOTE_COLOR = QColor(228, 250, 236)
MIDI_LABEL_TEXT = QColor(230, 248, 238)
MIDI_LABEL_BG = QColor(12, 30, 20, 120)

EDGE_PX = 8        # trim/resize zone width


def fade_overlay_path(x0: float, x1: float, height: float) -> QPainterPath:
    """Darkened fade overlay with a bezier edge.

    `x0` is the silent clip edge (fade start), `x1` the point where the
    fade reaches full level. The region above/behind the curve is filled;
    the edge is a smooth S-shaped bezier (horizontal tangents at both
    ends), easing gently from the bottom of the clip to its top.
    """
    mid = (x0 + x1) / 2.0
    path = QPainterPath()
    path.moveTo(x0, height)
    path.lineTo(x0, 0)
    path.lineTo(x1, 0)
    # Smooth ease-in-out from the loud top point down to the silent
    # bottom corner.
    path.cubicTo(mid, 0.0, mid, height, x0, height)
    path.closeSubpath()
    return path


def edge_zone_px(width: float) -> float:
    """Trim/resize grab zone for a clip of `width` px.

    Full EDGE_PX on normal clips, shrinking on narrow ones so the middle
    always keeps a usable move zone (at least half of the clip).
    """
    return max(1.0, min(float(EDGE_PX), width / 4.0))
FADE_HANDLE_PX = 8
LABEL_H = 15


def waveform_source_window(clip: Clip,
                           timeline_len: int | None = None) -> tuple[int, int]:
    """Approximate source-frame window shown by the waveform preview.

    Warped playback maps timeline-relative frames as
    ``src = (source_offset + rel) / stretch_ratio``. Peaks are still drawn
    from the original file, so stretch/stretch_hd remain a source-window
    preview rather than an offline OLA/vocoder render.
    """
    ratio = max(float(clip.stretch_ratio), 1e-9)
    length = max(1, int(timeline_len if timeline_len is not None
                        else clip.length))
    src_start = max(0.0, float(clip.source_offset) / ratio)
    src_end = max(src_start + 1.0,
                  float(clip.source_offset + length) / ratio)
    return int(math.floor(src_start)), max(1, int(math.ceil(src_end - src_start)))


class _BaseClipItem(QGraphicsItem):
    """Shared drag-to-move / drag-to-another-track behaviour."""

    def __init__(self, track_id: int, timeline) -> None:
        super().__init__()
        self.track_id = track_id
        self.timeline = timeline  # TimelineView: scale, snap, callbacks
        self._mode = None         # None | "move" | subclasses add more
        self._shift_press = False
        self._press_scene = QPointF()
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.OpenHandCursor)

    # Subclasses provide these.
    def start_frame(self) -> int: ...
    def length_frames(self) -> int: ...
    def is_midi(self) -> bool: ...

    def height_px(self) -> float:
        return self.timeline.track_height - 8

    def _track_color(self) -> QColor:
        """Base color of the owning track (clips are tinted to match)."""
        track = self.timeline.project.track_by_id(self.track_id)
        if track is not None and track.color:
            c = QColor(track.color)
            if c.isValid():
                return c
        return MIDI_CLIP_COLOR if self.is_midi() else CLIP_COLOR

    def width_px(self) -> float:
        return max(2.0, self.length_frames() / self.timeline.samples_per_px)

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self.width_px(), self.height_px())

    def refresh_geometry(self) -> None:
        self.prepareGeometryChange()
        x = self.start_frame() / self.timeline.samples_per_px
        y = self.timeline.track_y(self.track_id) + 4
        self.setPos(x, y)
        self.update()

    # -- move drag ---------------------------------------------------------

    def _begin_move(self, event: QGraphicsSceneMouseEvent) -> None:
        self._mode = "move"
        self.setCursor(Qt.ClosedHandCursor)
        self._press_scene = event.scenePos()
        self._orig_frame = self.start_frame()
        self._preview_frame = self._orig_frame
        self._preview_track_id = self.track_id

    def _update_move(self, event: QGraphicsSceneMouseEvent) -> None:
        dx = event.scenePos().x() - self._press_scene.x()
        frame = self._orig_frame + int(dx * self.timeline.samples_per_px)
        frame = self.timeline.snap_frame(frame)
        self._preview_frame = frame

        info = self.timeline.row_info_at_y(event.scenePos().y())
        preview_y = self.timeline.track_y(self._preview_track_id) + 4
        if info and info["kind"] == "track":
            target = info["track"]
            if target.is_midi == self.is_midi() and not target.is_group:
                self._preview_track_id = target.id
                preview_y = info["y"] + 4
        self.setPos(frame / self.timeline.samples_per_px, preview_y)

    def _finish_move(self) -> None:
        frame = self._preview_frame
        target_track = self._preview_track_id
        if target_track != self.track_id:
            self.timeline.request_track_change(
                self.track_id, target_track, self._clip_id(),
                self._orig_frame, frame, self.is_midi())
        elif frame != self._orig_frame:
            self._emit_move(self._orig_frame, frame)
        else:
            self.refresh_geometry()

    def _clip_id(self) -> int: ...
    def _emit_move(self, old_frame: int, new_frame: int) -> None: ...

    def _handle_shift_press(self, event: QGraphicsSceneMouseEvent) -> bool:
        """Shift+click: range-select on the same track, additive toggle
        across tracks. Returns True if the event was consumed."""
        if event.button() == Qt.LeftButton \
                and event.modifiers() & Qt.ShiftModifier:
            self.timeline.extend_selection_to(self)
            self._shift_press = True
            event.accept()
            return True
        return False

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        # The release that ends a shift-click must not reach the Qt base
        # handler: QGraphicsItem finalizes selection on release and treats
        # only Ctrl as the multi-select modifier, so it would collapse the
        # range selection back to the clicked clip.
        if self._shift_press:
            self._shift_press = False
            event.accept()
            return
        super().mouseReleaseEvent(event)


class AudioClipItem(_BaseClipItem):
    def __init__(self, clip: Clip, track_id: int, timeline) -> None:
        super().__init__(track_id, timeline)
        self.clip = clip
        self._pixmap = None
        self._pixmap_width = 0
        self._pixmap_color = 0
        self._pixmap_source_window = (0, 0)
        # During a Ctrl+drag stretch preview the source window is frozen
        # at its original length so the waveform visibly squashes or
        # stretches with the new width (instead of being cropped).
        self._stretch_preview_len: int | None = None

    def start_frame(self) -> int:
        return self.clip.start_frame

    def length_frames(self) -> int:
        return self.clip.length

    def is_midi(self) -> bool:
        return False

    def _clip_id(self) -> int:
        return self.clip.id

    def _emit_move(self, old_frame: int, new_frame: int) -> None:
        self.timeline.request_move_clip(self.track_id, self.clip.id,
                                        old_frame, new_frame)

    def refresh_geometry(self) -> None:
        self._pixmap = None  # zoom or trim may have changed
        super().refresh_geometry()

    # -- painting ------------------------------------------------------------

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem,
              widget=None) -> None:
        rect = self.boundingRect()
        # The track colors are light pastels: darken for the clip body so
        # the waveform (a lighter tint of the same hue) stays readable.
        # Selected clips are noticeably brighter (body and wave) with a
        # thicker white border, so the selection reads at a glance in any
        # theme (all tones derive from the track accent color).
        base = self._track_color()
        color = base.darker(112) if self.isSelected() else base.darker(170)
        painter.setBrush(QBrush(color))
        border = CLIP_SELECTED_BORDER if self.isSelected() else base
        painter.setPen(QPen(border, 2.0 if self.isSelected() else 1))
        painter.drawRoundedRect(rect, 4, 4)

        w = int(rect.width())
        wave_h = int(rect.height() - LABEL_H)
        wave_color = base.lighter(165 if self.isSelected() else 140)
        timeline_len = self._stretch_preview_len or self.clip.length
        src_off, src_len = waveform_source_window(self.clip, timeline_len)
        src_window = (src_off, src_len)
        if self._pixmap is None or self._pixmap_width != w \
                or self._pixmap_color != wave_color.rgba() \
                or self._pixmap_source_window != src_window:
            spp = max(256, int(self.timeline.samples_per_px))
            peaks = self.timeline.bridge.file_peaks(self.clip.file_id, spp)
            p0 = int(src_off / spp) * 2
            p1 = int(math.ceil((src_off + src_len) / spp)) * 2
            peaks = peaks[p0:p1] if peaks else peaks
            self._pixmap = render_waveform(peaks, w, wave_h, wave_color)
            self._pixmap_width = w
            self._pixmap_color = wave_color.rgba()
            self._pixmap_source_window = src_window
        painter.drawPixmap(0, LABEL_H, self._pixmap)

        # Name strip on top.
        if rect.width() > 30:
            painter.fillRect(QRectF(1, 1, rect.width() - 2, LABEL_H),
                             CLIP_LABEL_BG)
            painter.setPen(QPen(CLIP_LABEL_TEXT))
            painter.drawText(QRectF(5, 0, rect.width() - 10, LABEL_H + 2),
                             Qt.AlignLeft | Qt.AlignVCenter,
                             self.clip.display_name)

        # Fade overlays: darkened regions with a bezier envelope edge.
        spp = self.timeline.samples_per_px
        h = rect.height()
        if self.clip.fade_in > 0:
            fx = self.clip.fade_in / spp
            painter.fillPath(fade_overlay_path(0.0, fx, h), FADE_COLOR)
        if self.clip.fade_out > 0:
            fx = rect.width() - self.clip.fade_out / spp
            painter.fillPath(fade_overlay_path(rect.width(), fx, h),
                             FADE_COLOR)

        # Fade handles (small squares at the top).
        painter.setBrush(QBrush(HANDLE_COLOR))
        painter.setPen(Qt.NoPen)
        fin_x = self.clip.fade_in / spp
        fout_x = rect.width() - self.clip.fade_out / spp
        painter.drawRect(QRectF(fin_x - 3, 0, 6, 6))
        painter.drawRect(QRectF(fout_x - 3, 0, 6, 6))

    # -- interaction -----------------------------------------------------------

    def _zone(self, pos: QPointF) -> str:
        rect = self.boundingRect()
        spp = self.timeline.samples_per_px
        fin_x = self.clip.fade_in / spp
        fout_x = rect.width() - self.clip.fade_out / spp
        if pos.y() <= FADE_HANDLE_PX:
            if abs(pos.x() - fin_x) <= FADE_HANDLE_PX:
                return "fade_in"
            if abs(pos.x() - fout_x) <= FADE_HANDLE_PX:
                return "fade_out"
        edge = edge_zone_px(rect.width())
        if pos.x() <= edge:
            return "trim_left"
        if pos.x() >= rect.width() - edge:
            return "trim_right"
        return "move"

    def hoverMoveEvent(self, event) -> None:
        if self.timeline.slice_mode:
            self.timeline.update_slice_hover(event.scenePos())
            self.setCursor(Qt.CrossCursor)
            super().hoverMoveEvent(event)
            return
        zone = self._zone(event.pos())
        if zone in ("trim_left", "trim_right"):
            self.setCursor(Qt.SizeHorCursor)
        elif zone in ("fade_in", "fade_out"):
            self.setCursor(Qt.PointingHandCursor)
        else:
            self.setCursor(Qt.OpenHandCursor)
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() == Qt.LeftButton and self.timeline.slice_mode:
            frame = self.timeline.frame_at_x(event.scenePos().x())
            if self.timeline.snap_enabled:
                frame = self.timeline.snap_frame(frame)
            if self.clip.start_frame < frame < self.clip.end_frame:
                self.timeline.request_slice(
                    self.track_id, self.clip.id, frame, False)
            event.accept()
            return
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        if self._handle_shift_press(event):
            return
        zone = self._zone(event.pos())
        self._press_scene = event.scenePos()
        self._orig = (self.clip.start_frame, self.clip.source_offset,
                      self.clip.length)
        self._orig_fades = (self.clip.fade_in, self.clip.fade_out)
        if event.modifiers() & Qt.ControlModifier \
                and zone in ("trim_left", "trim_right"):
            # Ctrl+drag on an edge: time-stretch instead of trim.
            self._mode = ("stretch_left" if zone == "trim_left"
                          else "stretch_right")
            self._stretch_preview_len = self.clip.length
        elif zone == "move":
            self._begin_move(event)
        else:
            self._mode = zone
        super().mousePressEvent(event)
        event.accept()

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self._mode == "move":
            self._update_move(event)
            return
        if self._mode is None:  # e.g. press was consumed by slice mode
            super().mouseMoveEvent(event)
            return
        spp = self.timeline.samples_per_px
        d_frames = int((event.scenePos().x() - self._press_scene.x()) * spp)
        start0, off0, len0 = self._orig
        min_len = int(0.05 * self.timeline.bridge.sample_rate)

        if self._mode in ("stretch_left", "stretch_right"):
            # Time-stretch preview: only the visual length changes; the
            # audio is re-rendered on release with the clip's warp method.
            from ..audio_stretch import MAX_RATIO, MIN_RATIO
            ratio0 = self.clip.stretch_ratio
            if self._mode == "stretch_right":
                new_end = self.timeline.snap_frame(start0 + len0 + d_frames)
                new_len = max(min_len, new_end - start0)
            else:
                new_start = self.timeline.snap_frame(start0 + d_frames)
                new_len = max(min_len, start0 + len0 - new_start)
            # Clamp so the total ratio stays within sane bounds.
            k = new_len / max(1, len0)
            k = max(MIN_RATIO / max(1e-9, ratio0),
                    min(k, MAX_RATIO / max(1e-9, ratio0)))
            new_len = max(min_len, int(round(len0 * k)))
            if self._mode == "stretch_left":
                self.clip.start_frame = start0 + len0 - new_len
            self.clip.length = new_len
            self.refresh_geometry()
            return

        if self._mode == "trim_left":
            new_start = self.timeline.snap_frame(start0 + d_frames)
            delta = new_start - start0
            delta = max(-off0, min(delta, len0 - min_len))
            self.clip.start_frame = start0 + delta
            self.clip.source_offset = off0 + delta
            self.clip.length = len0 - delta
            self.refresh_geometry()
        elif self._mode == "trim_right":
            file_frames = int(self.timeline.bridge.engine.file_frames(
                self.clip.file_id))
            max_len = (file_frames - off0) if file_frames > 0 else (1 << 62)
            new_end = self.timeline.snap_frame(start0 + len0 + d_frames)
            new_len = max(min_len, min(new_end - start0, max_len))
            self.clip.length = new_len
            self.refresh_geometry()
        elif self._mode == "fade_in":
            f = max(0, min(self._orig_fades[0] + d_frames,
                           self.clip.length))
            self.clip.fade_in = f
            self.update()
        elif self._mode == "fade_out":
            f = max(0, min(self._orig_fades[1] - d_frames,
                           self.clip.length))
            self.clip.fade_out = f
            self.update()

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        self.setCursor(Qt.OpenHandCursor)
        mode, self._mode = self._mode, None
        if mode == "move":
            self._finish_move()
        elif mode in ("stretch_left", "stretch_right"):
            self._stretch_preview_len = None
            self._pixmap = None
            start0, off0, len0 = self._orig
            new_len = self.clip.length
            if new_len != len0:
                k = new_len / max(1, len0)
                new = (self.clip.start_frame,           # start (left anchor moved)
                       int(round(off0 * k)),            # offset in stretched file
                       new_len,
                       int(round(self._orig_fades[0] * k)),
                       int(round(self._orig_fades[1] * k)),
                       self.clip.stretch_ratio * k)
                old = (start0, off0, len0,
                       self._orig_fades[0], self._orig_fades[1],
                       self.clip.stretch_ratio)
                # Restore the preview; the undo command re-applies.
                (self.clip.start_frame, self.clip.source_offset,
                 self.clip.length) = self._orig
                self.timeline.request_stretch(self.track_id, self.clip.id,
                                              old, new)
            else:
                (self.clip.start_frame, self.clip.source_offset,
                 self.clip.length) = self._orig
                self.refresh_geometry()
        elif mode in ("trim_left", "trim_right"):
            new = (self.clip.start_frame, self.clip.source_offset,
                   self.clip.length)
            if new != self._orig:
                # Restore, the undo command re-applies.
                (self.clip.start_frame, self.clip.source_offset,
                 self.clip.length) = self._orig
                self.timeline.request_trim(self.track_id, self.clip.id,
                                           self._orig, new)
        elif mode in ("fade_in", "fade_out"):
            new = (self.clip.fade_in, self.clip.fade_out)
            if new != self._orig_fades:
                self.clip.fade_in, self.clip.fade_out = self._orig_fades
                self.timeline.request_fades(self.track_id, self.clip.id,
                                            self._orig_fades, new)
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:
        menu = QMenu()
        props_action = menu.addAction("Properties…")
        rename_action = menu.addAction("Rename\tCtrl+R")
        split_action = menu.addAction("Split at playhead\tCtrl+E")
        duplicate_action = menu.addAction("Duplicate\tCtrl+D")
        remove_action = menu.addAction("Delete clip\tDel")
        playhead = self.timeline.playhead_frame()
        split_action.setEnabled(
            self.clip.start_frame < playhead < self.clip.end_frame)
        chosen = menu.exec(event.screenPos())
        if chosen == props_action:
            self.timeline.request_properties(self.track_id, self.clip.id,
                                             False)
        elif chosen == rename_action:
            self.timeline.request_rename(self.track_id, self.clip.id, False)
        elif chosen == split_action:
            self.timeline.request_split(self.track_id, self.clip.id, playhead)
        elif chosen == duplicate_action:
            self.timeline.request_duplicate(self.track_id, self.clip.id,
                                            False)
        elif chosen == remove_action:
            self.timeline.request_remove(self.track_id, self.clip.id, False)


class MidiClipItem(_BaseClipItem):
    def __init__(self, clip: MidiClip, track_id: int, timeline) -> None:
        super().__init__(track_id, timeline)
        self.clip = clip

    def _project(self):
        return self.timeline.project

    def start_frame(self) -> int:
        return self._project().beats_to_frames(self.clip.start_beat)

    def length_frames(self) -> int:
        return self._project().beats_to_frames(self.clip.length_beats)

    def is_midi(self) -> bool:
        return True

    def _clip_id(self) -> int:
        return self.clip.id

    def _emit_move(self, old_frame: int, new_frame: int) -> None:
        project = self._project()
        self.timeline.request_move_midi_clip(
            self.track_id, self.clip.id,
            project.frames_to_beats(old_frame),
            project.frames_to_beats(new_frame))

    # -- painting -------------------------------------------------------------

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem,
              widget=None) -> None:
        rect = self.boundingRect()
        base = self._track_color()
        # Same selection treatment as audio clips: brighter body/notes
        # and a thicker white border (all tints from the track color).
        color = base.darker(112) if self.isSelected() else base.darker(170)
        painter.setBrush(QBrush(color))
        border = CLIP_SELECTED_BORDER if self.isSelected() else base
        painter.setPen(QPen(border, 2.0 if self.isSelected() else 1))
        painter.drawRoundedRect(rect, 4, 4)

        # Mini note preview.
        if self.clip.notes and self.clip.length_beats > 0:
            src0 = self.clip.source_start_beat
            src1 = src0 + self.clip.length_beats
            visible_notes = [n for n in self.clip.notes
                             if n.end_beat > src0 and n.start_beat < src1]
        else:
            visible_notes = []
        if visible_notes and self.clip.length_beats > 0:
            pitches = [n.pitch for n in visible_notes]
            lo, hi = min(pitches), max(pitches)
            span = max(hi - lo + 1, 8)
            note_h = max(2.0, (rect.height() - LABEL_H - 4) / span)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(
                base.lighter(175 if self.isSelected() else 150)))
            for n in visible_notes:
                start = max(n.start_beat, src0)
                end = min(n.end_beat, src1)
                nx = (start - src0) / self.clip.length_beats * rect.width()
                nw = max(2.0, (end - start) / self.clip.length_beats
                         * rect.width())
                ny = LABEL_H + 2 + (hi - n.pitch) / span \
                    * (rect.height() - LABEL_H - 4)
                painter.drawRect(QRectF(nx, ny, nw, note_h - 1))

        if rect.width() > 30:
            painter.fillRect(QRectF(1, 1, rect.width() - 2, LABEL_H),
                             MIDI_LABEL_BG)
            painter.setPen(QPen(MIDI_LABEL_TEXT))
            painter.drawText(QRectF(5, 0, rect.width() - 10, LABEL_H + 2),
                             Qt.AlignLeft | Qt.AlignVCenter, self.clip.name)

    # -- interaction ------------------------------------------------------------

    def _zone(self, pos: QPointF) -> str:
        rect = self.boundingRect()
        edge = edge_zone_px(rect.width())
        if pos.x() <= edge:
            return "resize_left"
        if pos.x() >= rect.width() - edge:
            return "resize"
        return "move"

    def hoverMoveEvent(self, event) -> None:
        if self.timeline.slice_mode:
            self.timeline.update_slice_hover(event.scenePos())
            self.setCursor(Qt.CrossCursor)
            super().hoverMoveEvent(event)
            return
        zone = self._zone(event.pos())
        if zone in ("resize", "resize_left"):
            self.setCursor(Qt.SizeHorCursor)
        else:
            self.setCursor(Qt.OpenHandCursor)
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() == Qt.LeftButton and self.timeline.slice_mode:
            beat = self.timeline.project.frames_to_beats(
                self.timeline.frame_at_x(event.scenePos().x()))
            if self.timeline.snap_enabled:
                beat = self.timeline.snap_beat(beat)
            if self.clip.start_beat < beat < self.clip.end_beat:
                self.timeline.request_slice(
                    self.track_id, self.clip.id, beat, True)
            event.accept()
            return
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        if self._handle_shift_press(event):
            return
        self._press_scene = event.scenePos()
        self._orig_len = self.clip.length_beats
        self._orig = (self.clip.start_beat, self.clip.source_start_beat,
                      self.clip.length_beats)
        zone = self._zone(event.pos())
        if zone == "move":
            self._begin_move(event)
        else:
            self._mode = zone
        super().mousePressEvent(event)
        event.accept()

    def _drag_beats(self, event: QGraphicsSceneMouseEvent) -> float:
        project = self._project()
        spp = self.timeline.samples_per_px
        return project.frames_to_beats(
            int((event.scenePos().x() - self._press_scene.x()) * spp))

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self._mode == "move":
            self._update_move(event)
            return
        if self._mode is None:
            super().mouseMoveEvent(event)
            return
        d_beats = self._drag_beats(event)
        min_len = self.timeline.snap_beats or 0.25
        start0, src0, len0 = self._orig
        if self._mode == "resize":
            new_len = self.timeline.snap_beat(len0 + d_beats)
            self.clip.length_beats = max(min_len, new_len)
            self.refresh_geometry()
        elif self._mode == "resize_left":
            # The right edge stays anchored; the note window shifts with
            # the left edge (like audio trim, but empty space is allowed).
            new_start = self.timeline.snap_beat(start0 + d_beats)
            delta = max(-start0, min(new_start - start0, len0 - min_len))
            self.clip.start_beat = start0 + delta
            self.clip.source_start_beat = src0 + delta
            self.clip.length_beats = len0 - delta
            self.refresh_geometry()

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        self.setCursor(Qt.OpenHandCursor)
        mode, self._mode = self._mode, None
        if mode == "move":
            self._finish_move()
        elif mode == "resize":
            new_len = self.clip.length_beats
            if new_len != self._orig_len:
                self.clip.length_beats = self._orig_len
                self.timeline.request_resize_midi_clip(
                    self.track_id, self.clip.id, self._orig_len, new_len)
        elif mode == "resize_left":
            new = (self.clip.start_beat, self.clip.source_start_beat,
                   self.clip.length_beats)
            if new != self._orig:
                (self.clip.start_beat, self.clip.source_start_beat,
                 self.clip.length_beats) = self._orig
                self.timeline.request_trim_midi_clip(
                    self.track_id, self.clip.id, self._orig, new)
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        self.timeline.request_open_midi_clip(self.track_id, self.clip.id)
        event.accept()

    def contextMenuEvent(self, event) -> None:
        menu = QMenu()
        open_action = menu.addAction("Open in piano roll")
        props_action = menu.addAction("Properties…")
        rename_action = menu.addAction("Rename\tCtrl+R")
        split_action = menu.addAction("Split at playhead\tCtrl+E")
        duplicate_action = menu.addAction("Duplicate\tCtrl+D")
        remove_action = menu.addAction("Delete clip\tDel")
        playhead_beat = self.timeline.project.frames_to_beats(
            self.timeline.playhead_frame())
        split_action.setEnabled(
            self.clip.start_beat < playhead_beat < self.clip.end_beat)
        chosen = menu.exec(event.screenPos())
        if chosen == open_action:
            self.timeline.request_open_midi_clip(self.track_id, self.clip.id)
        elif chosen == props_action:
            self.timeline.request_properties(self.track_id, self.clip.id,
                                             True)
        elif chosen == rename_action:
            self.timeline.request_rename(self.track_id, self.clip.id, True)
        elif chosen == split_action:
            self.timeline.request_slice(
                self.track_id, self.clip.id, playhead_beat, True)
        elif chosen == duplicate_action:
            self.timeline.request_duplicate(self.track_id, self.clip.id, True)
        elif chosen == remove_action:
            self.timeline.request_remove(self.track_id, self.clip.id, True)
