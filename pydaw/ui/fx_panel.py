"""Bottom device chain panel (Ableton-style FX rack for the selected track)."""

from __future__ import annotations

from PySide6.QtCore import QMimeData, QPoint, Qt, Signal
from PySide6.QtGui import QDrag, QPainter
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                               QMenu, QPushButton, QScrollArea, QSlider,
                               QVBoxLayout, QWidget)

from ..engine_bridge import EngineBridge, MASTER_FX_ID
from ..model.project import EFFECT_DEFS, Effect, Track
from . import theme
from .i18n import tr, trf, trf
from .knob import ResetSlider
from .track_header import NO_INSTRUMENT_LABEL
from .vst_widgets import VstParamPanel

CARD_WIDTH = 200
CARD_STYLE = (
    f"background: {theme.SURFACE};"
    f" border: 1px solid {theme.BORDER_SOFT}; border-radius: 6px;"
)

# Drag-and-drop reorder of effect cards inside one chain.
FX_SLOT_MIME = "application/x-pydaw-fx-slot"


def anchored_menu_pos(menu: QMenu, anchor: QWidget) -> QPoint:
    """Popup position under `anchor`, flipped above it if the screen ends.

    Qt's own popup fitting misplaces menus with fractional
    QT_SCALE_FACTOR values, so the position is computed explicitly.
    """
    hint = menu.sizeHint()
    screen = anchor.screen().availableGeometry()
    below = anchor.mapToGlobal(anchor.rect().bottomLeft())
    x = min(max(below.x(), screen.left()),
            max(screen.left(), screen.right() - hint.width() + 1))
    y = below.y()
    if y + hint.height() > screen.bottom() + 1:
        y = anchor.mapToGlobal(anchor.rect().topLeft()).y() - hint.height()
    y = min(max(y, screen.top()), screen.bottom() + 1 - hint.height())
    return QPoint(x, y)


def drag_card_pixmap(card: QWidget):
    """Drag preview of an effect card, slightly brightened while held.

    The overlay color comes from the theme so the effect matches both the
    dark and light palettes.
    """
    pixmap = card.grab()
    painter = QPainter(pixmap)
    painter.fillRect(pixmap.rect(), theme.drag_highlight_color())
    painter.end()
    return pixmap


