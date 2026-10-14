"""Automation lane graphics item on the timeline."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QGraphicsItem, QGraphicsSceneHoverEvent,
                               QGraphicsSceneMouseEvent,
                               QGraphicsSceneWheelEvent, QMenu)

from ..model.project import (AUTOMATION_SHAPE_LABELS, AUTOMATION_SHAPES,
                             AutomationLane, AutomationPoint)
from . import theme
from .i18n import tr

LANE_LINE = QColor(theme.ACCENT)
HANDLE_SIZE = 6
PAD = 6
CURVE_STEPS = 16  # sampling resolution for non-linear segments
SEGMENT_HIT_RADIUS = 7.0
CURVE_DRAG_PIXELS = 80.0


def shape_curve(shape: str, t: float, amount: float = 1.0) -> float:
    """Normalized interpolation curve leaving a point (same as engine)."""
    amount = max(-1.0, min(1.0, amount))
    if shape == "step":
        shaped = 0.0 if amount >= 0.0 else 1.0
    elif shape == "exp":
        shaped = t * t * t if amount >= 0.0 else 1.0 - (1.0 - t) ** 3
    elif shape == "bezier":
        smooth = t * t * (3.0 - 2.0 * t)
        shaped = smooth if amount >= 0.0 else 2.0 * t - smooth
    else:
        return t
    return t + abs(amount) * (shaped - t)


class AutomationLaneItem(QGraphicsItem):
    """Editable automation curve for one lane."""

    def __init__(self, lane: AutomationLane, track_id: int, timeline) -> None:
        super().__init__()
        self.lane = lane
        self.track_id = track_id
        self.timeline = timeline
        self._drag_index: int | None = None
        self._curve_drag_index: int | None = None
        self._curve_drag_start_y = 0.0
        self._curve_drag_start_amount = 1.0
        self._curve_drag_direction = 1.0
        self._hover_segment: int | None = None
        self._saved_points: list[tuple[float, float, str, float]] | None = None
        self.setZValue(5)
        self.setAcceptHoverEvents(True)
        self.refresh_geometry()

    def refresh_geometry(self) -> None:
        self.prepareGeometryChange()
        row = self.timeline.row_info_for_lane(self.track_id, self.lane.id)
        y = row["y"] if row else 0
        height = row["height"] if row else self.timeline.AUTO_LANE_HEIGHT
        width = max(400.0, self.timeline._scene.sceneRect().width())
        self._lane_y = y
        self._lane_h = height
        self._lane_w = width
        self.setPos(0, y)
        self.update()

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self._lane_w, self._lane_h)

    def _beat_to_x(self, beat: float) -> float:
        return self.timeline.x_at_frame(
            self.timeline.project.beats_to_frames(beat))

    def _x_to_beat(self, x: float) -> float:
        return self.timeline.project.frames_to_beats(
            self.timeline.frame_at_x(x))

    def _value_to_y(self, value: float) -> float:
        v = max(0.0, min(1.0, value))
        inner = self._lane_h - 2 * PAD
        return PAD + (1.0 - v) * inner

    def _y_to_value(self, y: float) -> float:
        inner = max(1.0, self._lane_h - 2 * PAD)
        return max(0.0, min(1.0, 1.0 - (y - PAD) / inner))

    def _handle_at(self, pos: QPointF) -> int | None:
        half = HANDLE_SIZE / 2 + 2
        for i, pt in enumerate(self.lane.points):
            hx = self._beat_to_x(pt.beat)
            hy = self._value_to_y(pt.value)
            if abs(pos.x() - hx) <= half and abs(pos.y() - hy) <= half:
                return i
        return None

    def _snapshot(self) -> list[tuple[float, float, str, float]]:
        return [(p.beat, p.value, p.shape, p.curve_amount)
                for p in self.lane.points]

    def _segment_polyline(self, a: AutomationPoint,
                          b: AutomationPoint) -> list[QPointF]:
        x0 = self._beat_to_x(a.beat)
        x1 = self._beat_to_x(b.beat)
        y0 = self._value_to_y(a.value)
        y1 = self._value_to_y(b.value)
        if a.shape == "linear":
            return [QPointF(x0, y0), QPointF(x1, y1)]
        points = [QPointF(x0, y0)]
        if a.shape == "step":
            amount = max(-1.0, min(1.0, a.curve_amount))
            if amount >= 0.0:
                before_jump = y0 + (y1 - y0) * (1.0 - amount)
                points.extend([QPointF(x1, before_jump), QPointF(x1, y1)])
            else:
                after_jump = y0 + (y1 - y0) * abs(amount)
                points.extend([QPointF(x0, after_jump), QPointF(x1, y1)])
            return points
        for s in range(1, CURVE_STEPS):
            t = s / CURVE_STEPS
            ct = shape_curve(a.shape, t, a.curve_amount)
            points.append(QPointF(x0 + (x1 - x0) * t,
                                  y0 + (y1 - y0) * ct))
        points.append(QPointF(x1, y1))
        return points

    def _segments(self):
        indexed = sorted(enumerate(self.lane.points),
                         key=lambda pair: pair[1].beat)
        for (idx, a), (_, b) in zip(indexed, indexed[1:]):
            yield idx, self._segment_polyline(a, b)

    def _segment_direction(self, source_idx: int) -> float:
        indexed = sorted(enumerate(self.lane.points),
                         key=lambda pair: pair[1].beat)
        for (idx, a), (_, b) in zip(indexed, indexed[1:]):
            if idx == source_idx:
                return 1.0 if b.value >= a.value else -1.0
        return 1.0

    @staticmethod
    def _distance_to_line(pos: QPointF, a: QPointF, b: QPointF) -> float:
        dx = b.x() - a.x()
        dy = b.y() - a.y()
        length_sq = dx * dx + dy * dy
        if length_sq <= 1e-12:
            return ((pos.x() - a.x()) ** 2 + (pos.y() - a.y()) ** 2) ** 0.5
        u = ((pos.x() - a.x()) * dx + (pos.y() - a.y()) * dy) / length_sq
        u = max(0.0, min(1.0, u))
        x = a.x() + u * dx
        y = a.y() + u * dy
        return ((pos.x() - x) ** 2 + (pos.y() - y) ** 2) ** 0.5

    def _segment_at(self, pos: QPointF) -> int | None:
        best_idx = None
        best_distance = SEGMENT_HIT_RADIUS
        for idx, points in self._segments():
            for a, b in zip(points, points[1:]):
                distance = self._distance_to_line(pos, a, b)
                if distance <= best_distance:
                    best_idx = idx
                    best_distance = distance
        return best_idx

    @staticmethod
    def _path_for_points(points: list[QPointF]) -> QPainterPath:
        path = QPainterPath(points[0])
        for point in points[1:]:
            path.lineTo(point)
        return path

    def paint(self, painter: QPainter, option, widget=None) -> None:
        rect = self.boundingRect()
        painter.setPen(QPen(QColor(theme.TEXT_FAINT), 1))
        painter.setFont(painter.font())
        label = self.lane.name or self.lane.target
        painter.drawText(int(PAD), int(PAD + 10), label)

        pts = sorted(self.lane.points, key=lambda p: p.beat)
        if not pts:
            painter.setPen(QPen(QColor(theme.TEXT_FAINT), 1))
            painter.drawText(int(PAD), int(rect.height() - PAD - 2),
                             tr("click to add a point"))
            return

        # Expanded (selected) lane: accent colour and a thicker curve.
        # Compact lanes are read-only, so they are drawn in a neutral
        # theme grey (no accent) to signal that state.
        expanded = self._expanded()
        line_color = LANE_LINE if expanded else QColor(theme.TEXT_FAINT)
        painter.setPen(QPen(line_color, 2.5 if expanded else 1.5))
        for idx, points in self._segments():
            hovered = idx == self._hover_segment or idx == self._curve_drag_index
            color = QColor(theme.AMBER) if hovered else line_color
            width = 4.0 if hovered else (2.5 if expanded else 1.5)
            painter.setPen(QPen(color, width))
            painter.drawPath(self._path_for_points(points))

        painter.setBrush(QBrush(line_color))
        painter.setPen(QPen(QColor(theme.TEXT if expanded
                                   else theme.BORDER_SOFT), 1))
        half = HANDLE_SIZE / 2
        for pt in pts:
            x = self._beat_to_x(pt.beat)
            y = self._value_to_y(pt.value)
            painter.drawRect(QRectF(x - half, y - half,
                                    HANDLE_SIZE, HANDLE_SIZE))

    def _emit_change(self, old: list, new: list) -> None:
        self.timeline.automation_points_changed.emit(
            self.track_id, self.lane.id, old, new)

    def _show_point_menu(self, idx: int, screen_pos) -> None:
        pt = self.lane.points[idx]
        menu = QMenu()
        delete_action = menu.addAction(tr("Delete point"))
        menu.addSeparator()
        shape_actions = {}
        curve_menu = menu.addMenu(tr("Curve to next point"))
        for shape in AUTOMATION_SHAPES:
            act = curve_menu.addAction(tr(AUTOMATION_SHAPE_LABELS[shape]))
            act.setCheckable(True)
            act.setChecked(pt.shape == shape)
            shape_actions[act] = shape
        chosen = menu.exec(screen_pos)
        if chosen is None:
            return
        old = self._snapshot()
        if chosen == delete_action:
            new = [p for i, p in enumerate(old) if i != idx]
            self._emit_change(old, new)
        elif chosen in shape_actions:
            shape = shape_actions[chosen]
            if shape != pt.shape:
                new = [(b, v, shape if i == idx else s, amount)
                       for i, (b, v, s, amount) in enumerate(old)]
                self._emit_change(old, new)

    def _editable(self) -> bool:
        return self.timeline.is_automation_editable(self.track_id,
                                                    self.lane.id)

    def _expanded(self) -> bool:
        return self.timeline.selected_lane() == (self.track_id, self.lane.id)

    def hoverMoveEvent(self, event: QGraphicsSceneHoverEvent) -> None:
        idx = self._segment_at(event.pos()) if self._editable() else None
        if idx != self._hover_segment:
            self._hover_segment = idx
            adjustable = idx is not None \
                and self.lane.points[idx].shape != "linear"
            self.setCursor(Qt.SizeVerCursor if adjustable else Qt.ArrowCursor)
            self.update()
        event.accept()

    def hoverLeaveEvent(self, event: QGraphicsSceneHoverEvent) -> None:
        if self._hover_segment is not None:
            self._hover_segment = None
            self.unsetCursor()
            self.update()
        event.accept()

    def wheelEvent(self, event: QGraphicsSceneWheelEvent) -> None:
        if not self._editable() or not event.modifiers() & Qt.ControlModifier:
            event.ignore()
            return
        delta = event.delta()
        if not self.cycle_segment_shape(event.pos(), delta):
            event.ignore()
            return
        event.accept()

    def cycle_segment_shape(self, pos: QPointF, delta: int) -> bool:
        """Cycle the hovered segment shape from the view's wheel handler."""
        if not self._editable() or not delta:
            return False
        idx = self._segment_at(pos)
        if idx is None:
            return False
        old = self._snapshot()
        point = self.lane.points[idx]
        shape_index = AUTOMATION_SHAPES.index(point.shape)
        shape = AUTOMATION_SHAPES[
            (shape_index + (1 if delta > 0 else -1)) % len(AUTOMATION_SHAPES)]
        new = [(b, v, shape if i == idx else s, amount)
               for i, (b, v, s, amount) in enumerate(old)]
        self._emit_change(old, new)
        self._hover_segment = idx
        self.update()
        return True

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if not self._editable():
            event.ignore()
            return
        if event.button() == Qt.RightButton:
            idx = self._handle_at(event.pos())
            if idx is not None:
                self._show_point_menu(idx, event.screenPos())
                event.accept()
                return
        if event.button() == Qt.LeftButton:
            idx = self._handle_at(event.pos())
            if idx is not None:
                self._drag_index = idx
                self._saved_points = self._snapshot()
                event.accept()
                return
            idx = self._segment_at(event.pos())
            if idx is not None:
                if self.lane.points[idx].shape == "linear":
                    event.accept()
                    return
                self._curve_drag_index = idx
                self._curve_drag_start_y = event.pos().y()
                self._curve_drag_start_amount = self.lane.points[idx].curve_amount
                self._curve_drag_direction = self._segment_direction(idx)
                self._saved_points = self._snapshot()
                self._hover_segment = idx
                event.accept()
                return
            # Empty spot: single click adds a point and starts dragging it;
            # the whole add(+drag) becomes one undo entry on release.
            beat = max(0.0, self._x_to_beat(event.pos().x()))
            if not event.modifiers() & (Qt.ControlModifier | Qt.AltModifier):
                beat = self.timeline.snap_beat(beat)
            value = self._y_to_value(event.pos().y())
            self._saved_points = self._snapshot()
            pt = AutomationPoint(beat, value)
            self.lane.points.append(pt)
            self.lane.points.sort(key=lambda p: p.beat)
            self._drag_index = self.lane.points.index(pt)
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if not self._editable():
            event.ignore()
            return
        if self._curve_drag_index is not None:
            delta = (event.pos().y() - self._curve_drag_start_y) \
                / CURVE_DRAG_PIXELS * self._curve_drag_direction
            point = self.lane.points[self._curve_drag_index]
            point.curve_amount = max(
                -1.0, min(1.0, self._curve_drag_start_amount + delta))
            self.update()
            event.accept()
            return
        if self._drag_index is None:
            super().mouseMoveEvent(event)
            return
        pt = self.lane.points[self._drag_index]
        if event.modifiers() & (Qt.ControlModifier | Qt.AltModifier):
            beat = max(0.0, self._x_to_beat(event.pos().x()))
        else:
            beat = self.timeline.snap_beat(
                max(0.0, self._x_to_beat(event.pos().x())))
        pt.beat = beat
        pt.value = self._y_to_value(event.pos().y())
        self.lane.points.sort(key=lambda p: p.beat)
        self._drag_index = self.lane.points.index(pt)
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if not self._editable():
            event.ignore()
            return
        if self._curve_drag_index is not None and self._saved_points is not None:
            old = self._saved_points
            new = self._snapshot()
            if old != new:
                self.lane.points = [AutomationPoint(*point) for point in old]
                self._emit_change(old, new)
            self._curve_drag_index = None
            self._saved_points = None
            self.update()
            event.accept()
            return
        if self._drag_index is not None and self._saved_points is not None:
            old = self._saved_points
            new = self._snapshot()
            if old != new:
                restore = [AutomationPoint(*point) for point in old]
                self.lane.points = restore
                self._emit_change(old, new)
            self._drag_index = None
            self._saved_points = None
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)