class _CardHeader(QFrame):
    """Raised title bar with effect name and remove button.

    Also the drag handle: dragging the header starts a slot-reorder drag
    (mime FX_SLOT_MIME with the card's chain slot index).
    """

    remove_clicked = Signal()

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self._press_pos: QPoint | None = None
        self.setCursor(Qt.OpenHandCursor)
        self.setToolTip(tr("Drag to reorder effects"))
        self.setStyleSheet(
            f"_CardHeader {{ background: {theme.RAISED};"
            f" border: none; border-top-left-radius: 6px;"
            f" border-top-right-radius: 6px; }}")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 4, 4)
        layout.setSpacing(4)
        lab = QLabel(title)
        lab.setStyleSheet(
            f"color: {theme.TEXT}; font-weight: bold; font-size: 11px;"
            " background: transparent; border: none;")
        layout.addWidget(lab, 1)
        rm = QPushButton("\u00d7")
        rm.setFixedSize(22, 20)
        rm.setToolTip(tr("Remove effect"))
        rm.setStyleSheet(
            f"QPushButton {{ background: transparent;"
            f" color: {theme.TEXT_DIM}; border: none; border-radius: 3px;"
            f" padding: 0; }}"
            f"QPushButton:hover {{ background: {theme.RED}; color: white; }}")
        rm.clicked.connect(self.remove_clicked.emit)
        layout.addWidget(rm)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._press_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._press_pos = None
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if (self._press_pos is None
                or not (event.buttons() & Qt.LeftButton)
                or (event.position().toPoint() - self._press_pos
                    ).manhattanLength()
                < QApplication.startDragDistance()):
            super().mouseMoveEvent(event)
            return
        card = self.parentWidget()
        slot = getattr(card, "slot", None)
        if slot is None:
            return
        press = self._press_pos
        self._press_pos = None
        mime = QMimeData()
        mime.setData(FX_SLOT_MIME, str(slot).encode("ascii"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(drag_card_pixmap(card))
        # Anchor the preview under the grab point; without a hot spot the
        # drag window first appears at the pixmap's top-left and jumps.
        drag.setHotSpot(self.mapTo(card, press))
        drag.exec(Qt.MoveAction)


class _DeviceCard(QFrame):
    """Base vertical device card (~200 px wide)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedWidth(CARD_WIDTH)
        self.setStyleSheet(f"_DeviceCard {{ {CARD_STYLE} }}")


class _InstrumentCard(_DeviceCard):
    select_clicked = Signal()
    editor_clicked = Signal()
    remove_clicked = Signal()

    def __init__(self, track: Track, bridge: EngineBridge, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # All children get a parent at construction time (a parentless
        # widget polished before layout insertion can flash as a tiny
        # top-level window on Windows; rebuild() runs on every reorder).
        header = QFrame(self)
        header.setStyleSheet(
            f"background: {theme.RAISED}; border: none;"
            f" border-top-left-radius: 6px; border-top-right-radius: 6px;")
        hlay = QHBoxLayout(header)
        hlay.setContentsMargins(8, 4, 8, 4)
        title = QLabel(tr("Instrument"))
        title.setStyleSheet(
            f"color: {theme.ACCENT_MIDI}; font-weight: bold; font-size: 11px;"
            " background: transparent; border: none;")
        hlay.addWidget(title, 1)
        self._remove_btn = QPushButton("\u00d7")
        self._remove_btn.setFixedSize(22, 20)
        self._remove_btn.setToolTip(tr("Remove instrument"))
        self._remove_btn.setStyleSheet(
            f"QPushButton {{ background: transparent;"
            f" color: {theme.TEXT_DIM}; border: none; border-radius: 3px;"
            f" padding: 0; }}"
            f"QPushButton:hover {{ background: {theme.RED}; color: white; }}")
        self._remove_btn.setVisible(track.instrument.uses_vst)
        self._remove_btn.clicked.connect(self.remove_clicked.emit)
        hlay.addWidget(self._remove_btn)
        layout.addWidget(header)

        body = QVBoxLayout()
        body.setContentsMargins(8, 6, 8, 8)
        body.setSpacing(6)
        inst = track.instrument
        if inst.uses_vst:
            name = inst.vst_name or inst.vst_path or "VST"
            color = theme.TEXT
        else:
            name = NO_INSTRUMENT_LABEL
            color = theme.TEXT_DIM
        self._name_label = QLabel(name, self)
        self._name_label.setWordWrap(True)
        self._name_label.setStyleSheet(
            f"color: {color}; font-size: 10px;"
            " background: transparent; border: none;")
        body.addWidget(self._name_label)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        select_btn = QPushButton(tr("Select\u2026"), self)
        select_btn.setFixedHeight(22)
        select_btn.setToolTip(tr("Choose a VST instrument"))
        select_btn.clicked.connect(self.select_clicked.emit)
        btn_row.addWidget(select_btn)

        self._editor_btn = QPushButton(tr("Editor"), self)
        self._editor_btn.setFixedHeight(22)
        self._editor_btn.setToolTip(tr("Open VST instrument editor"))
        self._editor_btn.setEnabled(inst.uses_vst)
        self._editor_btn.setVisible(inst.uses_vst)
        self._editor_btn.clicked.connect(self.editor_clicked.emit)
        btn_row.addWidget(self._editor_btn)
        body.addLayout(btn_row)
        body.addStretch(1)
        layout.addLayout(body, 1)


class _BuiltinEffectCard(_DeviceCard):
    param_changed = Signal(int, int, float)  # slot, param index, value
    remove_clicked = Signal(int)

    def __init__(self, slot: int, fx_type: str, params: list[float],
                 parent=None) -> None:
        super().__init__(parent)
        self.slot = slot
        self._sliders: list[QSlider] = []
        self._ranges: list[tuple[float, float]] = []
        name, param_defs = EFFECT_DEFS[fx_type]

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = _CardHeader(name)
        header.remove_clicked.connect(lambda: self.remove_clicked.emit(self.slot))
        layout.addWidget(header)

        body = QVBoxLayout()
        body.setContentsMargins(8, 6, 8, 8)
        body.setSpacing(4)

        params_host = QWidget(self)
        params_host.setStyleSheet("background: transparent;")
        params_layout = QVBoxLayout(params_host)
        params_layout.setContentsMargins(0, 0, 0, 0)
        params_layout.setSpacing(4)
        for i, (label, lo, hi, _default) in enumerate(param_defs):
            row = QVBoxLayout()
            row.setSpacing(2)
            lab = QLabel(label, params_host)
            lab.setStyleSheet(
                f"color: {theme.TEXT_DIM}; font-size: 9px;"
                " background: transparent; border: none;")
            row.addWidget(lab)
            slider = ResetSlider(
                Qt.Horizontal,
                int(round((_default - lo) / (hi - lo) * 1000)),
                f"{_default:g}", parent=params_host)
            slider.setFixedHeight(16)
            slider.setRange(0, 1000)
            slider.setToolTip(trf("{label} (RMB — reset)", label=label))
            value = params[i] if i < len(params) else _default
            slider.setValue(int((value - lo) / (hi - lo) * 1000))
            slider.valueChanged.connect(
                lambda v, idx=i, lo=lo, hi=hi:
                self.param_changed.emit(self.slot, idx, lo + (hi - lo) * v / 1000))
            row.addWidget(slider)
            params_layout.addLayout(row)
            self._sliders.append(slider)
            self._ranges.append((lo, hi))

        params_scroll = QScrollArea(self)
        params_scroll.setWidgetResizable(True)
        params_scroll.setFrameShape(QFrame.NoFrame)
        params_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        params_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        params_scroll.setStyleSheet(theme.thin_scroll_area_qss())
        params_scroll.setWidget(params_host)
        body.addWidget(params_scroll, 1)
        layout.addLayout(body, 1)

    def show_param_value(self, param_index: int, value: float) -> None:
        """Display an automated value without emitting param_changed."""
        if not (0 <= param_index < len(self._sliders)):
            return
        slider = self._sliders[param_index]
        if slider.isSliderDown():
            return
        lo, hi = self._ranges[param_index]
        if hi <= lo:
            return
        ivalue = int(round((value - lo) / (hi - lo) * 1000))
        ivalue = max(0, min(1000, ivalue))
        if slider.value() != ivalue:
            slider.blockSignals(True)
            slider.setValue(ivalue)
            slider.blockSignals(False)


class _VstEffectCard(_DeviceCard):
    remove_clicked = Signal(int)
    editor_clicked = Signal(int)
    param_changed = Signal(int, int, float)

    def __init__(self, bridge: EngineBridge, track_id: int, slot: int,
                 fx: Effect, parent=None) -> None:
        super().__init__(parent)
        self.slot = slot

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = _CardHeader(fx.display_name)
        header.remove_clicked.connect(lambda: self.remove_clicked.emit(self.slot))
        layout.addWidget(header)

        body = QVBoxLayout()
        body.setContentsMargins(8, 6, 8, 8)
        body.setSpacing(4)

        loaded = bridge.resolve_fx_slot(track_id, slot) is not None
        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        editor_btn = QPushButton(tr("Editor"), self)
        editor_btn.setFixedHeight(22)
        editor_btn.setToolTip(tr("Open plugin editor window"))
        # Probing hasEditor here would create/destroy a plugin view on
        # every panel rebuild (some plugins flash tiny windows doing so);
        # the editor window falls back to generic sliders anyway.
        editor_btn.setEnabled(loaded)
        editor_btn.clicked.connect(lambda: self.editor_clicked.emit(self.slot))
        btn_row.addWidget(editor_btn)

        params_btn = QPushButton(tr("Params"), self)
        params_btn.setCheckable(True)
        params_btn.setFixedHeight(22)
        params_btn.setToolTip(tr("Show plugin parameters"))
        btn_row.addWidget(params_btn)
        body.addLayout(btn_row)

        self.param_panel: VstParamPanel | None = None
        if not loaded:
            warn = QLabel(tr("Plugin failed to load"), self)
            warn.setWordWrap(True)
            warn.setStyleSheet(
                f"color: {theme.AMBER}; font-size: 10px;"
                " background: transparent; border: none;")
            body.addWidget(warn)
        else:
            # Full parameter list (same length as the automation picker);
            # the panel scrolls, so no artificial cap is needed.
            panel = VstParamPanel(bridge, track_id, slot, max_params=512,
                                  parent=self)
            panel.setVisible(False)
            panel.param_changed.connect(
                lambda idx, norm: self.param_changed.emit(self.slot, idx, norm))
            params_btn.toggled.connect(panel.setVisible)
            body.addWidget(panel, 1)
            self.param_panel = panel

        layout.addLayout(body, 1)


class _AddEffectCard(QFrame):
    """Dashed card with + Add effect menu trigger."""

    add_builtin = Signal(str)   # fx_type
    add_vst = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedWidth(CARD_WIDTH)
        self.setStyleSheet(
            f"_AddEffectCard {{ background: transparent;"
            f" border: 1px dashed {theme.BORDER}; border-radius: 6px; }}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        self._btn = QPushButton(tr("+ Add effect"), self)
        self._btn.setToolTip(tr("Add a built-in or VST effect"))
        self._btn.setStyleSheet(
            f"QPushButton {{ background: {theme.RAISED};"
            f" color: {theme.TEXT_DIM}; border: none; border-radius: 4px;"
            f" padding: 8px; font-size: 11px; }}"
            f"QPushButton:hover {{ background: {theme.HOVER};"
            f" color: {theme.TEXT}; }}")
        self._btn.clicked.connect(self._show_menu)
        layout.addWidget(self._btn, 0, Qt.AlignCenter)
        layout.addStretch(1)

    def _show_menu(self) -> None:
        menu = QMenu(self)
        for fx_type, (name, _) in EFFECT_DEFS.items():
            menu.addAction(name, lambda t=fx_type: self.add_builtin.emit(t))
        menu.addSeparator()
        menu.addAction(tr("VST Plugin\u2026"), self.add_vst.emit)
        menu.popup(anchored_menu_pos(menu, self._btn))


def fx_drop_target_slot(x: float, card_rects: list) -> int | None:
    """Chain slot targeted by a drop at strip x-coordinate `x`.

    `card_rects` are the effect-card geometries (QRect, parent/strip
    coordinates, slot order). The dragged card takes the place of the card
    the cursor is over; gaps between cards belong to the card on the
    right, everything past the last card targets the last slot. Purely
    geometric in logical pixels, so it behaves identically at any
    QT_SCALE_FACTOR.
    """
    if not card_rects:
        return None
    for i, rect in enumerate(card_rects):
        if x <= rect.right():
            return i
    return len(card_rects) - 1


class _FxStrip(QWidget):
    """Card strip host accepting effect-reorder drops (FX_SLOT_MIME)."""

    slot_dropped = Signal(int, int)  # from_slot, to_slot (chain indices)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.effect_cards: list[QWidget] = []  # slot order, set by FxPanel

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasFormat(FX_SLOT_MIME):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasFormat(FX_SLOT_MIME):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        if not event.mimeData().hasFormat(FX_SLOT_MIME):
            return
        try:
            from_slot = int(
                bytes(event.mimeData().data(FX_SLOT_MIME)).decode("ascii"))
        except ValueError:
            return
        to_slot = fx_drop_target_slot(
            event.position().x(),
            [card.geometry() for card in self.effect_cards])
        event.acceptProposedAction()
        if to_slot is not None and to_slot != from_slot:
            self.slot_dropped.emit(from_slot, to_slot)


class FxPanel(QWidget):
    """Horizontal device chain for the selected track."""

    effect_added = Signal(int, str)
    vst_add_requested = Signal(int)
    effect_removed = Signal(int, int)
    effect_moved = Signal(int, int, int)  # track_id, from_slot, to_slot
    instrument_remove_requested = Signal(int)  # track_id
    param_changed = Signal(int, int, int, float)
    vst_param_changed = Signal(int, int, int, float)
    vst_editor_requested = Signal(int, int)
    vst_plugin_requested = Signal(int)
    vst_instrument_editor_requested = Signal(int)

    def __init__(self, bridge: EngineBridge, parent=None) -> None:
        super().__init__(parent)
        self._bridge = bridge
        self.track: Track | None = None
        # Per-slot cards for live automation feedback.
        self._builtin_cards: dict[int, _BuiltinEffectCard] = {}
        self._vst_cards: dict[int, _VstEffectCard] = {}

        self.setMinimumHeight(120)
        self.setStyleSheet(
            f"FxPanel {{ background: {theme.PANEL};"
            f" border-top: 1px solid {theme.BORDER_SOFT}; }}")

        root = QHBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(10)

        self._header_box = QVBoxLayout()
        self._header_box.setSpacing(4)
        self._track_name = QLabel(tr("Select a track"), self)
        self._track_name.setStyleSheet(
            f"color: {theme.TEXT}; font-weight: bold; font-size: 13px;"
            " background: transparent; border: none;")
        self._header_box.addWidget(self._track_name)
        self._type_badge = QLabel("", self)
        self._type_badge.setStyleSheet(
            " font-size: 9px; font-weight: bold; letter-spacing: 1px;"
            " background: transparent; border: none;")
        self._type_badge.hide()
        self._header_box.addWidget(self._type_badge)
        self._header_box.addStretch(1)
        root.addLayout(self._header_box)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setStyleSheet("background: transparent;")

        self._strip_host = _FxStrip(self._scroll)
        self._strip_host.setStyleSheet("background: transparent;")
        self._strip_host.slot_dropped.connect(self._on_slot_dropped)
        self._strip = QHBoxLayout(self._strip_host)
        self._strip.setContentsMargins(0, 0, 0, 0)
        self._strip.setSpacing(8)
        self._strip.addStretch(1)
        self._scroll.setWidget(self._strip_host)
        root.addWidget(self._scroll, 1)

        self._placeholder = QLabel(tr("Select a track"), self)
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; font-size: 12px;"
            " background: transparent; border: none;")
        root.addWidget(self._placeholder, 1)
        self._scroll.hide()

    def set_track(self, track: Track | None) -> None:
        self.track = track
        if track is None:
            self._track_name.setText(tr("Select a track"))
            self._type_badge.hide()
            self._scroll.hide()
            self._placeholder.setText(tr("Select a track"))
            self._placeholder.show()
        else:
            self._track_name.setText(track.name)
            if track.id == MASTER_FX_ID:
                badge, accent = tr("MASTER"), theme.RED
            elif track.is_group:
                badge, accent = tr("GROUP"), theme.AMBER
            elif track.is_midi:
                badge, accent = tr("MIDI"), theme.ACCENT_MIDI
            else:
                badge, accent = tr("AUDIO"), theme.ACCENT
            self._type_badge.setText(badge)
            self._type_badge.setStyleSheet(
                f"color: {accent}; font-size: 9px; font-weight: bold;"
                f" letter-spacing: 1px; background: transparent; border: none;")
            self._type_badge.show()
            self._scroll.show()
            self._placeholder.hide()

    def retranslate(self) -> None:
        """Refresh static strings after a live language switch."""
        track = self.track
        self.set_track(track)
        if track is not None:
            self.rebuild()

    def _on_slot_dropped(self, from_slot: int, to_slot: int) -> None:
        if self.track is not None:
            self.effect_moved.emit(self.track.id, from_slot, to_slot)

    def rebuild(self) -> None:
        self._builtin_cards.clear()
        self._vst_cards.clear()
        self._strip_host.effect_cards = []
        while self._strip.count() > 1:
            item = self._strip.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if self.track is None:
            return
        track = self.track
        tid = track.id

        # Cards are parented to the strip immediately: a parentless widget
        # that gets polished before insertWidget() can flash as a tiny
        # top-level window on Windows.
        if track.is_midi:
            inst_card = _InstrumentCard(track, self._bridge,
                                        parent=self._strip_host)
            inst_card.select_clicked.connect(
                lambda: self.vst_plugin_requested.emit(tid))
            inst_card.editor_clicked.connect(
                lambda: self.vst_instrument_editor_requested.emit(tid))
            inst_card.remove_clicked.connect(
                lambda: self.instrument_remove_requested.emit(tid))
            self._strip.insertWidget(0, inst_card)

        for slot, fx in enumerate(track.effects):
            if fx.type == "vst":
                card = _VstEffectCard(self._bridge, tid, slot, fx,
                                      parent=self._strip_host)
                card.remove_clicked.connect(
                    lambda s: self.effect_removed.emit(tid, s))
                card.editor_clicked.connect(
                    lambda s: self.vst_editor_requested.emit(tid, s))
                card.param_changed.connect(
                    lambda s, i, v:
                    self.vst_param_changed.emit(tid, s, i, v))
                self._vst_cards[slot] = card
            else:
                card = _BuiltinEffectCard(slot, fx.type, fx.params,
                                          parent=self._strip_host)
                card.param_changed.connect(
                    lambda s, i, v: self.param_changed.emit(tid, s, i, v))
                card.remove_clicked.connect(
                    lambda s: self.effect_removed.emit(tid, s))
                self._builtin_cards[slot] = card
            idx = self._strip.count() - 1
            self._strip.insertWidget(idx, card)
            self._strip_host.effect_cards.append(card)

        add_card = _AddEffectCard(parent=self._strip_host)
        add_card.add_builtin.connect(
            lambda t: self.effect_added.emit(tid, t))
        add_card.add_vst.connect(
            lambda: self.vst_add_requested.emit(tid))
        self._strip.insertWidget(self._strip.count() - 1, add_card)

    # -- live automation feedback -------------------------------------------

    def show_builtin_param(self, slot: int, param: int, value: float) -> None:
        card = self._builtin_cards.get(slot)
        if card is not None:
            card.show_param_value(param, value)

    def refresh_vst_panels(self) -> None:
        """Re-read normalized VST params for visible param panels."""
        for card in self._vst_cards.values():
            if card.param_panel is not None:
                card.param_panel.refresh_from_engine()

    def rebuild_vst_param_panels(self) -> None:
        """Recreate VST param sliders after a plugin changed its
        parameter list (preset switch / dynamic GUI)."""
        for card in self._vst_cards.values():
            if card.param_panel is not None:
                card.param_panel.rebuild_params()
