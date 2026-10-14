"""Main window: wires together model, engine bridge, timeline and panels."""

from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile

from PySide6.QtCore import QEvent, QEventLoop, QObject, QRunnable, QSettings, \
    Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QHBoxLayout,
                               QInputDialog, QMainWindow, QMenu, QMessageBox,
                               QSplitter, QVBoxLayout, QWidget)

from ..audio_join import render_join_wav
from ..audio_stretch import MAX_RATIO, MIN_RATIO, NEW_CLIP_WARP, load_clip_audio
from ..engine_bridge import EngineBridge, ENGINE_AVAILABLE, MASTER_FX_ID
from ..paths import default_export_dir, default_projects_dir
from ..model.commands import Command, CompositeCommand, UndoStack
from ..model.project import (AutomationLane, AutomationPoint, Clip,
                             EFFECT_DEFS, Effect, Instrument, MidiClip,
                             MidiNote, Project, Track, next_id)
from . import knob
from .fx_panel import FxPanel
from .i18n import tr, trf
from .export_dialog import (ExportDialog, MASTER_ID, export_mix_to_file,
                            safe_filename)
from .mixer_window import MixerWindow
from .piano_roll import PianoRollDialog
from .playhead_smoother import PlayheadSmoother
from .plugin_browser import PluginBrowserDialog
from .settings_dialog import SettingsDialog
from .shortcuts_dialog import ShortcutsDialog
from .keys import letter_key
from .timeline import TimelineView
from .track_header import (MasterTrackHeader, TrackPanel,
                           VstParamPickerDialog)
from .track_properties_dialog import TrackPropertiesDialog
from .transport_bar import TransportBar
from .vst_editor_window import VstEditorWindow

PROJECT_FILTER = "PyDAW project (*.pydaw)"
AUDIO_FILTER = "Audio files (*.wav *.flac *.mp3 *.aif *.aiff)"

SESSION_RESTORE_FILE = os.path.join(
    tempfile.gettempdir(), f"pydaw_session_restore_{os.getpid()}.pydaw")


class ToolTipSuppressor(QObject):
    """App-level event filter: swallows all tooltip events when installed."""

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.ToolTip:
            return True
        return super().eventFilter(obj, event)


def _group_block_end(tracks: list[Track], group_idx: int) -> int:
    """Index after the last child of the group at ``group_idx``."""
    group = tracks[group_idx]
    if not group.is_group:
        return group_idx + 1
    gid = group.id
    i = group_idx + 1
    while i < len(tracks) and tracks[i].group_id == gid:
        i += 1
    return i


def _group_id_for_insert(tracks: list[Track], insert_idx: int) -> int:
    """Infer group_id when a track is placed at ``insert_idx``."""
    if insert_idx <= 0 or insert_idx > len(tracks):
        return 0
    prev = tracks[insert_idx - 1]
    if prev.is_group:
        return prev.id
    if prev.group_id != 0:
        gid = prev.group_id
        j = insert_idx - 1
        while j >= 0 and tracks[j].group_id == gid:
            j -= 1
        if j >= 0 and tracks[j].is_group and tracks[j].id == gid:
            return gid
    return 0


def _is_inside_group_block(tracks: list[Track], insert_idx: int) -> bool:
    for i, t in enumerate(tracks):
        if not t.is_group:
            continue
        end = _group_block_end(tracks, i)
        if i < insert_idx < end or insert_idx == i + 1:
            return True
    return False


def _reorder_tracks(tracks: list[Track], track_id: int, new_index: int
                    ) -> tuple[list[Track], dict[int, int]]:
    """Return reordered track list and {track_id: new_group_id} changes."""
    src_idx = next(i for i, t in enumerate(tracks) if t.id == track_id)
    track = tracks[src_idx]

    if track.is_group:
        block_end = _group_block_end(tracks, src_idx)
        block = tracks[src_idx:block_end]
        rest = tracks[:src_idx] + tracks[block_end:]
    else:
        block = [track]
        rest = tracks[:src_idx] + tracks[src_idx + 1:]

    insert_idx = new_index if new_index <= src_idx else new_index - len(block)
    insert_idx = max(0, min(insert_idx, len(rest)))

    if track.is_group and _is_inside_group_block(rest, insert_idx):
        for i, t in enumerate(rest):
            if t.is_group:
                end = _group_block_end(rest, i)
                if i < insert_idx < end or insert_idx == i + 1:
                    insert_idx = end
                    break

    new_tracks = rest[:insert_idx] + block + rest[insert_idx:]

    group_changes: dict[int, int] = {}
    for t in block:
        if t.is_group:
            continue
        idx = new_tracks.index(t)
        new_gid = _group_id_for_insert(new_tracks, idx)
        if t.group_id != new_gid:
            group_changes[t.id] = new_gid
    return new_tracks, group_changes


def _move_track_to_group(tracks: list[Track], track_id: int,
                         group_id: int) -> tuple[list[Track], int, int]:
    """Move track to end of group block; returns (tracks, old_group_id, old_index)."""
    track = next(t for t in tracks if t.id == track_id)
    old_gid = track.group_id
    src_idx = tracks.index(track)
    group_idx = next(i for i, t in enumerate(tracks) if t.id == group_id)
    block_end = _group_block_end(tracks, group_idx)

    new_list = tracks[:src_idx] + tracks[src_idx + 1:]
    if src_idx < block_end:
        block_end -= 1
    new_list.insert(block_end, track)
    return new_list, old_gid, src_idx


def _remove_track_from_group(tracks: list[Track], track_id: int
                             ) -> tuple[list[Track], int, int]:
    """Ungroup track and place it after the group block."""
    track = next(t for t in tracks if t.id == track_id)
    old_gid = track.group_id
    src_idx = tracks.index(track)
    if old_gid == 0:
        return tracks, 0, src_idx

    group_idx = next(i for i, t in enumerate(tracks) if t.id == old_gid)
    block_end = _group_block_end(tracks, group_idx)
    new_list = tracks[:src_idx] + tracks[src_idx + 1:]
    if src_idx < block_end:
        block_end -= 1
    new_list.insert(block_end, track)
    return new_list, old_gid, src_idx


class _LoaderSignals(QObject):
    loaded = Signal(str, int, int, int, int)  # path, track_id, frame, file_id, frames
    failed = Signal(str, str)                 # path, error


class _FileLoadTask(QRunnable):
    """Decodes an audio file in a worker thread (loadFile releases the GIL)."""

    def __init__(self, bridge: EngineBridge, path: str, track_id: int,
                 frame: int, signals: _LoaderSignals) -> None:
        super().__init__()
        self.bridge = bridge
        self.path = path
        self.track_id = track_id
        self.frame = frame
        self.signals = signals

    def run(self) -> None:
        try:
            file_id, frames = self.bridge.load_file(self.path)
        except Exception as exc:  # noqa: BLE001 - show any decode error
            self.signals.failed.emit(self.path, str(exc))
            return
        self.signals.loaded.emit(self.path, self.track_id, self.frame,
                                 file_id, frames)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PyDAW")
        self.resize(1280, 720)

        self.settings = QSettings("PyDAW", "PyDAW")
        self.restart_requested = False

        # Tooltips can be switched off globally (Settings -> Help): an
        # app-level filter swallows QEvent.ToolTip for every window.
        self._tooltip_suppressor = ToolTipSuppressor(self)
        saved_tips = str(self.settings.value("help/tooltips", "true")).lower()
        self._tooltips_enabled = True
        self._apply_tooltips_enabled(saved_tips not in ("false", "0"))
        self.project = Project()
        self.bridge = EngineBridge(
            sample_rate=int(self.settings.value("audio/sample_rate", 48000)),
            buffer_frames=int(self.settings.value("audio/buffer", 512)),
            device_id=int(self.settings.value("audio/device_id", 0)),
            api_name=str(self.settings.value("audio/api_name", "") or ""))
        self.project.sample_rate = self.bridge.sample_rate
        self.undo_stack = UndoStack(on_change=self._update_undo_actions)
        self._piano_rolls: dict[int, PianoRollDialog] = {}
        self._vst_editors: dict[tuple[int, int], VstEditorWindow] = {}
        self._selected_track_id: int | None = None
        # Proxy Track for the master FX chain (id = MASTER_FX_ID): lets the
        # FX panel, effect and automation handlers reuse the per-track
        # code path (its effects/automation_lanes alias the project lists).
        self._master_fx_track = Track(id=MASTER_FX_ID, name="Master")
        self._fx_panel_last_size = 200
        self._mixer: MixerWindow | None = None
        self._folded_groups: set[int] = set()
        self._folded_auto: set[int] = set()  # tracks with collapsed lanes
        # True while playback drives widget values from automation lanes;
        # on stop the widgets are restored from the model once.
        self._auto_feedback_active = False
        # TWEAK mode: after the user touches any knob/slider, a confirmation
        # dialog offers to create an automation lane for that parameter.
        self._tweak_mode = False
        self._tweak_dialog_open = False
        self._tweak_candidate: tuple[int, str, int, int, str] | None = None
        self._tweak_timer = QTimer(self)
        self._tweak_timer.setSingleShot(True)
        self._tweak_timer.setInterval(500)
        self._tweak_timer.timeout.connect(self._show_tweak_dialog)
        # Continuous-control undo: consecutive changes of one parameter
        # coalesce into a single gesture, finalized after a short pause.
        self._param_gesture: dict | None = None
        self._param_gesture_timer = QTimer(self)
        self._param_gesture_timer.setSingleShot(True)
        self._param_gesture_timer.setInterval(500)
        self._param_gesture_timer.timeout.connect(self._finish_param_gesture)
        # Any push/undo/redo first flushes the pending gesture so history
        # order matches what the user actually did (e.g. Ctrl+Z right
        # after dragging a knob undoes that knob move, not the previous
        # command).
        self.undo_stack.set_flush(self._finish_param_gesture)
        # Releasing any slider/knob closes the gesture at once, so two
        # quick drags of the same control are two undo steps, not one.
        knob.set_gesture_end_hook(self._finish_param_gesture)
        self._loader_signals = _LoaderSignals()
        self._loader_signals.loaded.connect(self._on_file_loaded)
        self._loader_signals.failed.connect(self._on_file_failed)
        self._pending_loads = 0
        self._vst_load_progress_depth = 0

        # -- layout ----------------------------------------------------
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.transport = TransportBar()
        root.addWidget(self.transport)

        body_widget = QWidget()
        body = QHBoxLayout(body_widget)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self.track_panel = TrackPanel(self.project, track_height=96)
        body.addWidget(self.track_panel)

        self.timeline = TimelineView(self.project, self.bridge)
        body.addWidget(self.timeline, 1)
        self._sync_master_proxy()
        self.timeline.master_track = self._master_fx_track
        self.track_panel.master_track = self._master_fx_track
        # Master strip lives inside the scrollable panel, right after the
        # tracks, so its automation lanes appear directly below it.
        self.master_header = MasterTrackHeader()
        self.track_panel.set_master_header(self.master_header)

        self.fx_panel = FxPanel(self.bridge)
        self.fx_panel.effect_added.connect(self._on_effect_added)
        self.fx_panel.vst_add_requested.connect(self._on_vst_effect_pick)
        self.fx_panel.effect_removed.connect(self._on_effect_removed)
        self.fx_panel.effect_moved.connect(self._on_effect_moved)
        self.fx_panel.instrument_remove_requested.connect(
            self._on_instrument_remove)
        self.fx_panel.param_changed.connect(self._on_effect_param)
        self.fx_panel.vst_param_changed.connect(self._on_vst_effect_param)
        self.fx_panel.vst_editor_requested.connect(self.open_vst_editor)
        self.fx_panel.vst_plugin_requested.connect(self._on_vst_plugin_pick)
        self.fx_panel.vst_instrument_editor_requested.connect(
            lambda tid: self.open_vst_editor(tid, -1))

        self._body_splitter = QSplitter(Qt.Vertical)
        self._body_splitter.addWidget(body_widget)
        self._body_splitter.addWidget(self.fx_panel)
        self._body_splitter.setStretchFactor(0, 1)
        self._body_splitter.setStretchFactor(1, 0)
        self._body_splitter.setSizes([520, self._fx_panel_last_size])

        root.addWidget(self._body_splitter, 1)
        self.setCentralWidget(central)

        # -- transport signals -------------------------------------------
        self.transport.play_clicked.connect(self.toggle_play)
        self.transport.stop_clicked.connect(self.stop_playback)
        self.transport.stop_double_clicked.connect(self.rewind_to_start)
        self.master_header.gain_changed.connect(self._on_master_gain)
        self.master_header.clicked.connect(self._select_master_for_fx)
        self.master_header.context_menu_requested.connect(
            self._show_master_context_menu)
        self.transport.bpm_changed.connect(self._on_bpm_changed)
        self.transport.metronome_toggled.connect(self._on_metronome)
        self.transport.loop_toggled.connect(self._on_loop_toggled)
        self.transport.snap_changed.connect(self._on_snap_changed)
        self.transport.snap_toggled.connect(self._on_snap_toggled)
        self.transport.follow_toggled.connect(self._on_follow_toggled)
        self.transport.mixer_clicked.connect(self.open_mixer)
        self.transport.tool_selected.connect(self._on_tool_selected)
        self.transport.slice_mode_toggled.connect(self._on_slice_mode_toggled)
        self.transport.tweak_toggled.connect(self._on_tweak_toggled)
        self.transport.time_display_mode_changed.connect(
            self._on_time_display_mode_changed)

        bars_mode = self.settings.value("transport/bars_mode", False)
        self.transport.set_bars_mode(str(bars_mode).lower() in ("true", "1"))

        # -- timeline signals ---------------------------------------------
        self.timeline.file_dropped.connect(self.add_file_to_track)
        self.timeline.clip_moved.connect(self._on_clip_moved)
        self.timeline.clip_track_changed.connect(self._on_clip_track_changed)
        self.timeline.clip_trimmed.connect(self._on_clip_trimmed)
        self.timeline.clip_fades_changed.connect(self._on_clip_fades)
        self.timeline.clip_properties_requested.connect(
            self._on_clip_properties)
        self.timeline.clip_rename_requested.connect(self._on_clip_rename)
        self.timeline.clip_stretched.connect(self._on_clip_stretched)
        self.timeline.clip_split_requested.connect(self._on_clip_split)
        self.timeline.clip_slice_requested.connect(self._on_clip_slice)
        self.timeline.clip_remove_requested.connect(self._on_clip_remove)
        self.timeline.clips_remove_requested.connect(self._on_clips_remove)
        self.timeline.clips_join_requested.connect(self._on_clips_join)
        self.timeline.clips_duplicate_requested.connect(
            self._on_clips_duplicate)
        self.timeline.clips_copy_requested.connect(self._on_clips_copy)
        self.timeline.clips_paste_requested.connect(self._on_clips_paste)
        self.timeline.loop_region_changed.connect(self._on_loop_region)
        self.timeline.midi_clip_moved.connect(self._on_midi_clip_moved)
        self.timeline.midi_clip_resized.connect(self._on_midi_clip_resized)
        self.timeline.midi_clip_trimmed.connect(self._on_midi_clip_trimmed)
        self.timeline.midi_clip_open_requested.connect(self.open_piano_roll)
        self.timeline.midi_clip_create_requested.connect(self.add_midi_clip)
        self.timeline.seek_requested.connect(self._on_seek)
        # Right-click "Add ... track" menus always append at the end
        # (before the master strip), ignoring the selection anchor.
        self.timeline.add_track_requested.connect(
            lambda: self.add_track(anchored=False))
        self.timeline.add_midi_track_requested.connect(
            lambda: self.add_midi_track(anchored=False))
        self.timeline.add_group_track_requested.connect(self.add_group_track)
        # Two-way scroll link. Timeline scrollbar values include the ruler
        # offset (scene starts at -RULER_HEIGHT), while the panel's content
        # starts with a top margin. Convert between the two spaces so rows
        # stay pixel-aligned at every scroll position.
        self.timeline.verticalScrollBar().valueChanged.connect(
            self._sync_track_panel_scroll_from_timeline)
        self.track_panel.scrolled.connect(
            self._sync_timeline_scroll_from_track_panel)
        # The timeline viewport is shorter than the panel's (horizontal
        # scrollbar), so its scroll range is larger; pad the panel bottom
        # to match, otherwise rows misalign when scrolled to the bottom.
        self._panel_extra_pad = 0
        self.timeline.verticalScrollBar().rangeChanged.connect(
            self._sync_scroll_ranges)
        self.track_panel.verticalScrollBar().rangeChanged.connect(
            self._sync_scroll_ranges)

        # -- track panel signals ---------------------------------------------
        self.track_panel.add_track_requested.connect(
            lambda: self.add_track(anchored=False))
        self.track_panel.add_midi_track_requested.connect(
            lambda: self.add_midi_track(anchored=False))
        self.track_panel.add_group_track_requested.connect(self.add_group_track)
        self.track_panel.track_reordered.connect(self._on_track_reordered)
        self.track_panel.group_fold_toggled.connect(self._on_group_fold_toggled)
        self.track_panel.move_to_group.connect(self._on_move_to_group)
        self.track_panel.remove_from_group.connect(self._on_remove_from_group)
        self.track_panel.gain_changed.connect(self._on_gain)
        self.track_panel.pan_changed.connect(self._on_pan)
        self.track_panel.width_changed.connect(self._on_stereo_width)
        self.track_panel.mute_toggled.connect(self._on_mute)
        self.track_panel.solo_toggled.connect(self._on_solo)
        self.track_panel.name_changed.connect(self._on_name)
        self.track_panel.properties_requested.connect(self._on_track_properties)
        self.track_panel.rename_requested.connect(self._rename_track_dialog)
        self.track_panel.remove_requested.connect(self.remove_track)
        self.track_panel.delete_selected_requested.connect(
            self._delete_selected_tracks)
        self.track_panel.vst_plugin_requested.connect(self._on_vst_plugin_pick)
        self.track_panel.vst_editor_requested.connect(
            lambda tid: self.open_vst_editor(tid, -1))
        self.track_panel.track_clicked.connect(self._select_track_for_fx)
        # A header click switches the "current selection" to the track:
        # drop the clip selection so it cannot shadow the header as the
        # insertion anchor for Ctrl+T / Ctrl+Shift+T (clip selection
        # survives rebuilds now, so stale selections would linger).
        self.track_panel.track_clicked.connect(
            lambda _tid: self.timeline._scene.clearSelection())
        self.track_panel.add_automation.connect(self._on_add_automation_lane)
        self.track_panel.add_automation_vst_pick.connect(
            self._on_add_automation_vst_pick)
        self.track_panel.automation_remove_requested.connect(
            self._on_remove_automation_lane)
        self.track_panel.automation_lane_clicked.connect(
            self._on_automation_lane_clicked)
        self.track_panel.auto_fold_toggled.connect(self._on_auto_fold_toggled)
        self.timeline.automation_points_changed.connect(
            self._on_automation_points_changed)

        self._menu_actions: dict[str, QAction] = {}
        self._default_shortcuts: dict[str, QKeySequence] = {}
        self._menu_title_keys: list[tuple[QMenu, str]] = []
        self._menu_label_keys: dict[str, str] = {}
        self._build_menu()

        # UI update timer (~60 Hz): playhead + meters.
        # Playhead smoothing state (see _smooth_playhead).
        self._playhead_smoother = PlayheadSmoother()
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()

        if not ENGINE_AVAILABLE:
            self.statusBar().showMessage(
                tr("C++ engine not built - running in silent UI mode"))
        elif not self.bridge.audio_ok:
            self.statusBar().showMessage(tr("No audio output device found"))
        elif getattr(self.bridge, "used_fallback_device", False):
            self.statusBar().showMessage(
                tr("Saved audio device unavailable - using default output"),
                8000)
        else:
            self._show_audio_status()

        # Start with one audio and one MIDI track for convenience.
        self.add_track()
        self.add_midi_track()
        self.undo_stack.clear()
        self.bridge.set_tempo(self.project.bpm)

    # -- menu ------------------------------------------------------------

    def _add_menu(self, title_key: str) -> QMenu:
        menu = self.menuBar().addMenu(tr(title_key))
        self._menu_title_keys.append((menu, title_key))
        return menu

    def _build_menu(self) -> None:
        file_menu = self._add_menu("&File")
        self._add_action(file_menu, "&New project", QKeySequence.New,
                         self.new_project, "file.new")
        self._add_action(file_menu, "&Open project...", QKeySequence.Open,
                         self.open_project, "file.open")
        self._add_action(file_menu, "&Save project", QKeySequence.Save,
                         self.save_project, "file.save")
        self._add_action(file_menu, "Save project &as...",
                         QKeySequence.SaveAs, self.save_project_as,
                         "file.save_as")
        file_menu.addSeparator()
        self._add_action(file_menu, "&Import audio file...",
                         QKeySequence("Ctrl+I"), self.import_audio_dialog,
                         "file.import_audio")
        self._add_action(file_menu, "&Export...",
                         QKeySequence("Ctrl+Shift+E"), self.export_mix,
                         "file.export_wav")
        file_menu.addSeparator()
        self._add_action(file_menu, "&Settings...", QKeySequence("Ctrl+,"),
                         self.open_settings, "file.settings")
        file_menu.addSeparator()
        self._add_action(file_menu, "E&xit", QKeySequence("Ctrl+Q"),
                         self.close, "file.exit")

        edit_menu = self._add_menu("&Edit")
        self.undo_action = self._add_action(
            edit_menu, "&Undo", QKeySequence.Undo, self.undo_stack.undo,
            "edit.undo")
        self.redo_action = self._add_action(
            edit_menu, "&Redo", QKeySequence.Redo, self.undo_stack.redo,
            "edit.redo")
        edit_menu.addSeparator()
        self._add_action(edit_menu, "Split at &playhead",
                         QKeySequence("Ctrl+E"), self.split_at_playhead,
                         "edit.split_playhead")
        self.mouse_tool_action = self._add_action(
            edit_menu, "&Mouse tool", QKeySequence("1"),
            lambda _checked=False: self._on_tool_selected("mouse"),
            "edit.mouse_tool")
        self.mouse_tool_action.setCheckable(True)
        self.mouse_tool_action.setChecked(True)
        self.select_tool_action = self._add_action(
            edit_menu, "&Selection tool", QKeySequence("2"),
            lambda _checked=False: self._on_tool_selected("select"),
            "edit.selection_tool")
        self.select_tool_action.setCheckable(True)
        self.slice_action = self._add_action(
            edit_menu, "&Slice tool", QKeySequence("C"),
            self._toggle_slice_mode, "edit.slice_tool")
        self.slice_action.setCheckable(True)
        self._update_undo_actions()

        # Selection tools live in their own menu between Edit and Track.
        select_menu = self._add_menu("&Select")
        self._add_action(select_menu, "Select &all",
                         QKeySequence("Ctrl+A"),
                         self._menu_select_all, "select.all")
        self._add_action(select_menu, "&Deselect all",
                         QKeySequence("Ctrl+Shift+A"),
                         self._menu_deselect_all, "select.none")
        self._add_action(select_menu, "&Invert selection",
                         QKeySequence("Ctrl+Shift+I"),
                         self._menu_invert_selection, "select.invert")
        self._add_action(select_menu, "Select all on &track",
                         QKeySequence("Ctrl+Alt+A"),
                         self._menu_select_all_on_track, "select.track")

        track_menu = self._add_menu("&Track")
        # QAction.triggered passes checked=False positionally: wrap in
        # lambdas so it is not mistaken for the `anchored` flag.
        self._add_action(track_menu, "&Add audio track",
                         QKeySequence("Ctrl+T"),
                         lambda: self.add_track(), "track.add_audio")
        self._add_action(track_menu, "Add &MIDI track",
                         QKeySequence("Ctrl+Shift+T"),
                         lambda: self.add_midi_track(), "track.add_midi")
        self._add_action(track_menu, "Add &group track",
                         QKeySequence("Ctrl+Shift+G"),
                         self.add_group_track, "track.add_group")
        self._add_action(track_menu, "&Group selected tracks",
                         QKeySequence("Ctrl+G"),
                         self.group_selected_tracks, "track.group_selected")
        self._add_action(track_menu, "&Rename track",
                         QKeySequence("Ctrl+Shift+R"),
                         self._rename_selected_track, "track.rename")

        view_menu = self._add_menu("&View")
        self._add_action(view_menu, "&Mixer",
                         QKeySequence("Ctrl+Shift+M"),
                         self.open_mixer, "view.mixer")
        self.fx_panel_action = self._add_action(
            view_menu, "&FX panel", QKeySequence("Ctrl+B"),
            self._toggle_fx_panel, "view.fx_panel")
        self.fx_panel_action.setCheckable(True)
        self.fx_panel_action.setChecked(True)
        view_menu.addSeparator()
        # Zoom in/out get several key bindings: the "+" key differs across
        # layouts (main row needs Shift, numpad sends its own code), so
        # Ctrl+"+"/Ctrl+"=" and their Shift variants all must work.
        self._add_action(view_menu, "Zoom &in",
                         ["Ctrl++", "Ctrl+=", "Ctrl+Shift+="],
                         lambda: self.timeline.zoom(1.25), "view.zoom_in")
        self._add_action(view_menu, "Zoom &out",
                         ["Ctrl+-", "Ctrl+Shift+-"],
                         lambda: self.timeline.zoom(0.8), "view.zoom_out")
        self._add_action(view_menu, "Zoom to &fit project",
                         QKeySequence("Ctrl+0"), self.timeline.zoom_to_fit,
                         "view.zoom_fit")

        transport_menu = self._add_menu("T&ransport")
        self._add_action(transport_menu, "&Play/Pause",
                         QKeySequence(Qt.Key_Space),
                         self.toggle_play, "transport.play")
        self._add_action(transport_menu, "&Stop", QKeySequence("Ctrl+."),
                         self.stop_playback, "transport.stop")
        self._add_action(transport_menu, "&Metronome",
                         QKeySequence("Ctrl+M"),
                         lambda: self.transport.metro_btn.toggle(),
                         "transport.metronome")
        self._add_action(transport_menu, "&Loop", QKeySequence("Ctrl+L"),
                         self._on_loop_shortcut, "transport.loop")

        help_menu = self._add_menu("&Help")
        self._add_action(help_menu, "Keyboard &shortcuts",
                         QKeySequence(Qt.Key_F1),
                         self.show_shortcuts_dialog, "help.shortcuts")

        exit_slice = QAction(self)
        exit_slice.setShortcut(QKeySequence(Qt.Key_Escape))
        exit_slice.triggered.connect(self._exit_slice_mode)
        self.addAction(exit_slice)

        self._apply_shortcut_settings()

    def _retranslate_ui(self) -> None:
        """Apply the active language to all live UI chrome."""
        for menu, key in self._menu_title_keys:
            menu.setTitle(tr(key))
        for action_id, key in self._menu_label_keys.items():
            action = self._menu_actions.get(action_id)
            if action is not None:
                action.setText(tr(key))
        self.transport.retranslate()
        self.track_panel.retranslate()
        self.fx_panel.retranslate()
        if self._mixer is not None:
            self._mixer.retranslate()
        for dlg in self._piano_rolls.values():
            dlg.retranslate()
        if ENGINE_AVAILABLE and self.bridge.audio_ok:
            self._show_audio_status()

    def _show_audio_status(self, timeout_ms: int = 0) -> None:
        msg = trf("Audio: {api}, {rate} Hz, buffer {buffer}",
                  api=self.bridge.api_name or "auto",
                  rate=self.bridge.sample_rate,
                  buffer=self.bridge.buffer_frames)
        self.statusBar().showMessage(msg, timeout_ms)

    def _add_action(self, menu, label_key, shortcut, slot,
                    action_id: str | None = None) -> QAction:
        """`shortcut` is a QKeySequence/str or a list of them (aliases for
        the same action, e.g. Ctrl++ and Ctrl+= for zoom in)."""
        action = QAction(tr(label_key), self)
        if isinstance(shortcut, (list, tuple)):
            action.setShortcuts([QKeySequence(s) for s in shortcut])
            primary = QKeySequence(shortcut[0]) if shortcut else QKeySequence()
        elif shortcut:
            action.setShortcut(shortcut)
            primary = QKeySequence(shortcut)
        else:
            primary = QKeySequence()
        action.triggered.connect(slot)
        menu.addAction(action)
        if action_id:
            self._menu_actions[action_id] = action
            self._menu_label_keys[action_id] = label_key
            self._default_shortcuts[action_id] = primary
        return action

    def _apply_shortcut_settings(self) -> None:
        for action_id, action in self._menu_actions.items():
            stored = self.settings.value(f"keys/{action_id}")
            if stored:
                seq = QKeySequence(str(stored))
                if not seq.isEmpty():
                    action.setShortcut(seq)

    def _update_undo_actions(self) -> None:
        if hasattr(self, "undo_action"):
            # A pending (not yet finalized) knob/slider gesture is
            # undoable too: undo flushes it first, so the action must be
            # enabled immediately - otherwise Ctrl+Z right after a tweak
            # is silently ignored ("works only the second time").
            pending = getattr(self, "_param_gesture", None) is not None
            self.undo_action.setEnabled(self.undo_stack.can_undo or pending)
            self.redo_action.setEnabled(self.undo_stack.can_redo)

    def show_shortcuts_dialog(self) -> None:
        ShortcutsDialog(self).exec()

    # -- Select menu ---------------------------------------------------------

    @staticmethod
    def _focused_text_widget():
        """The focused widget when it handles Ctrl+A itself (text entry)."""
        from PySide6.QtWidgets import (QAbstractSpinBox, QApplication,
                                       QLineEdit, QPlainTextEdit, QTextEdit)
        w = QApplication.focusWidget()
        if isinstance(w, (QLineEdit, QTextEdit, QPlainTextEdit,
                          QAbstractSpinBox)):
            return w
        return None

    def _menu_select_all(self) -> None:
        # The window-level Ctrl+A shortcut fires before the focused
        # widget sees the key: forward select-all to text fields so
        # typing workflows keep working.
        w = self._focused_text_widget()
        if w is not None:
            w.selectAll()
            return
        self.timeline.select_all_clips()

    def _menu_deselect_all(self) -> None:
        self.timeline.deselect_all_clips()

    def _menu_invert_selection(self) -> None:
        self.timeline.invert_clip_selection()

    def _menu_select_all_on_track(self) -> None:
        # Tracks of the selected clips; fall back to header selection.
        track_ids = self.timeline.selected_clip_track_ids()
        if not track_ids:
            track_ids = set(self.track_panel.selected_track_ids())
        if track_ids:
            self.timeline.select_clips_on_tracks(track_ids)

    def keyPressEvent(self, event) -> None:
        """Selection hotkeys work everywhere in the main window.

        The timeline handles Delete / Ctrl+A/C/V/D itself, but only while
        it has keyboard focus. Unhandled key presses bubble up from child
        widgets (transport, track panel, ...) to the main window, so
        forwarding them here keeps the selection tools active no matter
        which part of the main window was clicked last. Other program
        windows (Settings, piano roll, mixer, VST editors) are separate
        top-level windows — their key events never reach the main window,
        so their own shortcuts stay untouched. Widgets that consume these
        keys themselves (text fields, the track panel's Delete for
        selected tracks) accept the event, and it never gets here.
        """
        key = letter_key(event)
        ctrl = event.modifiers() & Qt.ControlModifier
        # F (zoom to fit selection/project) and bare +/-/= (zoom) are
        # timeline keys too: forward them so zooming works no matter
        # which main-window widget was clicked last. Text fields accept
        # these keys themselves, so typing is never intercepted.
        zoom_keys = (Qt.Key_F, Qt.Key_Plus, Qt.Key_Equal, Qt.Key_Minus)
        if key == Qt.Key_Delete or (
                ctrl and key in (Qt.Key_A, Qt.Key_C, Qt.Key_V, Qt.Key_D,
                                 Qt.Key_J)) \
                or (not event.modifiers() and key in zoom_keys):
            self.timeline.keyPressEvent(event)
            if event.isAccepted():
                return
        super().keyPressEvent(event)

    def _on_time_display_mode_changed(self, bars_mode: bool) -> None:
        self.settings.setValue("transport/bars_mode", bars_mode)

    def _on_slice_mode_toggled(self, on: bool) -> None:
        self.timeline.set_slice_mode(on)
        if on:
            self.timeline.set_tool("mouse")
            self.transport.set_slice_mode(True)
        else:
            self.transport.set_tool(self.timeline.tool)
        if hasattr(self, "slice_action"):
            self.slice_action.blockSignals(True)
            self.slice_action.setChecked(on)
            self.slice_action.blockSignals(False)
        self._sync_tool_actions()

    def _sync_tool_actions(self) -> None:
        if not hasattr(self, "mouse_tool_action"):
            return
        mouse_on = not self.timeline.slice_mode and self.timeline.tool == "mouse"
        select_on = not self.timeline.slice_mode and self.timeline.tool == "select"
        for action, checked in ((self.mouse_tool_action, mouse_on),
                                (self.select_tool_action, select_on)):
            action.blockSignals(True)
            action.setChecked(checked)
            action.blockSignals(False)

    def _on_tool_selected(self, tool: str) -> None:
        self.timeline.set_slice_mode(False)
        self.timeline.set_tool(tool)
        self.transport.set_tool(self.timeline.tool)
        if hasattr(self, "slice_action"):
            self.slice_action.blockSignals(True)
            self.slice_action.setChecked(False)
            self.slice_action.blockSignals(False)
        self._sync_tool_actions()

    def _toggle_slice_mode(self) -> None:
        self._on_slice_mode_toggled(not self.timeline.slice_mode)

    def _exit_slice_mode(self) -> None:
        if self.timeline.slice_mode:
            self._on_slice_mode_toggled(False)

    def split_at_playhead(self) -> None:
        playhead = self.timeline.playhead_frame()
        playhead_beat = self.project.frames_to_beats(playhead)
        targets = self.timeline._selected_clip_refs()
        if not targets:
            targets = self._clips_at_playhead(playhead, playhead_beat)
        commands: list[Command] = []
        for track_id, clip_id, is_midi in targets:
            cmd = self._make_split_command(
                track_id, clip_id, is_midi,
                playhead if not is_midi else playhead_beat)
            if cmd is not None:
                commands.append(cmd)
        if not commands:
            return
        if len(commands) == 1:
            self.undo_stack.push(commands[0])
        else:
            self.undo_stack.push(
                CompositeCommand(commands, "Split at playhead"))

    def _clips_at_playhead(self, frame: int, beat: float) -> list[tuple[int, int, bool]]:
        refs: list[tuple[int, int, bool]] = []
        for track in self.project.tracks:
            for clip in track.clips:
                if clip.start_frame < frame < clip.end_frame:
                    refs.append((track.id, clip.id, False))
            for mclip in track.midi_clips:
                if mclip.start_beat < beat < mclip.end_beat:
                    refs.append((track.id, mclip.id, True))
        return refs

    def _make_split_command(self, track_id: int, clip_id: int,
                            is_midi: bool, position) -> Command | None:
        if is_midi:
            return self._make_midi_split_command(track_id, clip_id, position)
        return self._make_audio_split_command(track_id, clip_id, position)

    @staticmethod
    def _audio_split_name(track: Track, clip: Clip) -> str:
        base = clip.display_name
        head, sep, tail = base.rpartition("_")
        if sep and tail.isdigit() and int(tail) >= 2:
            base = head

        next_suffix = 2
        for other in track.clips:
            name = other.display_name
            if name == base:
                next_suffix = max(next_suffix, 2)
                continue
            head, sep, tail = name.rpartition("_")
            if sep and head == base and tail.isdigit() and int(tail) >= 2:
                next_suffix = max(next_suffix, int(tail) + 1)
        return f"{base}_{next_suffix}"

    def _make_audio_split_command(self, track_id: int, clip_id: int,
                                  frame: int) -> Command | None:
        track = self.project.track_by_id(track_id)
        clip = track.clip_by_id(clip_id) if track else None
        if not clip or not (clip.start_frame < frame < clip.end_frame):
            return None
        left_len = frame - clip.start_frame
        orig = (clip.start_frame, clip.source_offset, clip.length,
                clip.fade_in, clip.fade_out)
        right = Clip(id=next_id(), file_path=clip.file_path,
                     file_id=clip.file_id, start_frame=frame,
                     length=clip.length - left_len,
                     source_offset=clip.source_offset + left_len,
                     fade_out=clip.fade_out,
                     name=self._audio_split_name(track, clip), warp=clip.warp,
                     stretch_ratio=clip.stretch_ratio,
                     auto_warp=clip.auto_warp)

        def do() -> None:
            clip.length = left_len
            clip.fade_out = 0
            self.bridge.trim_clip(track_id, clip)
            self.bridge.set_clip_fades(track_id, clip)
            track.clips.append(right)
            self.bridge.add_clip(track_id, right)
            self._refresh_all()

        def undo() -> None:
            (clip.start_frame, clip.source_offset, clip.length,
             clip.fade_in, clip.fade_out) = orig
            self.bridge.trim_clip(track_id, clip)
            self.bridge.set_clip_fades(track_id, clip)
            if right in track.clips:
                track.clips.remove(right)
            self.bridge.remove_clip(track_id, right.id)
            self._refresh_all()

        return Command(do, undo, "Split clip")

    def _make_midi_split_command(self, track_id: int, clip_id: int,
                                 split_beat: float) -> Command | None:
        track = self.project.track_by_id(track_id)
        clip = track.midi_clip_by_id(clip_id) if track else None
        if not clip or not (clip.start_beat < split_beat < clip.end_beat):
            return None
        split_offset = split_beat - clip.start_beat
        split_rel = clip.source_start_beat + split_offset
        orig_len = clip.length_beats
        orig_source_start = clip.source_start_beat
        orig_notes = [dataclasses.replace(n) for n in clip.notes]

        right_notes: list[MidiNote] = []
        left_updates: list[tuple[MidiNote, MidiNote]] = []
        left_removes: list[MidiNote] = []

        for note in clip.notes:
            note_end = note.start_beat + note.length_beats
            if note_end <= split_rel + 1e-12:
                continue
            if note.start_beat >= split_rel - 1e-12:
                right_notes.append(
                    MidiNote(id=next_id(), pitch=note.pitch,
                             start_beat=note.start_beat,
                             length_beats=note.length_beats,
                             velocity=note.velocity))
                left_removes.append(note)
            else:
                new_len = split_rel - note.start_beat
                if new_len > 1e-12:
                    left_updates.append(
                        (note, dataclasses.replace(
                            note, length_beats=new_len)))
                    right_len = note_end - split_rel
                    if right_len > 1e-12:
                        right_notes.append(
                            MidiNote(id=next_id(), pitch=note.pitch,
                                     start_beat=split_rel,
                                     length_beats=right_len,
                                     velocity=note.velocity))
                else:
                    left_removes.append(note)

        right = MidiClip(id=next_id(), name=clip.name,
                         start_beat=split_beat,
                         length_beats=clip.end_beat - split_beat,
                         source_start_beat=split_rel,
                         notes=right_notes)

        def do() -> None:
            clip.length_beats = split_offset
            for note in left_removes:
                if note in clip.notes:
                    clip.notes.remove(note)
                self.bridge.remove_note(track_id, clip_id, note.id)
            for note, updated in left_updates:
                note.length_beats = updated.length_beats
                self.bridge.update_note(self.project, track_id, clip_id, note)
            self.bridge.resize_midi_clip(self.project, track_id, clip)
            track.midi_clips.append(right)
            self.bridge.add_midi_clip(self.project, track_id, right)
            self._refresh_all()

        def undo() -> None:
            if right in track.midi_clips:
                track.midi_clips.remove(right)
            self.bridge.remove_midi_clip(track_id, right.id)
            clip.length_beats = orig_len
            clip.source_start_beat = orig_source_start
            clip.notes.clear()
            clip.notes.extend(dataclasses.replace(n) for n in orig_notes)
            self.bridge.resize_midi_clip(self.project, track_id, clip)
            for note in clip.notes:
                self.bridge.add_note(self.project, track_id, clip_id, note)
            self._refresh_all()

        return Command(do, undo, "Split MIDI clip")

    def _on_clip_slice(self, track_id: int, clip_id: int,
                       position, is_midi: bool) -> None:
        cmd = self._make_split_command(track_id, clip_id, is_midi, position)
        if cmd is not None:
            self.undo_stack.push(cmd)

        if hasattr(self, "undo_action"):
            self.undo_action.setEnabled(self.undo_stack.can_undo)
            self.redo_action.setEnabled(self.undo_stack.can_redo)

    # -- join clips (Ctrl+J) --------------------------------------------------

    @staticmethod
    def _join_clips_overlap(prev_end, next_start, min_gap: float) -> bool:
        """True when ``next`` starts before ``prev`` ends by at least ``min_gap``.

        Intervals are half-open: touching at ``next_start == prev_end`` is
        fine. Up to one sample/frame of grid/beat conversion slop is allowed
        so clips placed flush in the UI are not mistaken for overlaps."""
        return next_start + min_gap < prev_end

    def _on_clips_join(self, refs) -> None:
        """Join the selected clips of ONE lane into a single clip.

        Rules: all selected clips must live on the same track (mixed
        lanes = no-op with a status message). Gaps between MIDI clips
        yield no new notes; gaps between audio clips are rendered as
        silence in the baked file."""
        if len(refs) < 2:
            self.statusBar().showMessage(
                tr("Join: select at least two clips on one lane"), 4000)
            return
        track_ids = {track_id for track_id, _, _ in refs}
        if len(track_ids) != 1:
            self.statusBar().showMessage(
                tr("Join: all selected clips must be on the same lane"), 4000)
            return
        track = self.project.track_by_id(next(iter(track_ids)))
        if track is None:
            return
        is_midi = refs[0][2]
        clip_ids = [clip_id for _, clip_id, _ in refs]
        cmd = (self._make_join_midi_command(track, clip_ids) if is_midi
               else self._make_join_audio_command(track, clip_ids))
        if cmd is not None:
            self.undo_stack.push(cmd)

    def _make_join_audio_command(self, track, clip_ids) -> Command | None:
        clips = [c for cid in clip_ids
                 for c in [track.clip_by_id(cid)] if c is not None]
        if len(clips) < 2:
            return None
        clips.sort(key=lambda c: c.start_frame)
        for a, b in zip(clips, clips[1:]):
            if self._join_clips_overlap(a.end_frame, b.start_frame, 1.0):
                self.statusBar().showMessage(
                    tr("Join: overlapping clips cannot be joined"), 4000)
                return None
        try:
            wav_path = render_join_wav(clips, self.project.sample_rate)
            file_id, frames = self.bridge.load_file(wav_path)
        except Exception as exc:  # noqa: BLE001 - show any render error
            QMessageBox.warning(self, tr("Join clips"),
                                trf("Could not render the joined audio:\n{err}",
                                    err=exc))
            return None
        base = clips[0].start_frame
        total = max(c.end_frame for c in clips) - base
        joined = Clip(id=next_id(), file_path=wav_path, file_id=file_id,
                      start_frame=base, length=min(total, frames),
                      name=clips[0].display_name, warp=NEW_CLIP_WARP,
                      stretch_ratio=1.0, auto_warp=False)
        track_id = track.id

        def do() -> None:
            for c in clips:
                if c in track.clips:
                    track.clips.remove(c)
                self.bridge.remove_clip(track_id, c.id)
            track.clips.append(joined)
            self.bridge.add_clip(track_id, joined)
            self._refresh_all()

        def undo() -> None:
            if joined in track.clips:
                track.clips.remove(joined)
            self.bridge.remove_clip(track_id, joined.id)
            for c in clips:
                track.clips.append(c)
                self.bridge.add_clip(track_id, c)
            self._refresh_all()

        return Command(do, undo, "Join clips")

    def _make_join_midi_command(self, track, clip_ids) -> Command | None:
        clips = [c for cid in clip_ids
                 for c in [track.midi_clip_by_id(cid)] if c is not None]
        if len(clips) < 2:
            return None
        clips.sort(key=lambda c: c.start_beat)
        beat_slop = 1.0 / self.project.frames_per_beat
        for a, b in zip(clips, clips[1:]):
            if self._join_clips_overlap(a.end_beat, b.start_beat, beat_slop):
                self.statusBar().showMessage(
                    tr("Join: overlapping clips cannot be joined"), 4000)
                return None
        base = clips[0].start_beat
        end = max(c.end_beat for c in clips)

        # Only the audible part of every clip carries over: notes are
        # trimmed to their clip window, so gaps between the source clips
        # stay empty in the joined clip.
        notes: list[MidiNote] = []
        for c in clips:
            shift = c.start_beat - c.source_start_beat
            for n in c.notes:
                abs_start = shift + n.start_beat
                abs_end = abs_start + n.length_beats
                vis_start = max(abs_start, c.start_beat)
                vis_end = min(abs_end, c.end_beat)
                if vis_end - vis_start <= 1e-9:
                    continue
                notes.append(MidiNote(id=next_id(), pitch=n.pitch,
                                      start_beat=vis_start - base,
                                      length_beats=vis_end - vis_start,
                                      velocity=n.velocity))

        joined = MidiClip(id=next_id(), name=clips[0].name,
                          start_beat=base, length_beats=end - base,
                          notes=notes)
        track_id = track.id

        def do() -> None:
            for c in clips:
                self._close_piano_roll(c.id)
                if c in track.midi_clips:
                    track.midi_clips.remove(c)
                self.bridge.remove_midi_clip(track_id, c.id)
            track.midi_clips.append(joined)
            self.bridge.add_midi_clip(self.project, track_id, joined)
            self._refresh_all()

        def undo() -> None:
            self._close_piano_roll(joined.id)
            if joined in track.midi_clips:
                track.midi_clips.remove(joined)
            self.bridge.remove_midi_clip(track_id, joined.id)
            for c in clips:
                track.midi_clips.append(c)
                self.bridge.add_midi_clip(self.project, track_id, c)
            self._refresh_all()

        return Command(do, undo, "Join MIDI clips")

    # -- tracks (undoable) ---------------------------------------------------

    def _clip_anchor_insert(self) -> tuple[int, int] | None:
        """(insert index, group_id) for a new track.

        Exactly one clip selected: the track appears right below that
        clip's lane, inside the same group when the lane is grouped.
        Otherwise, exactly one track header selected: below that track
        (inheriting its group); for a *group* header the track is
        created as the first child inside that group."""
        refs = self.timeline._selected_clip_refs()
        if len(refs) == 1:
            track = self.project.track_by_id(refs[0][0])
            if track is not None and not track.is_group:
                return self.project.tracks.index(track) + 1, track.group_id
            return None
        if not refs:
            selected = self.track_panel.selected_track_ids()
            if len(selected) == 1:
                anchor = self.project.track_by_id(selected[0])
                if anchor is None:
                    return None
                if anchor.is_group:
                    # First child inside the selected group.
                    return self.project.tracks.index(anchor) + 1, anchor.id
                return (self.project.tracks.index(anchor) + 1,
                        anchor.group_id)
        return None

    def add_track(self, anchored: bool = True) -> None:
        """New audio track. `anchored=True` (the Ctrl+T hotkey) inserts
        below the selected clip/track when there is one; `anchored=False`
        (the right-click "Add audio track" menus) always appends at the
        end, right before the master strip."""
        track = Track(id=next_id(),
                      name=f"Track {len(self.project.tracks) + 1}")
        anchor = self._clip_anchor_insert() if anchored else None
        if anchor is not None:
            track.group_id = anchor[1]
            self._push_add_track(track, index=anchor[0])
        else:
            self._push_add_track(track)

    def add_midi_track(self, anchored: bool = True) -> None:
        track = Track(id=next_id(),
                      name=f"MIDI {len(self.project.tracks) + 1}",
                      type="midi",
                      instrument=Instrument(gain=0.0))
        anchor = self._clip_anchor_insert() if anchored else None
        if anchor is not None:
            track.group_id = anchor[1]
            self._push_add_track(track, index=anchor[0])
        else:
            self._push_add_track(track)

    def add_group_track(self) -> None:
        n = sum(1 for t in self.project.tracks if t.is_group) + 1
        track = Track(id=next_id(), name=f"Group {n}", type="group")
        self._push_add_track(track)

    def group_selected_tracks(self) -> None:
        selected_ids = self.track_panel.selected_track_ids()
        selected = [t for t in self.project.tracks
                    if t.id in selected_ids and not t.is_group]
        if not selected:
            return

        group = Track(
            id=next_id(),
            name=f"Group {sum(1 for t in self.project.tracks if t.is_group) + 1}",
            type="group")
        old_order = [t.id for t in self.project.tracks]
        old_groups = {t.id: t.group_id for t in self.project.tracks}
        selected_set = {t.id for t in selected}
        first_idx = min(self.project.tracks.index(t) for t in selected)

        without_selected = [t for t in self.project.tracks
                            if t.id not in selected_set]
        insert_idx = sum(1 for t in self.project.tracks[:first_idx]
                         if t.id not in selected_set)
        new_tracks = (
            without_selected[:insert_idx]
            + [group]
            + selected
            + without_selected[insert_idx:])
        new_order = [t.id for t in new_tracks]
        new_groups = dict(old_groups)
        for track in selected:
            new_groups[track.id] = group.id

        def apply(order_ids: list[int], groups: dict[int, int],
                  include_group: bool) -> None:
            group_was_present = self.project.track_by_id(group.id) is not None
            if include_group and self.project.track_by_id(group.id) is None:
                self.bridge.add_track(group, self.project)
            if not include_group and group_was_present:
                self.bridge.remove_track(group.id)
            objects: dict[int, Track] = {t.id: t for t in self.project.tracks}
            if include_group:
                objects[group.id] = group
            self.project.tracks = [objects[tid] for tid in order_ids
                                   if tid in objects]
            for tid, gid in groups.items():
                t = self.project.track_by_id(tid)
                if t and t.group_id != gid:
                    t.group_id = gid
                    self.bridge.set_track_group(tid, gid)
            self._selected_track_id = group.id if include_group else None
            self._refresh_all()
            if include_group:
                self.track_panel.set_selected_tracks([group.id] + selected_ids)

        self.undo_stack.push(Command(
            lambda: apply(new_order, new_groups, True),
            lambda: apply(old_order, old_groups, False),
            "Group tracks"))

    def _push_add_track(self, track: Track, index: int | None = None) -> None:
        def do() -> None:
            if index is None or index > len(self.project.tracks):
                self.project.tracks.append(track)
            else:
                self.project.tracks.insert(index, track)
            # add_track mirrors group_id into the engine when it is set.
            self.bridge.add_track(track, self.project)
            self._refresh_all()

        def undo() -> None:
            self.project.tracks.remove(track)
            self.bridge.remove_track(track.id)
            self._refresh_all()

        self.undo_stack.push(Command(do, undo, "Add track"))

    def _delete_selected_tracks(self, track_ids) -> None:
        ids = [tid for tid in track_ids if self.project.track_by_id(tid)]
        if not ids:
            return
        if len(ids) == 1:
            self.remove_track(ids[0])
            return
        for tid in reversed(ids):
            if self.project.track_by_id(tid):
                self.remove_track(tid)

    def remove_track(self, track_id: int) -> None:
        track = self.project.track_by_id(track_id)
        if track is None:
            return
        index = self.project.tracks.index(track)
        child_snapshots: list[tuple[int, int, int]] = []
        if track.is_group:
            for i, t in enumerate(self.project.tracks):
                if t.group_id == track_id:
                    child_snapshots.append((t.id, t.group_id, i))

        def do() -> None:
            self._close_vst_editors_for_track(track.id)
            for mclip in track.midi_clips:
                self._close_piano_roll(mclip.id)
            if track.is_group:
                for t in self.project.tracks:
                    if t.group_id == track_id:
                        t.group_id = 0
                        self.bridge.set_track_group(t.id, 0)
                self._folded_groups.discard(track_id)
            self.project.tracks.remove(track)
            self.bridge.remove_track(track.id)
            self._refresh_all()

        def undo() -> None:
            self.project.tracks.insert(index, track)
            self.bridge.add_track(track, self.project)
            if track.is_group:
                for cid, gid, _ in child_snapshots:
                    ct = self.project.track_by_id(cid)
                    if ct:
                        ct.group_id = gid
                        self.bridge.set_track_group(cid, gid)
            for clip in track.clips:
                self.bridge.add_clip(track.id, clip)
            for mclip in track.midi_clips:
                self.bridge.add_midi_clip(self.project, track.id, mclip)
            self._refresh_all()

        self.undo_stack.push(Command(do, undo, "Remove track"))

    def _on_track_reordered(self, track_id: int, new_index: int) -> None:
        old_order = [t.id for t in self.project.tracks]
        old_groups = {t.id: t.group_id for t in self.project.tracks}

        def apply(order_ids: list[int], groups: dict[int, int]) -> None:
            self.project.tracks = [
                self.project.track_by_id(tid) for tid in order_ids]
            for tid, gid in groups.items():
                t = self.project.track_by_id(tid)
                if t and t.group_id != gid:
                    t.group_id = gid
                    self.bridge.set_track_group(tid, gid)
            self._refresh_all()

        new_tracks, group_changes = _reorder_tracks(
            list(self.project.tracks), track_id, new_index)
        new_order = [t.id for t in new_tracks]
        new_groups = dict(old_groups)
        new_groups.update(group_changes)

        self.undo_stack.push(Command(
            lambda: apply(new_order, new_groups),
            lambda: apply(old_order, old_groups),
            "Reorder track"))

    def _on_group_fold_toggled(self, group_id: int) -> None:
        old_folded = set(self._folded_groups)

        def apply(folded: set[int]) -> None:
            self._folded_groups = set(folded)
            self._refresh_all()

        new_folded = set(old_folded)
        if group_id in new_folded:
            new_folded.discard(group_id)
        else:
            new_folded.add(group_id)

        self.undo_stack.push(Command(
            lambda: apply(new_folded),
            lambda: apply(old_folded),
            "Fold group"))

    def _on_auto_fold_toggled(self, track_id: int) -> None:
        """Collapse/expand the automation lanes of one track (view state)."""
        if track_id in self._folded_auto:
            self._folded_auto.discard(track_id)
        else:
            self._folded_auto.add(track_id)
        self._refresh_all()

    def _on_move_to_group(self, track_id: int, group_id: int) -> None:
        track = self.project.track_by_id(track_id)
        target = self.project.track_by_id(group_id)
        # Groups cannot be nested inside other groups.
        if (track is None or target is None or track.is_group
                or not target.is_group):
            return
        # When the clicked track is part of a multi-selection, the whole
        # selection moves into the group (in project order).
        selected = self.track_panel.selected_track_ids()
        if track_id in selected:
            move_ids = [t.id for t in self.project.tracks
                        if t.id in selected and not t.is_group
                        and t.id != group_id]
        else:
            move_ids = [track_id]
        move_ids = [tid for tid in move_ids
                    if self.project.track_by_id(tid).group_id != group_id]
        if not move_ids:
            return
        old_order = [t.id for t in self.project.tracks]
        old_groups = {t.id: t.group_id for t in self.project.tracks}

        def apply(order_ids: list[int], groups: dict[int, int]) -> None:
            self.project.tracks = [
                self.project.track_by_id(tid) for tid in order_ids]
            for tid, gid in groups.items():
                t = self.project.track_by_id(tid)
                if t and t.group_id != gid:
                    t.group_id = gid
                    self.bridge.set_track_group(tid, gid)
            self._refresh_all()

        new_tracks = list(self.project.tracks)
        # Reversed: group_id fields are only updated in apply(), so
        # _group_block_end() does not yet count already-inserted tracks;
        # inserting back-to-front at the same boundary keeps the
        # selection's original order.
        for tid in reversed(move_ids):
            new_tracks, _, _ = _move_track_to_group(new_tracks, tid, group_id)
        new_order = [t.id for t in new_tracks]
        new_groups = dict(old_groups)
        for tid in move_ids:
            new_groups[tid] = group_id

        self.undo_stack.push(Command(
            lambda: apply(new_order, new_groups),
            lambda: apply(old_order, old_groups),
            "Move to group" if len(move_ids) == 1
            else f"Move {len(move_ids)} tracks to group"))

    def _on_remove_from_group(self, track_id: int) -> None:
        old_order = [t.id for t in self.project.tracks]
        old_groups = {t.id: t.group_id for t in self.project.tracks}

        def apply(order_ids: list[int], groups: dict[int, int]) -> None:
            self.project.tracks = [
                self.project.track_by_id(tid) for tid in order_ids]
            for tid, gid in groups.items():
                t = self.project.track_by_id(tid)
                if t and t.group_id != gid:
                    t.group_id = gid
                    self.bridge.set_track_group(tid, gid)
            self._refresh_all()

        new_tracks, _, _ = _remove_track_from_group(
            list(self.project.tracks), track_id)
        new_order = [t.id for t in new_tracks]
        new_groups = dict(old_groups)
        new_groups[track_id] = 0

        self.undo_stack.push(Command(
            lambda: apply(new_order, new_groups),
            lambda: apply(old_order, old_groups),
            "Remove from group"))

    # -- audio clips (undoable) -----------------------------------------------

    def add_file_to_track(self, path: str, track_id: int, frame: int) -> None:
        if track_id < 0:
            self.add_track()
            track_id = self.project.tracks[-1].id
        self._pending_loads += 1
        self.statusBar().showMessage(
            trf("Loading {name}...", name=os.path.basename(path)))
        QThreadPool.globalInstance().start(
            _FileLoadTask(self.bridge, path, track_id, frame,
                          self._loader_signals))

    def _on_file_loaded(self, path: str, track_id: int, frame: int,
                        file_id: int, frames: int) -> None:
        self._pending_loads = max(0, self._pending_loads - 1)
        if not self._pending_loads:
            self.statusBar().showMessage(tr("Ready"), 2000)
        if frames <= 0:
            QMessageBox.warning(self, tr("Import failed"),
                                trf("Empty audio file: {name}",
                                    name=os.path.basename(path)))
            return
        track = self.project.track_by_id(track_id)
        if track is None:
            return  # track vanished while loading
        if frame < 0:
            # Import: append after whatever is already on the track
            # (loads finish asynchronously, so the end is computed at
            # arrival time - clips never overlap regardless of order).
            frame = max([c.start_frame + c.length for c in track.clips],
                        default=0)

        clip = Clip(id=next_id(), file_path=path, file_id=file_id,
                    start_frame=frame, length=frames, warp=NEW_CLIP_WARP)
        # One-time warp fit on insertion (when snap is enabled): stretch
        # the clip so its length lands on the grid, exactly like a manual
        # Ctrl+edge-drag warp. Afterwards it only re-warps on BPM changes
        # (see _apply_bpm); the user can still warp it manually.
        if self.timeline.snap_enabled:
            self._warp_new_clip_to_grid(clip)

        def do() -> None:
            track = self.project.track_by_id(track_id)
            if track is None:
                return
            track.clips.append(clip)
            self.bridge.add_clip(track_id, clip)
            self._refresh_all()

        def undo() -> None:
            track = self.project.track_by_id(track_id)
            if track and clip in track.clips:
                track.clips.remove(clip)
            self.bridge.remove_clip(track_id, clip.id)
            self._refresh_all()

        self.undo_stack.push(Command(do, undo, "Add clip"))

    def _on_file_failed(self, path: str, error: str) -> None:
        self._pending_loads = max(0, self._pending_loads - 1)
        QMessageBox.warning(self, tr("Import failed"),
                            trf("Cannot load {name}:\n{error}",
                                name=os.path.basename(path), error=error))

    def _on_clip_moved(self, track_id: int, clip_id: int,
                       old_frame: int, new_frame: int) -> None:
        def apply(frame: int):
            def fn() -> None:
                track = self.project.track_by_id(track_id)
                clip = track.clip_by_id(clip_id) if track else None
                if clip:
                    clip.start_frame = frame
                    self.bridge.move_clip(track_id, clip_id, frame)
                    self.timeline.refresh_layout()
            return fn

        self.undo_stack.push(Command(apply(new_frame), apply(old_frame),
                                     "Move clip"))

    def _on_clip_track_changed(self, from_track: int, to_track: int,
                               clip_id: int, old_frame: int, new_frame: int,
                               is_midi: bool) -> None:
        def move(src_id: int, dst_id: int, frame: int) -> None:
            src = self.project.track_by_id(src_id)
            dst = self.project.track_by_id(dst_id)
            if not src or not dst:
                return
            if is_midi:
                clip = src.midi_clip_by_id(clip_id)
                if clip is None:
                    return
                src.midi_clips.remove(clip)
                dst.midi_clips.append(clip)
                clip.start_beat = self.project.frames_to_beats(frame)
            else:
                clip = src.clip_by_id(clip_id)
                if clip is None:
                    return
                src.clips.remove(clip)
                dst.clips.append(clip)
                clip.start_frame = frame
            self.bridge.move_clip_to_track(src_id, dst_id, clip_id, frame,
                                           is_midi)
            self._refresh_all()

        self.undo_stack.push(Command(
            lambda: move(from_track, to_track, new_frame),
            lambda: move(to_track, from_track, old_frame),
            "Move clip to track"))

    def _on_clip_trimmed(self, track_id: int, clip_id: int,
                         old_t, new_t) -> None:
        def apply(t):
            def fn() -> None:
                track = self.project.track_by_id(track_id)
                clip = track.clip_by_id(clip_id) if track else None
                if clip:
                    clip.start_frame, clip.source_offset, clip.length = t
                    self.bridge.trim_clip(track_id, clip)
                    self.timeline.refresh_layout()
            return fn

        self.undo_stack.push(Command(apply(new_t), apply(old_t), "Trim clip"))

    def _on_clip_fades(self, track_id: int, clip_id: int,
                       old_f, new_f) -> None:
        def apply(f):
            def fn() -> None:
                track = self.project.track_by_id(track_id)
                clip = track.clip_by_id(clip_id) if track else None
                if clip:
                    clip.fade_in, clip.fade_out = f
                    self.bridge.set_clip_fades(track_id, clip)
                    self.timeline.refresh_layout()
            return fn

        self.undo_stack.push(Command(apply(new_f), apply(old_f), "Clip fades"))

    # -- clip properties / rename / warp ------------------------------------

    def _sync_clip_warp(self, track_id: int, clip, *,
                        interactive: bool = True) -> bool:
        """Push clip geometry and realtime warp params to the engine."""
        try:
            if clip.file_id <= 0:
                clip.file_id, _frames = load_clip_audio(self.bridge, clip)
            self.bridge.trim_clip(track_id, clip)
            self.bridge.set_clip_fades(track_id, clip)
            self.bridge.set_clip_warp(track_id, clip)
            self.timeline.refresh_layout()
            return True
        except Exception as exc:  # noqa: BLE001
            if interactive:
                QMessageBox.warning(
                    self, tr("Warp failed"),
                    trf("Cannot stretch {name}:\n{err}",
                        name=clip.display_name, err=exc))
            else:
                self.statusBar().showMessage(
                    trf("Warp failed: {name}", name=clip.display_name), 5000)
            return False

    def _warp_new_clip_to_grid(self, clip: Clip) -> None:
        """Stretch a freshly imported clip so its length lands on the
        timeline grid (nearest whole number of snap steps, at least one).

        Same mechanics as a manual Ctrl+edge-drag warp: the stretch ratio
        is updated and pushed to the realtime engine warp processor."""
        fpb = self.project.frames_per_beat
        step = self.timeline.snap_beats or 1.0
        beats = clip.length / fpb
        target_beats = max(step, round(beats / step) * step)
        target_len = max(1, int(round(target_beats * fpb)))
        k = target_len / max(1, clip.length)
        new_ratio = clip.stretch_ratio * k
        if abs(k - 1.0) < 1e-6 \
                or not (MIN_RATIO <= new_ratio <= MAX_RATIO):
            return
        clip.stretch_ratio = new_ratio
        clip.length = target_len

    def _warp_existing_clip_to_grid(self, track_id: int, clip: Clip) -> bool:
        """One-time grid fit of a clip already on the timeline (used when
        Snap or the clip's auto-warp is re-enabled).

        Both edges land on the grid: the start (left edge) snaps to the
        nearest grid line — a plain move, no audio change — and the length
        is warped to the nearest whole number of snap steps, so the right
        edge follows onto the grid too. Handles a clip whose left edge was
        dragged with Snap off (left edge snaps back, right edge keeps its
        grid position) as well as a clip simply moved off-grid (both edges
        snap). Returns True if the clip changed at all."""
        fpb = self.project.frames_per_beat
        step = self.timeline.snap_beats or 1.0
        step_frames = max(1.0, step * fpb)

        # 1. Snap the start to the nearest grid line.
        new_start = max(0, int(round(
            round(clip.start_frame / step_frames) * step_frames)))
        moved = new_start != clip.start_frame
        if moved:
            clip.start_frame = new_start
            self.bridge.move_clip(track_id, clip.id, new_start)

        # 2. Warp the length to a whole number of snap steps.
        beats = clip.length / fpb
        target_beats = max(step, round(beats / step) * step)
        target_len = max(1, int(round(target_beats * fpb)))
        k = target_len / max(1, clip.length)
        new_ratio = clip.stretch_ratio * k
        if abs(k - 1.0) < 1e-6 \
                or not (MIN_RATIO <= new_ratio <= MAX_RATIO):
            if moved:
                self.timeline.refresh_layout()
            return moved
        saved = (clip.stretch_ratio, clip.source_offset,
                 clip.fade_in, clip.fade_out, clip.length)
        clip.stretch_ratio = new_ratio
        clip.source_offset = int(round(clip.source_offset * k))
        clip.fade_in = int(round(clip.fade_in * k))
        clip.fade_out = int(round(clip.fade_out * k))
        clip.length = target_len
        if not self._sync_clip_warp(track_id, clip, interactive=False):
            # Keep the clip consistent with the still-loaded audio.
            (clip.stretch_ratio, clip.source_offset,
             clip.fade_in, clip.fade_out, clip.length) = saved
            if moved:
                self.timeline.refresh_layout()
            return moved
        return True

    def _rewarp_audio_clips_for_bpm(self, old_bpm: float,
                                    new_bpm: float) -> None:
        """Tempo change: keep every audio clip at its musical position
        and musical length by re-warping it (like Ableton warp).

        Model geometry updates immediately; the C++ engine applies warp
        in realtime (no offline re-render)."""
        k = old_bpm / new_bpm  # frames-per-beat scale factor
        for track in self.project.tracks:
            for clip in track.clips:
                if not clip.auto_warp:
                    continue
                new_start = int(round(clip.start_frame * k))
                new_len = max(1, int(round(clip.length * k)))
                new_ratio = clip.stretch_ratio * (new_len
                                                  / max(1, clip.length))
                if new_len != clip.length \
                        and MIN_RATIO <= new_ratio <= MAX_RATIO:
                    clip.stretch_ratio = new_ratio
                    clip.source_offset = int(round(clip.source_offset * k))
                    clip.fade_in = int(round(clip.fade_in * k))
                    clip.fade_out = int(round(clip.fade_out * k))
                    clip.length = new_len
                    self._sync_clip_warp(track.id, clip, interactive=False)
                elif new_len == clip.length and new_start != clip.start_frame:
                    pass
                if new_start != clip.start_frame:
                    clip.start_frame = new_start
                    self.bridge.move_clip(track.id, clip.id, new_start)
        self.timeline.refresh_layout()

    def _on_clip_properties(self, track_id: int, clip_id: int,
                            is_midi: bool = False) -> None:
        from .clip_properties import ClipPropertiesDialog
        track = self.project.track_by_id(track_id)
        if track is None:
            return
        if is_midi:
            self._on_midi_clip_properties(track_id, clip_id)
            return
        clip = track.clip_by_id(clip_id)
        if clip is None:
            return
        dlg = ClipPropertiesDialog(clip.display_name, clip.warp,
                                   clip.stretch_ratio, clip.auto_warp,
                                   parent=self)
        if dlg.exec() != QDialog.Accepted:
            return
        name, warp, auto_warp = dlg.values()
        if name == clip.display_name:
            name = clip.name  # unchanged: keep the file-name fallback
        old = (clip.name, clip.warp, clip.auto_warp)
        if (name, warp, auto_warp) == old:
            return

        def apply(vals):
            n, w, aw = vals

            def fn() -> None:
                t = self.project.track_by_id(track_id)
                c = t.clip_by_id(clip_id) if t else None
                if c is None:
                    return
                c.name = n
                warp_changed = c.warp != w
                c.warp = w
                auto_warp_turned_on = aw and not c.auto_warp
                c.auto_warp = aw
                if warp_changed:
                    self._sync_clip_warp(track_id, c)
                else:
                    self.timeline.viewport().update()
                # Turning auto-warp back on while Snap is active: snap the
                # clip length to the grid once, like on insertion.
                if auto_warp_turned_on and self.timeline.snap_enabled:
                    self._warp_existing_clip_to_grid(track_id, c)
            return fn

        self.undo_stack.push(Command(apply((name, warp, auto_warp)),
                                     apply(old), "Clip properties"))

    def _on_midi_clip_properties(self, track_id: int, clip_id: int) -> None:
        """MIDI clip Properties: for now only the name is editable."""
        from .clip_properties import ClipPropertiesDialog
        track = self.project.track_by_id(track_id)
        clip = track.midi_clip_by_id(clip_id) if track else None
        if clip is None:
            return
        dlg = ClipPropertiesDialog(clip.name, midi=True, parent=self)
        if dlg.exec() != QDialog.Accepted:
            return
        name, _ = dlg.values()
        if not name or name == clip.name:
            return
        old_name = clip.name

        def apply(n: str):
            def fn() -> None:
                t = self.project.track_by_id(track_id)
                c = t.midi_clip_by_id(clip_id) if t else None
                if c is not None:
                    c.name = n
                    self.timeline.viewport().update()
            return fn

        self.undo_stack.push(Command(apply(name), apply(old_name),
                                     "Clip properties"))

    def _on_clip_rename(self, track_id: int, clip_id: int,
                        is_midi: bool) -> None:
        track = self.project.track_by_id(track_id)
        if track is None:
            return
        clip = (track.midi_clip_by_id(clip_id) if is_midi
                else track.clip_by_id(clip_id))
        if clip is None:
            return
        current = clip.name if is_midi else clip.display_name
        text, ok = QInputDialog.getText(self, tr("Rename clip"), tr("Name:"),
                                        text=current)
        if not ok:
            return
        new_name = text.strip()
        if not new_name or new_name == current:
            return
        old_name = clip.name

        def apply(n: str):
            def fn() -> None:
                t = self.project.track_by_id(track_id)
                c = ((t.midi_clip_by_id(clip_id) if is_midi
                      else t.clip_by_id(clip_id)) if t else None)
                if c is not None:
                    c.name = n
                    self.timeline.viewport().update()
            return fn

        self.undo_stack.push(Command(apply(new_name), apply(old_name),
                                     "Rename clip"))

    def _on_clip_stretched(self, track_id: int, clip_id: int,
                           old_t, new_t) -> None:
        """Ctrl+edge-drag: (start, source_offset, length, fade_in,
        fade_out, stretch_ratio) before/after."""
        def apply(t):
            def fn() -> None:
                track = self.project.track_by_id(track_id)
                clip = track.clip_by_id(clip_id) if track else None
                if clip is None:
                    return
                (clip.start_frame, clip.source_offset, clip.length,
                 clip.fade_in, clip.fade_out, clip.stretch_ratio) = t
                self.statusBar().showMessage(
                    trf("Warp ({method}): {ratio}x",
                        method=clip.warp,
                        ratio=f"{clip.stretch_ratio:.3f}"), 4000)
                self._sync_clip_warp(track_id, clip)
            return fn

        self.undo_stack.push(Command(apply(new_t), apply(old_t),
                                     "Stretch clip"))

    def _on_clip_split(self, track_id: int, clip_id: int, frame: int) -> None:
        cmd = self._make_audio_split_command(track_id, clip_id, frame)
        if cmd is not None:
            self.undo_stack.push(cmd)

    def _make_remove_clip_command(self, track_id: int, clip_id: int,
                                  is_midi: bool) -> Command | None:
        track = self.project.track_by_id(track_id)
        if track is None:
            return None
        if is_midi:
            clip = track.midi_clip_by_id(clip_id)
            if clip is None:
                return None

            def do() -> None:
                track.midi_clips.remove(clip)
                self.bridge.remove_midi_clip(track_id, clip_id)
                self._close_piano_roll(clip_id)
                self._refresh_all()

            def undo() -> None:
                track.midi_clips.append(clip)
                self.bridge.add_midi_clip(self.project, track_id, clip)
                self._refresh_all()
        else:
            clip = track.clip_by_id(clip_id)
            if clip is None:
                return None

            def do() -> None:
                track.clips.remove(clip)
                self.bridge.remove_clip(track_id, clip_id)
                self._refresh_all()

            def undo() -> None:
                track.clips.append(clip)
                self.bridge.add_clip(track_id, clip)
                self._refresh_all()

        return Command(do, undo, "Delete clip")

    def _on_clip_remove(self, track_id: int, clip_id: int,
                        is_midi: bool) -> None:
        cmd = self._make_remove_clip_command(track_id, clip_id, is_midi)
        if cmd is not None:
            self.undo_stack.push(cmd)

    def _on_clips_remove(self, refs) -> None:
        """Delete a whole selection (audio + MIDI mixed) as one undoable
        command: undo restores all clips together, redo removes them
        together."""
        commands = [cmd for track_id, clip_id, is_midi in refs
                    if (cmd := self._make_remove_clip_command(
                        track_id, clip_id, is_midi)) is not None]
        if not commands:
            return
        if len(commands) == 1:
            self.undo_stack.push(commands[0])
        else:
            self.undo_stack.push(
                CompositeCommand(commands,
                                 f"Delete {len(commands)} clips"))

    # -- duplicate / copy / paste ------------------------------------------

    def _resolve_clip_refs(self, refs) -> list[tuple[int, bool, object]]:
        """(track_id, clip_id, is_midi) refs -> (track_id, is_midi, clip)."""
        out = []
        for track_id, clip_id, is_midi in refs:
            track = self.project.track_by_id(track_id)
            if track is None:
                continue
            clip = (track.midi_clip_by_id(clip_id) if is_midi
                    else track.clip_by_id(clip_id))
            if clip is not None:
                out.append((track_id, is_midi, clip))
        return out

    def _clip_bounds_frames(self, entries) -> tuple[int, int]:
        """(min start, max end) in frames over (track_id, is_midi, clip)."""
        starts, ends = [], []
        for _tid, is_midi, clip in entries:
            if is_midi:
                starts.append(self.project.beats_to_frames(clip.start_beat))
                ends.append(self.project.beats_to_frames(clip.end_beat))
            else:
                starts.append(clip.start_frame)
                ends.append(clip.end_frame)
        return min(starts), max(ends)

    def _copy_of_clip(self, clip, is_midi: bool,
                      offset_frames: int, offset_beats: float):
        if is_midi:
            return MidiClip(id=next_id(), name=clip.name,
                            start_beat=clip.start_beat + offset_beats,
                            length_beats=clip.length_beats,
                            source_start_beat=clip.source_start_beat,
                            fade_in_beats=clip.fade_in_beats,
                            fade_out_beats=clip.fade_out_beats,
                            notes=[dataclasses.replace(n, id=next_id())
                                   for n in clip.notes])
        return dataclasses.replace(clip, id=next_id(),
                                   start_frame=clip.start_frame
                                   + offset_frames)

    def _push_add_clips_command(self, copies, text: str) -> None:
        """Undoable insertion of (track_id, is_midi, clip) copies."""
        if not copies:
            return

        def do() -> None:
            for track_id, is_midi, copy in copies:
                track = self.project.track_by_id(track_id)
                if track is None:
                    continue
                if is_midi:
                    track.midi_clips.append(copy)
                    self.bridge.add_midi_clip(self.project, track_id, copy)
                else:
                    track.clips.append(copy)
                    self.bridge.add_clip(track_id, copy)
                # Incremental item add: no full rebuild, so chained Ctrl+D
                # stays instant even on large projects.
                self.timeline.add_clip_item(track_id, copy, is_midi)
            self.timeline.select_clips([c.id for _t, _m, c in copies])

        def undo() -> None:
            for track_id, is_midi, copy in copies:
                track = self.project.track_by_id(track_id)
                if is_midi:
                    if track and copy in track.midi_clips:
                        track.midi_clips.remove(copy)
                    self.bridge.remove_midi_clip(track_id, copy.id)
                    self._close_piano_roll(copy.id)
                else:
                    if track and copy in track.clips:
                        track.clips.remove(copy)
                    self.bridge.remove_clip(track_id, copy.id)
                self.timeline.remove_clip_item(copy.id)

        self.undo_stack.push(Command(do, undo, text))

    def _on_clips_duplicate(self, refs) -> None:
        """Duplicate the selection as one block: every copy is shifted by
        the span of the whole group, so relative positions are kept."""
        entries = self._resolve_clip_refs(refs)
        if not entries:
            return
        start, end = self._clip_bounds_frames(entries)
        offset_frames = max(0, end - start)
        offset_beats = self.project.frames_to_beats(offset_frames)
        copies = [(tid, is_midi,
                   self._copy_of_clip(clip, is_midi,
                                      offset_frames, offset_beats))
                  for tid, is_midi, clip in entries]
        self._push_add_clips_command(copies, "Duplicate clips")

    def _on_clip_duplicate(self, track_id: int, clip_id: int,
                           is_midi: bool) -> None:
        """Single-clip duplicate (kept for context menus / tests)."""
        self._on_clips_duplicate([(track_id, clip_id, is_midi)])

    def _on_clips_copy(self, refs) -> None:
        """Ctrl+C: snapshot the selected clips into the app clipboard."""
        entries = self._resolve_clip_refs(refs)
        if not entries:
            return
        # Store deep copies so later edits/deletes don't affect the paste.
        self._clip_clipboard = [
            (tid, is_midi, self._copy_of_clip(clip, is_midi, 0, 0.0))
            for tid, is_midi, clip in entries]

    def _on_clips_paste(self) -> None:
        """Ctrl+V: paste the clipboard at the playhead, keeping the
        relative layout of the copied group."""
        snapshot = getattr(self, "_clip_clipboard", None)
        if not snapshot:
            return
        entries = [(tid, is_midi, clip)
                   for tid, is_midi, clip in snapshot
                   if self.project.track_by_id(tid) is not None]
        if not entries:
            return
        start, _end = self._clip_bounds_frames(entries)
        offset_frames = self.timeline.playhead_frame() - start
        offset_beats = self.project.frames_to_beats(offset_frames)
        copies = [(tid, is_midi,
                   self._copy_of_clip(clip, is_midi,
                                      offset_frames, offset_beats))
                  for tid, is_midi, clip in entries]
        self._push_add_clips_command(copies, "Paste clips")

    def import_audio_dialog(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, tr("Import audio"), default_projects_dir(), AUDIO_FILTER)
        if not paths:
            return
        # Imported audio always lands on a fresh audio track appended at
        # the end of the list (never mixed into an existing track).
        self.add_track(anchored=False)
        target = self.project.tracks[-1]
        for path in paths:
            # frame=-1: append after the clips already on the track, so
            # multiple imported files line up one after another instead
            # of piling up at frame 0.
            self.add_file_to_track(path, target.id, -1)

    # -- MIDI clips (undoable) ---------------------------------------------

    def add_midi_clip(self, track_id: int, start_beat: float) -> None:
        track = self.project.track_by_id(track_id)
        if track is None or not track.is_midi:
            return
        clip = MidiClip(id=next_id(),
                        name=f"MIDI {len(track.midi_clips) + 1}",
                        start_beat=start_beat, length_beats=4.0)

        def do() -> None:
            track.midi_clips.append(clip)
            self.bridge.add_midi_clip(self.project, track_id, clip)
            self._refresh_all()

        def undo() -> None:
            if clip in track.midi_clips:
                track.midi_clips.remove(clip)
            self.bridge.remove_midi_clip(track_id, clip.id)
            self._close_piano_roll(clip.id)
            self._refresh_all()

        self.undo_stack.push(Command(do, undo, "Add MIDI clip"))

    def _on_midi_clip_moved(self, track_id: int, clip_id: int,
                            old_beat: float, new_beat: float) -> None:
        def apply(beat: float):
            def fn() -> None:
                track = self.project.track_by_id(track_id)
                clip = track.midi_clip_by_id(clip_id) if track else None
                if clip:
                    clip.start_beat = beat
                    self.bridge.move_midi_clip(self.project, track_id, clip)
                    self.timeline.refresh_layout()
            return fn

        self.undo_stack.push(Command(apply(new_beat), apply(old_beat),
                                     "Move MIDI clip"))

    def _on_midi_clip_resized(self, track_id: int, clip_id: int,
                              old_len: float, new_len: float) -> None:
        def apply(length: float):
            def fn() -> None:
                track = self.project.track_by_id(track_id)
                clip = track.midi_clip_by_id(clip_id) if track else None
                if clip:
                    clip.length_beats = length
                    self.bridge.resize_midi_clip(self.project, track_id, clip)
                    self.timeline.refresh_layout()
                    self._refresh_piano_roll(clip_id)
            return fn

        self.undo_stack.push(Command(apply(new_len), apply(old_len),
                                     "Resize MIDI clip"))

    def _on_midi_clip_trimmed(self, track_id: int, clip_id: int,
                              old_t, new_t) -> None:
        """Left-edge resize: (start_beat, source_start_beat, length_beats)."""
        def apply(t):
            def fn() -> None:
                track = self.project.track_by_id(track_id)
                clip = track.midi_clip_by_id(clip_id) if track else None
                if clip:
                    (clip.start_beat, clip.source_start_beat,
                     clip.length_beats) = t
                    self.bridge.trim_midi_clip(self.project, track_id, clip)
                    self.timeline.refresh_layout()
                    self._refresh_piano_roll(clip_id)
            return fn

        self.undo_stack.push(Command(apply(new_t), apply(old_t),
                                     "Trim MIDI clip"))

    # -- piano roll -----------------------------------------------------------

    def open_piano_roll(self, track_id: int, clip_id: int) -> None:
        if clip_id in self._piano_rolls:
            self._piano_rolls[clip_id].raise_()
            self._piano_rolls[clip_id].activateWindow()
            return
        track = self.project.track_by_id(track_id)
        clip = track.midi_clip_by_id(clip_id) if track else None
        if not track or not clip:
            return
        dlg = PianoRollDialog(
            track, clip,
            self.timeline.snap_beats if self.timeline.snap_enabled else 0.0,
            self)
        dlg.note_add_requested.connect(self._on_note_add)
        dlg.notes_add_requested.connect(self._on_notes_add)
        dlg.notes_change_requested.connect(self._on_notes_change)
        dlg.notes_remove_requested.connect(self._on_notes_remove)
        dlg.note_slice_requested.connect(self._on_note_slice)
        dlg.velocities_change_requested.connect(self._on_velocities_change)
        dlg.preview_on.connect(self.bridge.preview_note_on)
        dlg.preview_off.connect(self.bridge.preview_note_off)
        dlg.undo_requested.connect(self.undo_stack.undo)
        dlg.redo_requested.connect(self.undo_stack.redo)
        dlg.finished.connect(lambda _: self._piano_rolls.pop(clip_id, None))
        self._piano_rolls[clip_id] = dlg
        dlg.show()

    def _close_piano_roll(self, clip_id: int) -> None:
        dlg = self._piano_rolls.pop(clip_id, None)
        if dlg:
            dlg.close()

    def _refresh_piano_roll(self, clip_id: int) -> None:
        dlg = self._piano_rolls.get(clip_id)
        if dlg:
            dlg.refresh()

    def _on_note_add(self, track_id: int, clip_id: int, pitch: int,
                     start_beat: float, length_beats: float) -> None:
        track = self.project.track_by_id(track_id)
        clip = track.midi_clip_by_id(clip_id) if track else None
        if clip is None:
            return
        note = MidiNote(id=next_id(), pitch=pitch, start_beat=start_beat,
                        length_beats=length_beats)

        def do() -> None:
            clip.notes.append(note)
            self.bridge.add_note(self.project, track_id, clip_id, note)
            self._refresh_piano_roll(clip_id)
            self.timeline.refresh_layout()

        def undo() -> None:
            if note in clip.notes:
                clip.notes.remove(note)
            self.bridge.remove_note(track_id, clip_id, note.id)
            self._refresh_piano_roll(clip_id)
            self.timeline.refresh_layout()

        self.undo_stack.push(Command(do, undo, "Add note"))

    def _on_notes_add(self, track_id: int, clip_id: int, specs) -> None:
        """Add several notes at once (paste / duplicate).

        `specs` is a list of (start_beat, pitch, length_beats, velocity).
        """
        track = self.project.track_by_id(track_id)
        clip = track.midi_clip_by_id(clip_id) if track else None
        if clip is None or not specs:
            return
        notes = [MidiNote(id=next_id(), pitch=pitch, start_beat=start,
                          length_beats=length, velocity=velocity)
                 for start, pitch, length, velocity in specs]

        def do() -> None:
            for note in notes:
                clip.notes.append(note)
                self.bridge.add_note(self.project, track_id, clip_id, note)
            # Select the new notes so Ctrl+D/Ctrl+V can be chained.
            dlg = self._piano_rolls.get(clip_id)
            if dlg is not None:
                dlg.view.selection = {n.id for n in notes}
            self._refresh_piano_roll(clip_id)
            self.timeline.refresh_layout()

        def undo() -> None:
            for note in notes:
                if note in clip.notes:
                    clip.notes.remove(note)
                self.bridge.remove_note(track_id, clip_id, note.id)
            self._refresh_piano_roll(clip_id)
            self.timeline.refresh_layout()

        self.undo_stack.push(Command(do, undo, "Paste notes"))

    def _on_notes_change(self, track_id: int, clip_id: int, changes) -> None:
        """`changes` is a list of (note_id, old_tuple, new_tuple)."""
        def apply(index: int):
            def fn() -> None:
                track = self.project.track_by_id(track_id)
                clip = track.midi_clip_by_id(clip_id) if track else None
                if clip is None:
                    return
                for note_id, *tuples in changes:
                    note = clip.note_by_id(note_id)
                    if note:
                        (note.pitch, note.start_beat,
                         note.length_beats) = tuples[index]
                        self.bridge.update_note(self.project, track_id,
                                                clip_id, note)
                self._refresh_piano_roll(clip_id)
                self.timeline.refresh_layout()
            return fn

        self.undo_stack.push(Command(apply(1), apply(0), "Edit notes"))

    def _on_notes_remove(self, track_id: int, clip_id: int, note_ids) -> None:
        track = self.project.track_by_id(track_id)
        clip = track.midi_clip_by_id(clip_id) if track else None
        if clip is None:
            return
        notes = [n for n in clip.notes if n.id in set(note_ids)]
        if not notes:
            return

        def do() -> None:
            for note in notes:
                if note in clip.notes:
                    clip.notes.remove(note)
                self.bridge.remove_note(track_id, clip_id, note.id)
            self._refresh_piano_roll(clip_id)
            self.timeline.refresh_layout()

        def undo() -> None:
            for note in notes:
                clip.notes.append(note)
                self.bridge.add_note(self.project, track_id, clip_id, note)
            self._refresh_piano_roll(clip_id)
            self.timeline.refresh_layout()

        self.undo_stack.push(Command(do, undo, "Delete notes"))

    def _on_note_slice(self, track_id: int, clip_id: int, note_id: int,
                       slice_beat: float) -> None:
        track = self.project.track_by_id(track_id)
        clip = track.midi_clip_by_id(clip_id) if track else None
        if clip is None:
            return
        note = clip.note_by_id(note_id)
        if note is None:
            return
        if not (note.start_beat < slice_beat < note.end_beat):
            return
        old_state = (note.pitch, note.start_beat, note.length_beats,
                     note.velocity)
        left_len = slice_beat - note.start_beat
        right_note = MidiNote(
            id=next_id(), pitch=note.pitch, start_beat=slice_beat,
            length_beats=note.end_beat - slice_beat, velocity=note.velocity)

        def do() -> None:
            note.length_beats = left_len
            clip.notes.append(right_note)
            self.bridge.update_note(self.project, track_id, clip_id, note)
            self.bridge.add_note(self.project, track_id, clip_id, right_note)
            self._refresh_piano_roll(clip_id)
            self.timeline.refresh_layout()

        def undo() -> None:
            if right_note in clip.notes:
                clip.notes.remove(right_note)
            self.bridge.remove_note(track_id, clip_id, right_note.id)
            (note.pitch, note.start_beat, note.length_beats,
             note.velocity) = old_state
            self.bridge.update_note(self.project, track_id, clip_id, note)
            self._refresh_piano_roll(clip_id)
            self.timeline.refresh_layout()

        self.undo_stack.push(Command(do, undo, "Slice note"))

    def _on_velocities_change(self, track_id: int, clip_id: int,
                              changes) -> None:
        """`changes` is a list of (note_id, old_velocity, new_velocity)."""
        def apply(index: int):
            def fn() -> None:
                track = self.project.track_by_id(track_id)
                clip = track.midi_clip_by_id(clip_id) if track else None
                if clip is None:
                    return
                for note_id, *velocities in changes:
                    note = clip.note_by_id(note_id)
                    if note:
                        note.velocity = velocities[index]
                        self.bridge.update_note(self.project, track_id,
                                                clip_id, note)
                self._refresh_piano_roll(clip_id)
            return fn

        self.undo_stack.push(Command(apply(1), apply(0), "Note velocity"))

    # -- instrument / effects -----------------------------------------------

    def _instrument_snapshot(self, track: Track) -> tuple:
        """(path, uid, name, state) of the current instrument; live VST
        state is pulled from the engine first so undo restores it."""
        if track.instrument.uses_vst:
            self.bridge.capture_vst_states(self.project)
        inst = track.instrument
        return (inst.vst_path, inst.vst_uid, inst.vst_name, inst.vst_state)

    def _remove_inst_lanes(self, track: Track) -> list:
        """Detach all instrument automation lanes (returned for undo)."""
        removed = []
        for lane in list(track.automation_lanes):
            if lane.target == "inst":
                track.automation_lanes.remove(lane)
                self.bridge.remove_automation_lane(track.id, lane.id)
                removed.append(lane)
        return removed

    def _restore_inst_lanes(self, track: Track, lanes: list) -> None:
        for lane in lanes:
            track.automation_lanes.append(lane)
            self.bridge.sync_automation_lane(self.project, track, lane)

    def _apply_instrument(self, track_id: int, snapshot: tuple) -> bool:
        """Load an instrument snapshot into model + engine (both undo
        directions). Empty path = built-in synth."""
        track = self.project.track_by_id(track_id)
        if track is None or not track.is_midi:
            return False
        editor = self._vst_editors.pop((track_id, -1), None)
        if editor:
            editor.close()
        path, uid, name, state = snapshot
        inst = track.instrument
        if path:
            if not self.bridge.set_vst_instrument(track_id, path, uid):
                return False
            inst.vst_path, inst.vst_uid = path, uid
            inst.vst_name, inst.vst_state = name, state
            if state:
                self.bridge.vst_set_state(track_id, -1, state)
        else:
            inst.vst_path = ""
            inst.vst_uid = ""
            inst.vst_name = ""
            inst.vst_state = b""
            self.bridge.clear_vst_instrument(track_id)
            self.bridge.set_instrument(track_id, inst)
        self._sync_track_instrument_ui(track_id)
        self._refresh_all()
        return True

    def _instrument_snapshot_now(self, track_id: int) -> tuple:
        track = self.project.track_by_id(track_id)
        if track is None or not track.is_midi or not track.instrument.uses_vst:
            return "", "", "", b""
        state = b""
        try:
            state = self.bridge.vst_get_state(track_id, -1)
        except Exception:
            state = track.instrument.vst_state
        return (track.instrument.vst_path, track.instrument.vst_uid,
                track.instrument.vst_name, state)

    def _push_instrument_command(self, track_id: int, new_snapshot: tuple,
                                 text: str) -> bool:
        """Undoable instrument change; the first do() is applied eagerly so
        a failed VST load never lands in the history."""
        track = self.project.track_by_id(track_id)
        if track is None or not track.is_midi:
            return False
        old_snapshot = self._instrument_snapshot(track)
        old_lanes = self._remove_inst_lanes(track)
        if not self._apply_instrument(track_id, new_snapshot):
            self._restore_inst_lanes(track, old_lanes)
            return False
        state = {"applied": True}

        def do() -> None:
            if state.pop("applied", None):
                return  # already applied eagerly, push() must not repeat it
            t = self.project.track_by_id(track_id)
            if t is not None:
                self._remove_inst_lanes(t)
            self._apply_instrument(track_id, new_snapshot)

        def undo() -> None:
            self._apply_instrument(track_id, old_snapshot)
            t = self.project.track_by_id(track_id)
            if t is not None:
                self._restore_inst_lanes(t, old_lanes)
                self._refresh_all()

        self.undo_stack.push(Command(do, undo, text))
        return True

    def _on_vst_plugin_pick(self, track_id: int) -> None:
        track = self.project.track_by_id(track_id)
        if track is None or not track.is_midi:
            return
        dlg = PluginBrowserDialog(self.bridge, self, instruments_only=True)
        if dlg.exec() != dlg.DialogCode.Accepted:
            self._sync_track_instrument_ui(track_id)
            return
        plugin = dlg.selected_plugin()
        if plugin is None:
            self._sync_track_instrument_ui(track_id)
            return
        snapshot = (plugin.path, plugin.uid, plugin.name, b"")
        ok = self._with_vst_load_progress(
            track_id, -1, plugin.name,
            lambda: self._push_instrument_command(
                track_id, snapshot, "Set instrument"))
        if not ok:
            self._sync_track_instrument_ui(track_id)
            QMessageBox.warning(
                self, tr("VST instrument"),
                trf("Could not load plugin:\n{name}", name=plugin.name))

    def _on_instrument_remove(self, track_id: int) -> None:
        """Clear the VST instrument of a MIDI track (bottom panel button)."""
        track = self.project.track_by_id(track_id)
        if track is None or not track.is_midi \
                or not track.instrument.uses_vst:
            return
        old_snapshot = self._instrument_snapshot_now(track_id)
        old_lanes = self._remove_inst_lanes(track)
        if not self._apply_instrument(track_id, ("", "", "", b"")):
            self._restore_inst_lanes(track, old_lanes)
            return
        state = {"applied": True}

        def do() -> None:
            if state.pop("applied", None):
                return
            self._remove_inst_lanes(track)
            self._apply_instrument(track_id, ("", "", "", b""))

        def undo() -> None:
            self._apply_instrument(track_id, old_snapshot)
            t = self.project.track_by_id(track_id)
            if t is not None:
                self._restore_inst_lanes(t, old_lanes)
                self._refresh_all()

        self.undo_stack.push(Command(do, undo, "Remove instrument"))

    def _sync_track_instrument_ui(self, track_id: int) -> None:
        header = self.track_panel.headers.get(track_id)
        if header:
            header._sync_instrument_ui()
        if self._selected_track_id == track_id:
            track = self.project.track_by_id(track_id)
            if track:
                self.fx_panel.set_track(track)
                self.fx_panel.rebuild()

    def _sync_master_proxy(self) -> None:
        """Point the master proxy track at the current project's lists."""
        self._master_fx_track.effects = self.project.master_effects
        self._master_fx_track.automation_lanes = \
            self.project.master_automation_lanes

    def _fx_track(self, track_id: int) -> Track | None:
        """Track for FX handlers; MASTER_FX_ID maps to the master proxy."""
        if track_id == MASTER_FX_ID:
            self._sync_master_proxy()
            return self._master_fx_track
        return self.project.track_by_id(track_id)

    def _select_track_for_fx(self, track_id: int) -> None:
        track = self.project.track_by_id(track_id)
        if track is None:
            return
        self._selected_track_id = track_id
        self.master_header.set_selected(False)
        self._set_selected_lane(None)
        if track_id not in self.track_panel.selected_track_ids():
            self.track_panel.set_selected_track(track_id)
        self.fx_panel.set_track(track)
        self.fx_panel.rebuild()
        self._show_fx_panel()

    def _on_mixer_fx_requested(self, track_id: int) -> None:
        """FX button in the mixer: track strip or the master section."""
        if track_id == MASTER_FX_ID:
            self._select_master_for_fx()
        else:
            self._select_track_for_fx(track_id)

    def _select_master_for_fx(self) -> None:
        self._selected_track_id = MASTER_FX_ID
        self._set_selected_lane(None)
        self.track_panel.set_selected_track(None)
        self.master_header.set_selected(True)
        self.fx_panel.set_track(self._fx_track(MASTER_FX_ID))
        self.fx_panel.rebuild()
        self._show_fx_panel()

    def _set_selected_lane(self, key: tuple[int, int] | None) -> None:
        """Select/deselect an automation lane (selected lane is taller)."""
        self.track_panel.set_selected_lane(key)
        self.timeline.set_selected_lane(key)

    def _on_automation_lane_clicked(self, track_id: int, lane_id: int) -> None:
        key = (track_id, lane_id)
        if self.timeline.selected_lane() == key:
            key = None  # click again to collapse
        self._set_selected_lane(key)

    def _timeline_row_scroll_offset(self) -> int:
        """Difference between timeline scrollbar space and panel content space."""
        return int(round(-self.timeline.sceneRect().top()))

    def _sync_track_panel_scroll_from_timeline(self, value: int) -> None:
        self.track_panel.sync_scroll(value + self._timeline_row_scroll_offset())

    def _sync_timeline_scroll_from_track_panel(self, value: int) -> None:
        self.timeline.verticalScrollBar().setValue(
            value - self._timeline_row_scroll_offset())

    def _sync_scroll_ranges(self) -> None:
        """Grow the panel's bottom pad until its scroll range matches the
        timeline's, so scroll sync never clamps (converges via the
        rangeChanged signal this handler is connected to)."""
        pbar = self.track_panel.verticalScrollBar()
        tbar = self.timeline.verticalScrollBar()
        target = tbar.maximum() + self._timeline_row_scroll_offset()
        extra = self._panel_extra_pad + target - pbar.maximum()
        extra = max(0, min(extra, 200))
        if extra != self._panel_extra_pad:
            self._panel_extra_pad = extra
            self.track_panel.set_extra_bottom_pad(extra)

    def _show_fx_panel(self) -> None:
        if not self.fx_panel.isVisible():
            self._set_fx_panel_visible(True)

    def _toggle_fx_panel(self) -> None:
        self._set_fx_panel_visible(not self.fx_panel.isVisible())

    def _set_fx_panel_visible(self, visible: bool) -> None:
        sizes = self._body_splitter.sizes()
        if visible:
            if not self.fx_panel.isVisible():
                self.fx_panel.show()
            total = sum(sizes) if sum(sizes) > 0 else 720
            body_h = max(200, total - self._fx_panel_last_size)
            self._body_splitter.setSizes([body_h, self._fx_panel_last_size])
        else:
            if len(sizes) >= 2 and sizes[1] > 0:
                self._fx_panel_last_size = sizes[1]
            self.fx_panel.hide()
            total = sum(sizes) if sum(sizes) > 0 else 720
            self._body_splitter.setSizes([total, 0])
        if hasattr(self, "fx_panel_action"):
            self.fx_panel_action.blockSignals(True)
            self.fx_panel_action.setChecked(visible)
            self.fx_panel_action.blockSignals(False)

    def open_vst_editor(self, track_id: int, slot: int) -> None:
        key = (track_id, slot)
        existing = self._vst_editors.get(key)
        if existing is not None:
            existing.focus_editor()
            return
        track = self._fx_track(track_id)
        if track is None:
            return
        if slot < 0:
            if not track.instrument.uses_vst:
                return
            title = track.instrument.vst_name or "VST Instrument"
            plugin_uid = track.instrument.vst_uid
        else:
            if not (0 <= slot < len(track.effects)):
                return
            fx = track.effects[slot]
            if fx.type != "vst":
                return
            title = fx.display_name
            plugin_uid = fx.uid
        win = VstEditorWindow(self.bridge, track_id, slot, title,
                              plugin_uid, self)
        win.route_params_externally = True
        win.param_edited.connect(self._on_vst_effect_param)
        win.undo_requested.connect(self.undo_stack.undo)
        win.redo_requested.connect(self.undo_stack.redo)
        win.finished.connect(
            lambda _: self._vst_editors.pop(key, None))
        self._vst_editors[key] = win
        win.show()

    def _defer_vst_editor_open(self, track_id: int, slot: int) -> None:
        """Open the editor window once the subprocess load finishes."""
        key = (track_id, slot)
        if key in self._vst_editors:
            return

        timer = QTimer(self)
        timer.setInterval(200)
        attempts = [0]

        def tick() -> None:
            attempts[0] += 1
            if self.bridge.vst_is_loading(track_id, slot):
                if attempts[0] >= 300:
                    timer.stop()
                return
            timer.stop()
            # Always open the window: it retries attach internally and
            # falls back to generic sliders when the plugin has no editor.
            self.open_vst_editor(track_id, slot)

        timer.timeout.connect(tick)
        timer.start()
        tick()

    def _set_vst_load_progress_active(self, active: bool) -> None:
        if active:
            self._vst_load_progress_depth += 1
        else:
            self._vst_load_progress_depth = max(
                0, self._vst_load_progress_depth - 1)

    def _make_vst_load_progress_dialog(self, label: str):
        from PySide6.QtWidgets import QProgressDialog

        progress = QProgressDialog(label, "", 0, 0, self)
        progress.setCancelButton(None)
        progress.setWindowTitle(tr("Loading VST"))
        progress.setWindowModality(Qt.ApplicationModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setMinimumWidth(360)
        return progress

    @staticmethod
    def _show_vst_load_progress_dialog(progress) -> None:
        progress.setValue(0)
        progress.show()
        progress.raise_()
        progress.activateWindow()
        QApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)
        progress.repaint()
        QApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents)

    @staticmethod
    def _vst_load_failed(diagnostic: str) -> bool:
        fail_markers = ("load failed", "not running", "timed out",
                        "no response", "not found", "plugin is offline")
        return any(m in diagnostic.lower() for m in fail_markers)

    def _vst_target_is_loading(self, track_id: int, slot: int) -> bool:
        if self.bridge.vst_is_loading(track_id, slot):
            return True
        # Diagnostic is a second source of truth for older/transitioning
        # native builds and avoids closing the progress window during the
        # short registry/commit window while a heavy plugin is still loading.
        return "loading" in self.bridge.vst_diagnostic(track_id, slot).lower()

    def _wait_vst_load_progress(self, label: str, apply_fn,
                                target_fn) -> bool:
        """Show modal progress, run `apply_fn`, wait for VST load targets."""
        progress = self._make_vst_load_progress_dialog(label)
        self._show_vst_load_progress_dialog(progress)

        timer_was_active = self._timer.isActive()
        self._set_vst_load_progress_active(True)
        try:
            if not apply_fn():
                return False

            QApplication.processEvents(
                QEventLoop.ProcessEventsFlag.AllEvents)

            targets = list(target_fn())
            timer = QTimer(self)
            timer.setInterval(50)
            state = {"saw_loading": False, "ok": True, "done": False}
            attempts = [0]

            def tick() -> None:
                attempts[0] += 1
                QApplication.processEvents(
                    QEventLoop.ProcessEventsFlag.AllEvents)
                progress.raise_()
                loading = [target for target in targets
                           if self._vst_target_is_loading(target[0], target[1])]
                if loading:
                    state["saw_loading"] = True
                    if attempts[0] >= 1200:
                        state["ok"] = False
                        state["done"] = True
                        timer.stop()
                    return
                timer.stop()
                for track_id, slot, _name in targets:
                    diag = self.bridge.vst_diagnostic(track_id, slot)
                    if self._vst_load_failed(diag):
                        state["ok"] = False
                        break
                if attempts[0] >= 1200:
                    state["ok"] = False
                state["done"] = True

            timer.timeout.connect(tick)
            timer.start()
            tick()
            while not state["done"]:
                QApplication.processEvents(
                    QEventLoop.ProcessEventsFlag.AllEvents, 50)
            return state["ok"]
        finally:
            progress.close()
            self._set_vst_load_progress_active(False)
            if timer_was_active and not self._timer.isActive():
                self._timer.start()

    def _with_vst_load_progress(self, track_id: int, slot: int,
                                plugin_name: str, apply_fn,
                                *, open_editor: bool = True) -> bool:
        """Show modal progress, run `apply_fn`, wait for subprocess load."""
        label = trf("Loading VST plugin:\n{name}", name=plugin_name)

        def target_fn():
            return [(track_id, slot, plugin_name)]

        ok = self._wait_vst_load_progress(label, apply_fn, target_fn)
        if ok and open_editor:
            self._defer_vst_editor_open(track_id, slot)
        return ok

    def _project_vst_load_targets(self, project: Project
                                  ) -> list[tuple[int, int, str]]:
        targets: list[tuple[int, int, str]] = []
        for track in project.tracks:
            if track.is_midi and track.instrument.uses_vst:
                diag = self.bridge.vst_diagnostic(track.id, -1)
                if not self._vst_load_failed(diag):
                    targets.append((track.id, -1,
                                    track.instrument.vst_name or
                                    track.instrument.vst_path))
            for slot, fx in enumerate(track.effects):
                if fx.type != "vst":
                    continue
                if self.bridge.resolve_fx_slot(track.id, slot) is not None:
                    targets.append((track.id, slot, fx.name or fx.path))
        for slot, fx in enumerate(project.master_effects):
            if fx.type != "vst":
                continue
            if self.bridge.resolve_fx_slot(MASTER_FX_ID, slot) is not None:
                targets.append((MASTER_FX_ID, slot, fx.name or fx.path))
        return targets

    @staticmethod
    def _project_has_vst(project: Project) -> bool:
        for track in project.tracks:
            if track.is_midi and track.instrument.uses_vst:
                return True
            if any(fx.type == "vst" for fx in track.effects):
                return True
        return any(fx.type == "vst" for fx in project.master_effects)

    def _sync_project_with_vst_progress(self, project: Project) -> bool:
        if not self._project_has_vst(project):
            self.bridge.sync_project(project)
            return True
        label = tr("Loading VST plugins...")
        return self._wait_vst_load_progress(
            label,
            lambda: (self.bridge.sync_project(project) or True),
            lambda: self._project_vst_load_targets(project))

    def _close_vst_editors_for_track(self, track_id: int) -> None:
        for key in list(self._vst_editors):
            if key[0] == track_id:
                self._vst_editors.pop(key).close()

    def _flush_qt_deletes(self) -> None:
        for _ in range(10):
            QApplication.processEvents(
                QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)

    def _close_all_vst_editors(self) -> None:
        for win in list(self._vst_editors.values()):
            win.detach_editor()
            win.close()
        self._vst_editors.clear()
        self._flush_qt_deletes()

    def _close_chain_editors(self, track_id: int) -> None:
        """Close effect editors of one chain: the engine reloads plugin
        instances on resync, so open editor windows would go stale."""
        for key in list(self._vst_editors):
            if key[0] == track_id and key[1] >= 0:
                self._vst_editors.pop(key).close()

    def _fx_chain_insert(self, track_id: int, slot: int, fx: Effect,
                         lanes: list) -> None:
        """Insert an effect and its automation lanes, keeping engine churn low."""
        track = self._fx_track(track_id)
        if track is None:
            return
        slot = max(0, min(slot, len(track.effects)))
        if slot == len(track.effects) and not lanes:
            if not self.bridge.append_effect(track_id, slot, fx):
                return
            track.effects.append(fx)
            if self._selected_track_id == track_id:
                self.fx_panel.rebuild()
            self._refresh_all()
            return

        self._close_chain_editors(track_id)
        if any(existing.type == "vst" for existing in track.effects):
            self.bridge.capture_vst_states(self.project)
        track.effects.insert(slot, fx)
        for lane in track.automation_lanes:
            if lane.target == "fx" and lane.fx_slot >= slot:
                lane.fx_slot += 1
        for lane in lanes:  # lanes carry their original fx_slot (= slot)
            track.automation_lanes.append(lane)
        self.bridge.resync_track_fx(track)
        for lane in track.automation_lanes:
            if lane.target == "fx":
                self.bridge.sync_automation_lane(self.project, track, lane)
        if self._selected_track_id == track_id:
            self.fx_panel.rebuild()
        self._refresh_all()

    def _fx_chain_remove(self, track_id: int,
                         slot: int) -> tuple[Effect, list] | None:
        """Remove the effect at `slot`; returns (effect, its lanes) so undo
        can restore them. Shared by remove-do and add-undo."""
        track = self._fx_track(track_id)
        if track is None or not (0 <= slot < len(track.effects)):
            return None
        self._close_chain_editors(track_id)
        fx = track.effects[slot]
        if (fx.type == "vst" and
                self.bridge.resolve_fx_slot(track_id, slot) is not None):
            state = self.bridge.vst_get_state(track_id, slot)
            if state:
                fx.state = state
        removed_lanes = []
        for lane in list(track.automation_lanes):
            if lane.target == "fx" and lane.fx_slot == slot:
                track.automation_lanes.remove(lane)
                self.bridge.remove_automation_lane(track_id, lane.id)
                removed_lanes.append(lane)
            elif lane.target == "fx" and lane.fx_slot > slot:
                lane.fx_slot -= 1
        fx = track.effects.pop(slot)
        self.bridge.remove_effect(track_id, slot)
        for lane in track.automation_lanes:
            if lane.target == "fx":
                self.bridge.sync_automation_lane(self.project, track, lane)
        if self._selected_track_id == track_id:
            self.fx_panel.rebuild()
        self._refresh_all()
        return fx, removed_lanes

    def _on_vst_effect_pick(self, track_id: int) -> None:
        dlg = PluginBrowserDialog(self.bridge, self, instruments_only=False)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        plugin = dlg.selected_plugin()
        if plugin is None or plugin.is_instrument:
            return
        track = self._fx_track(track_id)
        if track is None:
            return
        fx = Effect(type="vst", path=plugin.path, uid=plugin.uid,
                    name=plugin.name)
        slot = len(track.effects)
        state = {"applied": True}

        def apply_effect() -> bool:
            self._fx_chain_insert(track_id, slot, fx, [])
            if self.bridge.resolve_fx_slot(track_id, slot) is None:
                self._fx_chain_remove(track_id, slot)
                return False

            def do() -> None:
                if state.pop("applied", None):
                    return
                self._fx_chain_insert(track_id, slot, fx, [])

            self.undo_stack.push(Command(
                do, lambda: self._fx_chain_remove(track_id, slot),
                "Add VST effect"))
            return True

        if not self._with_vst_load_progress(
                track_id, slot, plugin.name, apply_effect):
            QMessageBox.warning(
                self, tr("VST effect"),
                trf("Could not load plugin:\n{name}", name=plugin.name))

    def _on_effect_added(self, track_id: int, fx_type: str) -> None:
        track = self._fx_track(track_id)
        if track is None:
            return
        fx = Effect(fx_type)
        slot = len(track.effects)
        self.undo_stack.push(Command(
            lambda: self._fx_chain_insert(track_id, slot, fx, []),
            lambda: self._fx_chain_remove(track_id, slot),
            "Add effect"))

    def _on_effect_removed(self, track_id: int, slot: int) -> None:
        track = self._fx_track(track_id)
        if track is None or not (0 <= slot < len(track.effects)):
            return
        removed: dict = {}

        def do() -> None:
            result = self._fx_chain_remove(track_id, slot)
            if result is not None:
                removed["fx"], removed["lanes"] = result

        def undo() -> None:
            if "fx" in removed:
                self._fx_chain_insert(track_id, slot, removed["fx"],
                                      removed["lanes"])

        self.undo_stack.push(Command(do, undo, "Remove effect"))

    def _on_effect_moved(self, track_id: int, from_slot: int,
                         to_slot: int) -> None:
        """Drag-and-drop reorder of one effect inside a chain (undoable)."""
        track = self._fx_track(track_id)
        if track is None or from_slot == to_slot:
            return
        n = len(track.effects)
        if not (0 <= from_slot < n and 0 <= to_slot < n):
            return

        def apply(src: int, dst: int):
            def fn() -> None:
                # Engine reloads plugin instances on resync: open editor
                # windows for this chain would point at dead views.
                for key in list(self._vst_editors):
                    if key[0] == track_id and key[1] >= 0:
                        self._vst_editors.pop(key).close()
                # Preserve live VST settings across the plugin reload.
                self.bridge.capture_vst_states(self.project)
                fx = track.effects.pop(src)
                track.effects.insert(dst, fx)
                for lane in track.automation_lanes:
                    if lane.target != "fx":
                        continue
                    if lane.fx_slot == src:
                        lane.fx_slot = dst
                    elif src < dst and src < lane.fx_slot <= dst:
                        lane.fx_slot -= 1
                    elif dst < src and dst <= lane.fx_slot < src:
                        lane.fx_slot += 1
                self.bridge.resync_track_fx(track)
                for lane in track.automation_lanes:
                    if lane.target == "fx":
                        self.bridge.sync_automation_lane(
                            self.project, track, lane)
                self._refresh_all()
            return fn

        self.undo_stack.push(Command(apply(from_slot, to_slot),
                                     apply(to_slot, from_slot),
                                     "Reorder effects"))

    def _apply_vst_param(self, track_id: int, slot: int, param: int,
                         normalized: float) -> None:
        # mark_user_edit=False: undo/redo and other programmatic writes
        # must not register as TWEAK-mode touches (otherwise Ctrl+Z with
        # TWEAK armed would pop the "Add automate lane?" dialog).
        self.bridge.vst_set_param(track_id, slot, param, normalized,
                                  mark_user_edit=False)
        self.bridge.vst_sync_controllers()
        # Mirror into visible generic panels (undo/redo of a slider that
        # the user is not currently dragging).
        if self._selected_track_id == track_id:
            self.fx_panel.refresh_vst_panels()
        editor = self._vst_editors.get((track_id, slot))
        if editor is not None:
            editor.refresh_params()

    def _on_vst_effect_param(self, track_id: int, slot: int, param: int,
                             normalized: float) -> None:
        old = self.bridge.vst_get_param(track_id, slot, param)
        self._record_param_gesture(
            ("vst", track_id, slot, param), old, normalized,
            lambda v: self._apply_vst_param(track_id, slot, param, v),
            "VST parameter")
        self._apply_vst_param(track_id, slot, param, normalized)
        # Generic host sliders count as TWEAK touches (the engine flag is
        # no longer set by programmatic writes, so register it here).
        self._note_vst_tweak(track_id, slot, param)

    def _apply_effect_param(self, track_id: int, slot: int, param: int,
                            value: float) -> None:
        track = self._fx_track(track_id)
        if track and 0 <= slot < len(track.effects):
            fx = track.effects[slot]
            while len(fx.params) <= param:
                fx.params.append(0.0)
            fx.params[param] = value
            self.bridge.set_effect_param(track_id, slot, param, value)
            if self._selected_track_id == track_id:
                self.fx_panel.show_builtin_param(slot, param, value)

    def _on_effect_param(self, track_id: int, slot: int, param: int,
                         value: float) -> None:
        track = self._fx_track(track_id)
        if track and 0 <= slot < len(track.effects):
            fx = track.effects[slot]
            if fx.type in EFFECT_DEFS and param < len(EFFECT_DEFS[fx.type][1]):
                fx_name, defs = EFFECT_DEFS[fx.type]
                label = f"{fx_name}: {defs[param][0]}"
                if track_id == MASTER_FX_ID:
                    label = f"Master · {label}"
                old = (fx.params[param] if param < len(fx.params)
                       else defs[param][3])
                self._record_param_gesture(
                    ("fx", track_id, slot, param), old, value,
                    lambda v: self._apply_effect_param(
                        track_id, slot, param, v), label)
                self._apply_effect_param(track_id, slot, param, value)
                self._note_tweak(track_id, "fx", slot, param, label)
            else:
                self._apply_effect_param(track_id, slot, param, value)

    # -- continuous-control undo (knobs & sliders, builtin + VST) -----------

    def _record_param_gesture(self, key: tuple, old_value: float,
                              new_value: float, apply_fn, text: str) -> None:
        """Coalesce a stream of changes to one parameter into one undo step.

        Call BEFORE applying the change: `old_value` is captured only when
        a new gesture starts. The gesture is finalized (pushed onto the
        undo stack) after 500 ms without changes or when another parameter
        is touched.
        """
        gesture = self._param_gesture
        if gesture is not None and gesture["key"] != key:
            self._finish_param_gesture()
            gesture = None
        if gesture is None:
            self._param_gesture = gesture = {
                "key": key, "old": old_value, "text": text}
        gesture["new"] = new_value
        gesture["apply"] = apply_fn
        self._param_gesture_timer.start()
        self._update_undo_actions()

    def _discard_param_gesture(self) -> None:
        """Drop a pending gesture without pushing it (project switch)."""
        self._param_gesture = None
        self._param_gesture_timer.stop()
        self._update_undo_actions()

    def _finish_param_gesture(self) -> None:
        gesture, self._param_gesture = self._param_gesture, None
        self._param_gesture_timer.stop()
        if gesture is None or gesture["old"] == gesture["new"]:
            if gesture is not None:
                self._update_undo_actions()  # no-op gesture: re-disable
            return
        apply_fn = gesture["apply"]
        old, new = gesture["old"], gesture["new"]
        # The final value is already applied live; push() re-applies it,
        # which is an idempotent no-op.
        self.undo_stack.push(Command(lambda: apply_fn(new),
                                     lambda: apply_fn(old),
                                     gesture["text"]))

    # -- TWEAK mode (touch a control -> offer an automation lane) -----------

    def _on_tweak_toggled(self, on: bool) -> None:
        self._tweak_mode = on
        self._tweak_candidate = None
        self._tweak_timer.stop()
        if on:
            # Drop tweaks recorded before the mode was armed.
            self.bridge.vst_consume_last_tweak()

    def _set_tweak_mode(self, on: bool) -> None:
        self._tweak_mode = on
        self.transport.set_tweak_mode(on)
        if not on:
            self._tweak_candidate = None
            self._tweak_timer.stop()

    def _note_tweak(self, track_id: int, target: str, fx_slot: int,
                    param: int, name: str) -> None:
        """A control was touched: remember it and (re)start the 1 s timer."""
        if not self._tweak_mode or self._tweak_dialog_open:
            return
        self._tweak_candidate = (track_id, target, fx_slot, param, name)
        self._tweak_timer.start()

    def _note_vst_tweak(self, track_id: int, slot: int, param: int) -> None:
        """Engine reported a user tweak inside a VST (editor or sliders)."""
        if not self._tweak_mode:
            return
        track = self._fx_track(track_id)
        if track is None:
            return
        # Name the lane after the *current* parameter list (preset
        # switches can rename/regroup params).
        if self.bridge.vst_refresh_changed_params():
            self._on_vst_params_changed()
        if slot < 0:
            prefix = track.instrument.vst_name or "Instrument"
            target, fx_slot = "inst", -1
        else:
            if not 0 <= slot < len(track.effects):
                return
            prefix = track.effects[slot].display_name or "VST"
            target, fx_slot = "fx", slot
        params = self.bridge.vst_params(track_id, slot)
        pname = ""
        if 0 <= param < len(params):
            pname = str(params[param][1])
        name = f"{prefix} · {pname or f'Param {param}'}"
        if track_id == MASTER_FX_ID:
            name = f"Master · {name}"
        self._note_tweak(track_id, target, fx_slot, param, name)

    def _show_tweak_dialog(self) -> None:
        if (not self._tweak_mode or self._tweak_dialog_open
                or self._tweak_candidate is None):
            return
        track_id, target, fx_slot, param, name = self._tweak_candidate
        self._tweak_dialog_open = True
        from .tweak_dialog import TweakDialog
        dlg = TweakDialog(name, self)
        result = dlg.exec()
        self._tweak_dialog_open = False
        # Either way Tweak disarms; accepting additionally creates the lane.
        self._set_tweak_mode(False)
        track = self._fx_track(track_id)
        if result == QDialog.Accepted and track is not None:
            self._on_add_automation_lane(track_id, target, fx_slot, param,
                                         name)

    def _lane_key(self, lane: AutomationLane) -> tuple[str, int, int]:
        return (lane.target, lane.fx_slot, lane.param)

    def _show_master_context_menu(self, global_pos) -> None:
        """Right-click on the master strip: add master automation lanes."""
        master = self._fx_track(MASTER_FX_ID)
        menu = QMenu(self)
        auto_menu = menu.addMenu("Add automation \u25b8")
        auto_menu.addAction(
            "Volume", lambda: self._on_add_automation_lane(
                MASTER_FX_ID, "volume", -1, 0, "Master · Volume"))
        for slot, fx in enumerate(master.effects):
            if fx.type == "vst":
                label = fx.display_name or "VST"
                auto_menu.addAction(
                    f"{label}: параметры\u2026",
                    lambda s=slot: self._on_add_automation_vst_pick(
                        MASTER_FX_ID, s))
            elif fx.type in EFFECT_DEFS:
                fx_name, params = EFFECT_DEFS[fx.type]
                for pi, (plabel, *_rest) in enumerate(params):
                    auto_menu.addAction(
                        f"{fx_name}: {plabel}",
                        lambda s=slot, p=pi,
                        n=f"Master · {fx_name}: {plabel}":
                        self._on_add_automation_lane(
                            MASTER_FX_ID, "fx", s, p, n))
        menu.exec(global_pos)

    def _initial_lane_value(self, track_id: int, track, target: str,
                            fx_slot: int, param: int) -> float:
        """Current parameter value, normalized 0..1, for a new lane's
        starting point (mirrors _native_automation_value in reverse)."""
        if target == "volume":
            gain = (self.project.master_gain if track_id == MASTER_FX_ID
                    else track.gain)
            return gain / 1.5
        if target == "pan":
            return (track.pan + 1.0) / 2.0
        if target == "width":
            return track.stereo_width / 2.0
        if target == "fx" and 0 <= fx_slot < len(track.effects):
            fx = track.effects[fx_slot]
            if fx.type == "vst":
                return self.bridge.vst_get_param(track_id, fx_slot, param)
            if fx.type in EFFECT_DEFS and param < len(EFFECT_DEFS[fx.type][1]):
                lo, hi = EFFECT_DEFS[fx.type][1][param][1:3]
                value = (fx.params[param] if param < len(fx.params)
                         else EFFECT_DEFS[fx.type][1][param][3])
                if hi > lo:
                    return (value - lo) / (hi - lo)
        if target == "inst":
            return self.bridge.vst_get_param(track_id, -1, param)
        return 0.5

    def _on_add_automation_lane(self, track_id: int, target: str,
                                fx_slot: int, param: int,
                                name: str,
                                initial: float | None = None) -> None:
        track = self._fx_track(track_id)
        if track is None:
            return
        for existing in track.automation_lanes:
            if self._lane_key(existing) == (target, fx_slot, param):
                return

        lane_id = next_id()
        if initial is None:
            initial = self._initial_lane_value(
                track_id, track, target, fx_slot, param)
        initial = max(0.0, min(1.0, initial))

        def apply_add() -> None:
            lane = AutomationLane(id=lane_id, target=target, fx_slot=fx_slot,
                                  param=param, name=name,
                                  points=[AutomationPoint(0.0, initial)])
            track.automation_lanes.append(lane)
            self.bridge.sync_automation_lane(self.project, track, lane)
            # A freshly added lane must be visible: unfold the track and
            # select the lane so it opens at full (expanded) height.
            self._folded_auto.discard(track_id)
            self._set_selected_lane((track_id, lane_id))
            self._refresh_all()

        def apply_remove() -> None:
            track.automation_lanes[:] = [
                l for l in track.automation_lanes if l.id != lane_id]
            self.bridge.remove_automation_lane(track_id, lane_id)
            self._refresh_all()

        self.undo_stack.push(Command(apply_add, apply_remove,
                                     "Add automation lane"))

    def _on_add_automation_vst_pick(self, track_id: int, fx_slot: int) -> None:
        track = self._fx_track(track_id)
        if track is None:
            return
        # Pick from the current parameter list: the plugin may have
        # restructured it (preset switch) since the last UI tick.
        if self.bridge.vst_refresh_changed_params():
            self._on_vst_params_changed()
        params = self.bridge.vst_params(track_id, fx_slot)
        if not params:
            return
        if fx_slot < 0:
            title = track.instrument.vst_name or "Instrument"
            prefix = title
        else:
            fx = track.effects[fx_slot]
            title = fx.display_name
            prefix = fx.display_name
        dlg = VstParamPickerDialog(params, title, self)
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        picked = dlg.selected()
        if picked is None:
            return
        param_index, param_name = picked
        display = f"{prefix} · {param_name}"
        self._on_add_automation_lane(
            track_id, "inst" if fx_slot < 0 else "fx", fx_slot,
            param_index, display)

    def _on_remove_automation_lane(self, track_id: int, lane_id: int) -> None:
        track = self._fx_track(track_id)
        if track is None:
            return
        lane = next((l for l in track.automation_lanes if l.id == lane_id),
                    None)
        if lane is None:
            return
        saved = AutomationLane(
            id=lane.id, target=lane.target, fx_slot=lane.fx_slot,
            param=lane.param, name=lane.name,
            points=[AutomationPoint(p.beat, p.value, p.shape, p.curve_amount)
                    for p in lane.points])

        def apply_remove() -> None:
            track.automation_lanes[:] = [
                l for l in track.automation_lanes if l.id != lane_id]
            self.bridge.remove_automation_lane(track_id, lane_id)
            self._refresh_all()

        def apply_add() -> None:
            track.automation_lanes.append(saved)
            self.bridge.sync_automation_lane(self.project, track, saved)
            self._refresh_all()

        self.undo_stack.push(Command(apply_remove, apply_add,
                                     "Remove automation lane"))

    def _on_automation_points_changed(self, track_id: int, lane_id: int,
                                      old_pts: list, new_pts: list) -> None:
        track = self._fx_track(track_id)
        if track is None:
            return
        lane = next((l for l in track.automation_lanes if l.id == lane_id),
                    None)
        if lane is None:
            return

        def apply(pts: list) -> None:
            # Tuples are (beat, value[, shape[, curve_amount]]).
            lane.points = [AutomationPoint(*p) for p in pts]
            self.bridge.sync_automation_lane(self.project, track, lane)
            # Light refresh only: this runs from inside the lane item's own
            # mouse handler, so a full scene rebuild (which deletes the item
            # mid-event) would be a use-after-free.
            self.timeline.refresh_automation_lane(track_id, lane_id)

        self.undo_stack.push(Command(lambda: apply(new_pts),
                                     lambda: apply(old_pts),
                                     "Edit automation"))

    # -- tempo / metronome / snap ------------------------------------------

    def _apply_bpm(self, bpm: float) -> None:
        old_bpm = self.project.bpm
        self.project.bpm = bpm
        # Audio clips are re-warped so both their musical position and
        # musical length survive the tempo change (dynamic grid warp).
        # Auto-warp is active only while Snap is on; with Snap off the
        # clips stay at their absolute time positions.
        if abs(old_bpm - bpm) > 1e-9 and self.timeline.snap_enabled:
            self._rewarp_audio_clips_for_bpm(old_bpm, bpm)
        # MIDI clips keep their beat positions; engine frames must move.
        self.bridge.sync_midi(self.project)
        self.transport.set_bpm(bpm)
        self.timeline.refresh_layout()
        self.timeline.viewport().update()

    def _on_bpm_changed(self, bpm: float) -> None:
        if abs(bpm - self.project.bpm) < 1e-9:
            return
        # Spinbox arrows/typing coalesce into one undo step per pause.
        self._record_param_gesture(("bpm",), self.project.bpm, bpm,
                                   self._apply_bpm, "Tempo")
        self._apply_bpm(bpm)

    def _on_metronome(self, on: bool) -> None:
        self.project.metronome = on
        self.bridge.set_metronome(on)

    def _apply_loop(self, start: float, end: float, enabled: bool) -> None:
        self.project.loop_start_beat = start
        self.project.loop_end_beat = end
        self.project.loop_enabled = enabled
        self.transport.set_loop(enabled)
        self.bridge.sync_loop(self.project)
        self.timeline.viewport().update()

    def _on_loop_toggled(self, on: bool) -> None:
        p = self.project
        if p.loop_enabled == on:
            return
        old = (p.loop_start_beat, p.loop_end_beat, p.loop_enabled)
        new = (p.loop_start_beat, p.loop_end_beat, on)
        self.undo_stack.push(Command(
            lambda: self._apply_loop(*new), lambda: self._apply_loop(*old),
            "Loop on" if on else "Loop off"))

    def _selected_clips_beat_span(self) -> tuple[float, float] | None:
        """(start, end) in beats covering all selected timeline clips."""
        start = end = None
        for track_id, clip_id, is_midi in self.timeline._selected_clip_refs():
            track = self.project.track_by_id(track_id)
            if track is None:
                continue
            if is_midi:
                clip = track.midi_clip_by_id(clip_id)
                if clip is None:
                    continue
                c_start, c_end = clip.start_beat, clip.end_beat
            else:
                clip = track.clip_by_id(clip_id)
                if clip is None:
                    continue
                c_start = self.project.frames_to_beats(clip.start_frame)
                c_end = self.project.frames_to_beats(clip.end_frame)
            start = c_start if start is None else min(start, c_start)
            end = c_end if end is None else max(end, c_end)
        if start is None or end is None or end <= start:
            return None
        return start, end

    def _on_loop_shortcut(self) -> None:
        """Ctrl+L: loop over the selected clips, or toggle looping.

        A second Ctrl+L with the same selection (the loop is already
        enabled over exactly that span) switches the loop off, so the
        shortcut works as a toggle in both scenarios.
        """
        span = self._selected_clips_beat_span()
        if span is not None:
            p = self.project
            already = (p.loop_enabled
                       and p.loop_start_beat == span[0]
                       and p.loop_end_beat == span[1])
            if not already:
                self._on_loop_region(*span)
                return
        self.transport.loop_btn.toggle()

    def _on_loop_region(self, start_beat: float, end_beat: float) -> None:
        """Loop region dragged on the ruler: set it and switch looping on."""
        p = self.project
        old = (p.loop_start_beat, p.loop_end_beat, p.loop_enabled)
        new = (start_beat, end_beat, end_beat > start_beat)
        if old == new:
            return
        self.undo_stack.push(Command(
            lambda: self._apply_loop(*new), lambda: self._apply_loop(*old),
            "Loop region"))

    def _on_snap_changed(self, beats: float) -> None:
        self.timeline.snap_beats = beats
        self.timeline.viewport().update()

    def _on_snap_toggled(self, enabled: bool) -> None:
        self.timeline.snap_enabled = enabled
        # Re-enabling Snap: one-time warp of every auto-warp clip onto the
        # current grid (BPM changes made while Snap was off are caught up).
        if enabled:
            warped = 0
            for track in self.project.tracks:
                for clip in track.clips:
                    if clip.auto_warp \
                            and self._warp_existing_clip_to_grid(track.id,
                                                                 clip):
                        warped += 1
            if warped:
                self.statusBar().showMessage(
                    trf("Snap: auto-warped {count} clip(s) to the grid",
                        count=warped), 4000)

    def _on_follow_toggled(self, on: bool) -> None:
        self.timeline.follow_playhead = on

    # -- mixer window ---------------------------------------------------------

    def open_mixer(self) -> None:
        if self._mixer is not None:
            self._mixer.raise_()
            self._mixer.activateWindow()
            return
        mixer = MixerWindow(self.project, self)
        mixer.gain_changed.connect(self._on_gain)
        mixer.pan_changed.connect(self._on_pan)
        mixer.width_changed.connect(self._on_stereo_width)
        mixer.mute_toggled.connect(self._on_mute)
        mixer.solo_toggled.connect(self._on_solo)
        mixer.fx_requested.connect(self._on_mixer_fx_requested)
        mixer.master_gain_changed.connect(self._on_master_gain)
        mixer.undo_requested.connect(self.undo_stack.undo)
        mixer.redo_requested.connect(self.undo_stack.redo)

        def _closed(_result) -> None:
            self._mixer = None

        mixer.finished.connect(_closed)
        self._mixer = mixer
        mixer.show()

    def _sync_track_controls(self, track_id: int) -> None:
        """Mirror a track parameter change into the mixer strip and header."""
        if self._mixer is not None:
            self._mixer.sync_strip(track_id)
        track = self.project.track_by_id(track_id)
        header = self.track_panel.headers.get(track_id)
        if track and header:
            for widget, value in (
                    (header.gain_slider, int(track.gain * 100)),
                    (header.pan_slider, int(track.pan * 100))):
                widget.blockSignals(True)
                widget.setValue(value)
                widget.blockSignals(False)
            for btn, state in ((header.mute_btn, track.mute),
                               (header.solo_btn, track.solo)):
                btn.blockSignals(True)
                btn.setChecked(state)
                btn.blockSignals(False)

    # -- track parameters (continuous controls; gesture-coalesced undo) --------

    def _apply_gain(self, track_id: int, gain: float) -> None:
        track = self.project.track_by_id(track_id)
        if track:
            track.gain = gain
            self.bridge.set_gain(track_id, gain)
            self._sync_track_controls(track_id)

    def _on_gain(self, track_id: int, gain: float) -> None:
        track = self.project.track_by_id(track_id)
        if track:
            self._record_param_gesture(
                ("gain", track_id), track.gain, gain,
                lambda v: self._apply_gain(track_id, v), "Volume")
            self._apply_gain(track_id, gain)
            self._note_tweak(track_id, "volume", -1, 0, "Volume")

    def _apply_pan(self, track_id: int, pan: float) -> None:
        track = self.project.track_by_id(track_id)
        if track:
            track.pan = pan
            self.bridge.set_pan(track_id, pan)
            self._sync_track_controls(track_id)

    def _on_pan(self, track_id: int, pan: float) -> None:
        track = self.project.track_by_id(track_id)
        if track:
            self._record_param_gesture(
                ("pan", track_id), track.pan, pan,
                lambda v: self._apply_pan(track_id, v), "Pan")
            self._apply_pan(track_id, pan)
            self._note_tweak(track_id, "pan", -1, 0, "Pan")

    def _apply_stereo_width(self, track_id: int, width: float) -> None:
        track = self.project.track_by_id(track_id)
        if track:
            track.stereo_width = width
            self.bridge.set_stereo_width(track_id, width)
            # Mirror between the track header and the mixer strip.
            header = self.track_panel.headers.get(track_id)
            if header is not None and hasattr(header, "width_knob"):
                header.width_knob.set_width(width)
            if self._mixer is not None:
                self._mixer.sync_strip(track_id)

    def _on_stereo_width(self, track_id: int, width: float) -> None:
        track = self.project.track_by_id(track_id)
        if track:
            self._record_param_gesture(
                ("width", track_id), track.stereo_width, width,
                lambda v: self._apply_stereo_width(track_id, v),
                "Stereo width")
            self._apply_stereo_width(track_id, width)
            self._note_tweak(track_id, "width", -1, 0, "Stereo width")

    def _apply_mute(self, track_id: int, mute: bool) -> None:
        track = self.project.track_by_id(track_id)
        if track:
            track.mute = mute
            self.bridge.set_mute(track_id, mute)
            self._sync_track_controls(track_id)

    def _on_mute(self, track_id: int, mute: bool) -> None:
        track = self.project.track_by_id(track_id)
        if track is None or track.mute == mute:
            return
        old = track.mute
        self.undo_stack.push(Command(
            lambda: self._apply_mute(track_id, mute),
            lambda: self._apply_mute(track_id, old),
            "Mute track" if mute else "Unmute track"))

    def _apply_solo(self, track_id: int, solo: bool) -> None:
        track = self.project.track_by_id(track_id)
        if track:
            track.solo = solo
            self.bridge.set_solo(track_id, solo)
            self._sync_track_controls(track_id)

    def _on_solo(self, track_id: int, solo: bool) -> None:
        track = self.project.track_by_id(track_id)
        if track is None or track.solo == solo:
            return
        old = track.solo
        self.undo_stack.push(Command(
            lambda: self._apply_solo(track_id, solo),
            lambda: self._apply_solo(track_id, old),
            "Solo track" if solo else "Unsolo track"))

    def _rename_selected_track(self) -> None:
        # Ambiguous with several headers selected: rename nothing.
        if len(self.track_panel.selected_track_ids()) > 1:
            return
        track_id = self.track_panel._selected_track_id
        if track_id is None:
            return
        self._rename_track_dialog(track_id)

    def _rename_track_dialog(self, track_id: int) -> None:
        """Small rename dialog for a specific track (Ctrl+Shift+R and the
        header context menu)."""
        track = self.project.track_by_id(track_id)
        if track is None:
            return
        # Same small dialog as clip rename (OK / Cancel, Escape cancels).
        text, ok = QInputDialog.getText(self, tr("Rename track"), tr("Name:"),
                                        text=track.name)
        if not ok:
            return
        new_name = text.strip()
        if not new_name or new_name == track.name:
            return
        self._on_name(track_id, new_name)

    def _apply_track_properties(self, track_id: int, name: str,
                                color: str) -> None:
        track = self.project.track_by_id(track_id)
        if track is None:
            return
        track.name = name
        track.color = color
        header = self.track_panel.headers.get(track_id)
        if header is not None:
            header.set_track_name(name)
            header.set_accent_color(color)
        if self._mixer is not None:
            self._mixer.rebuild()
        # Group lane tint follows the group's color.
        self.timeline.viewport().update()

    def _on_track_properties(self, track_id: int) -> None:
        track = self.project.track_by_id(track_id)
        if track is None:
            return
        dlg = TrackPropertiesDialog(track, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_name, new_color = dlg.result_values()
        if new_name == track.name and new_color == track.color:
            return
        old_name, old_color = track.name, track.color

        def apply(n: str, c: str) -> None:
            self._apply_track_properties(track_id, n, c)

        self.undo_stack.push(Command(
            lambda: apply(new_name, new_color),
            lambda: apply(old_name, old_color),
            "Track properties"))

    def _on_name(self, track_id: int, name: str) -> None:
        track = self.project.track_by_id(track_id)
        if track is None or track.name == name:
            return
        old_name = track.name

        def apply(n: str) -> None:
            self._apply_track_properties(track_id, n, track.color)

        self.undo_stack.push(Command(
            lambda: apply(name),
            lambda: apply(old_name),
            "Rename track"))

    def _apply_master_gain(self, gain: float) -> None:
        self.project.master_gain = gain
        self.bridge.set_master_gain(gain)
        if self._mixer is not None:
            self._mixer.set_master(gain)
        self.master_header.gain_slider.blockSignals(True)
        self.master_header.gain_slider.setValue(int(gain * 100))
        self.master_header.gain_slider.blockSignals(False)

    def _on_master_gain(self, gain: float) -> None:
        self._record_param_gesture(
            ("master_gain",), self.project.master_gain, gain,
            self._apply_master_gain, "Master volume")
        self._apply_master_gain(gain)
        self._note_tweak(MASTER_FX_ID, "volume", -1, 0, "Master · Volume")

    def _sync_master_header(self) -> None:
        gain = self.project.master_gain
        self.master_header.gain_slider.blockSignals(True)
        self.master_header.gain_slider.setValue(int(gain * 100))
        self.master_header.gain_slider.blockSignals(False)

    # -- transport ---------------------------------------------------------------

    def toggle_play(self) -> None:
        if self._vst_load_progress_depth > 0:
            return
        if self.bridge.is_playing():
            self.bridge.pause()
            self._reset_meters()
        else:
            self.bridge.play()

    def stop_playback(self) -> None:
        """Single stop: pause in place (double click rewinds to zero)."""
        if self._vst_load_progress_depth > 0:
            return
        self.bridge.pause()
        self._reset_meters()

    def rewind_to_start(self) -> None:
        if self._vst_load_progress_depth > 0:
            return
        self.bridge.pause()
        self._reset_meters()
        self.bridge.seek(0)
        self._reset_playhead_smooth()
        self.timeline.set_playhead(0)

    def _reset_meters(self) -> None:
        self.master_header.meter.reset()
        self.track_panel.reset_meters()
        if self._mixer is not None:
            self._mixer.reset_meters()

    def _on_seek(self, frame: int) -> None:
        self.bridge.seek(frame)
        self._reset_playhead_smooth()
        self.timeline.set_playhead(frame)

    def _reset_playhead_smooth(self) -> None:
        self._playhead_smoother.reset()

    def _smooth_playhead(self, raw: int, playing: bool) -> int:
        """Visual smoothing of the engine playhead (see PlayheadSmoother)."""
        return self._playhead_smoother.update(
            raw, playing, self.bridge.sample_rate, self.bridge.buffer_frames)

    def _debug_playhead_tick(self, raw: int, playing: bool,
                             frame: int) -> None:
        """Temporary trace for PYDAW_DEBUG_PLAYHEAD=1 (~4 lines/sec)."""
        import time as _time
        now = _time.perf_counter()
        last = getattr(self, "_dbg_ph_last", 0.0)
        if now - last < 0.25:
            return
        self._dbg_ph_last = now
        print(f"[playhead] raw={raw} playing={playing} smooth={frame} "
              f"sr={self.bridge.sample_rate} buf={self.bridge.buffer_frames} "
              f"audio_ok={self.bridge.audio_ok} "
              f"running={self.bridge.engine.is_running()}",
              flush=True)

    def _on_tick(self) -> None:
        raw = self.bridge.playhead_frame()
        playing = self.bridge.is_playing()
        frame = self._smooth_playhead(raw, playing)
        if os.environ.get("PYDAW_DEBUG_PLAYHEAD"):
            self._debug_playhead_tick(raw, playing, frame)
        self.timeline.set_playhead(frame, playing)
        self.transport.set_time(frame / max(1, self.bridge.sample_rate))
        self.transport.set_playing(playing)
        l, r = self.bridge.master_peaks()
        self.master_header.meter.set_levels(l, r)
        self.track_panel.update_meters(self.bridge)
        if self._mixer is not None:
            self._mixer.update_meters(self.bridge)

        self._update_automation_feedback(raw, playing)

        # Plugins can restructure their parameter list at runtime (preset
        # switch, dynamic GUI): re-read stale lists and rebuild the
        # generic slider panels so new knobs show up everywhere
        # (FX panel, editor windows, automation picker, TWEAK naming).
        if self.bridge.vst_refresh_changed_params():
            self._on_vst_params_changed()

        if self._tweak_mode and not self._tweak_dialog_open:
            hit = self.bridge.vst_consume_last_tweak()
            if hit is not None:
                self._note_vst_tweak(*hit)

        # Piano roll playheads.
        if self._piano_rolls:
            beat = self.project.frames_to_beats(frame)
            for dlg in self._piano_rolls.values():
                local = dlg.clip.source_start_beat + beat - dlg.clip.start_beat
                visible_end = dlg.clip.source_start_beat + dlg.clip.length_beats
                dlg.view.set_playhead_beat(
                    local if dlg.clip.source_start_beat <= local <= visible_end
                    else -1.0)

    def _on_vst_params_changed(self) -> None:
        """A VST changed its parameter list: rebuild generic panels."""
        self.fx_panel.rebuild_vst_param_panels()
        for editor in self._vst_editors.values():
            editor.rebuild_params()

    # -- live automation feedback ---------------------------------------------

    @staticmethod
    def _display_slider(slider, ivalue: int) -> None:
        """Move a slider without emitting signals (skips user drags)."""
        if slider.isSliderDown() or slider.value() == ivalue:
            return
        slider.blockSignals(True)
        slider.setValue(ivalue)
        slider.blockSignals(False)

    def _show_builtin_fx_automation(self, shown_track_id: int, track,
                                    lane, value: float) -> None:
        """Mirror an automated builtin-FX param into the FX panel card."""
        if (self.fx_panel.track is None
                or self.fx_panel.track.id != shown_track_id):
            return
        effects = (self.project.master_effects
                   if shown_track_id == MASTER_FX_ID else track.effects)
        if not (0 <= lane.fx_slot < len(effects)):
            return
        fx = effects[lane.fx_slot]
        if fx.type not in EFFECT_DEFS:
            return
        params = EFFECT_DEFS[fx.type][1]
        if lane.param >= len(params):
            return
        lo, hi = params[lane.param][1:3]
        self.fx_panel.show_builtin_param(lane.fx_slot, lane.param,
                                         lo + (hi - lo) * value)

    def _update_automation_feedback(self, frame: int, playing: bool) -> None:
        """While playing, show automated values on knobs/sliders/panels.

        Values mirror the engine's lane evaluation (same interpolation);
        the model is NOT modified — on stop, widgets snap back to it.
        """
        if not playing:
            if self._auto_feedback_active:
                self._auto_feedback_active = False
                self._restore_controls_from_model()
            return

        beat = self.project.frames_to_beats(frame)
        any_lanes = False

        for track in self.project.tracks:
            gain = pan = width = None
            for lane in track.automation_lanes:
                value = lane.value_at(beat)
                if value is None:
                    continue
                any_lanes = True
                if lane.target == "volume":
                    gain = value * 1.5
                elif lane.target == "pan":
                    pan = value * 2.0 - 1.0
                elif lane.target == "width":
                    width = value * 2.0
                elif lane.target == "fx":
                    self._show_builtin_fx_automation(track.id, track,
                                                     lane, value)
            if gain is None and pan is None and width is None:
                continue
            header = self.track_panel.headers.get(track.id)
            if header is not None:
                if gain is not None:
                    self._display_slider(header.gain_slider,
                                         int(round(gain * 100)))
                if pan is not None:
                    self._display_slider(header.pan_slider,
                                         int(round(pan * 100)))
                if width is not None and hasattr(header, "width_knob"):
                    header.width_knob.set_width(width)
            if self._mixer is not None:
                strip = self._mixer.strips.get(track.id)
                if strip is not None:
                    strip.show_values(gain, pan, width)

        for lane in self.project.master_automation_lanes:
            value = lane.value_at(beat)
            if value is None:
                continue
            any_lanes = True
            if lane.target == "volume":
                mg = value * 1.5
                self._display_slider(self.master_header.gain_slider,
                                     int(round(mg * 100)))
                if self._mixer is not None:
                    self._display_slider(self._mixer.master_slider,
                                         int(round(mg * 100)))
            elif lane.target == "fx":
                self._show_builtin_fx_automation(
                    MASTER_FX_ID, self._master_fx_track, lane, value)

        if any_lanes:
            self._auto_feedback_active = True
            # VST targets: values were applied on the audio thread; push
            # them into the edit controllers so native editors and the
            # generic slider panels follow the automation.
            self.bridge.vst_sync_controllers()
            self.fx_panel.refresh_vst_panels()
            for win in self._vst_editors.values():
                win.refresh_params()

    def _restore_controls_from_model(self) -> None:
        """Snap widgets back to model values after automated playback."""
        for track in self.project.tracks:
            targets = {lane.target for lane in track.automation_lanes}
            if not targets:
                continue
            if targets & {"volume", "pan"}:
                self._sync_track_controls(track.id)
            if "width" in targets:
                header = self.track_panel.headers.get(track.id)
                if header is not None and hasattr(header, "width_knob"):
                    header.width_knob.set_width(track.stereo_width)
                if self._mixer is not None:
                    self._mixer.sync_strip(track.id)
            if "fx" in targets and (self.fx_panel.track is not None
                                    and self.fx_panel.track.id == track.id):
                for lane in track.automation_lanes:
                    if lane.target != "fx":
                        continue
                    if 0 <= lane.fx_slot < len(track.effects):
                        fx = track.effects[lane.fx_slot]
                        if (fx.type in EFFECT_DEFS
                                and lane.param < len(fx.params)):
                            self.fx_panel.show_builtin_param(
                                lane.fx_slot, lane.param,
                                fx.params[lane.param])
        if any(lane.target == "volume"
               for lane in self.project.master_automation_lanes):
            self._sync_master_header()
            if self._mixer is not None:
                self._mixer.set_master(self.project.master_gain)

    # -- settings ------------------------------------------------------------

    def _apply_tooltips_enabled(self, enabled: bool) -> None:
        """Install/remove the global tooltip filter (idempotent)."""
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is None:
            self._tooltips_enabled = enabled
            return
        # removeEventFilter is a no-op when the filter isn't installed,
        # so blindly re-applying the same state is safe.
        app.removeEventFilter(self._tooltip_suppressor)
        if not enabled:
            app.installEventFilter(self._tooltip_suppressor)
        self._tooltips_enabled = enabled

    def open_settings(self) -> None:
        from . import i18n
        old_theme = str(self.settings.value("ui/theme", "dark")).lower()
        old_scale = str(self.settings.value("ui/scale", "1.0"))
        old_language = i18n.normalize(
            self.settings.value("ui/language", ""))
        dlg = SettingsDialog(self.bridge, self._menu_actions,
                             self._default_shortcuts, self.settings, self)
        if not dlg.exec():
            return

        self.settings.setValue("ui/theme", dlg.theme_name)
        self.settings.setValue("ui/scale", dlg.ui_scale)
        self.settings.setValue("ui/language", dlg.language)
        self.settings.setValue("help/tooltips", dlg.tooltips_enabled)
        self._apply_tooltips_enabled(dlg.tooltips_enabled)

        for action_id in dlg.cleared_shortcut_ids:
            self.settings.remove(f"keys/{action_id}")
            if action_id in self._menu_actions:
                self._menu_actions[action_id].setShortcut(
                    self._default_shortcuts.get(action_id, QKeySequence()))

        for action_id, seq_str in dlg.shortcut_overrides.items():
            self.settings.setValue(f"keys/{action_id}", seq_str)
            if action_id in self._menu_actions:
                self._menu_actions[action_id].setShortcut(
                    QKeySequence(seq_str))

        appearance_changed = (dlg.theme_name != old_theme
                              or dlg.ui_scale != old_scale)
        language_changed = dlg.language != old_language

        if language_changed:
            i18n.set_language(dlg.language)
            self._retranslate_ui()

        if dlg.audio_changed:
            if appearance_changed:
                # The new engine picks these up from QSettings after
                # the soft restart below.
                self.settings.setValue("audio/sample_rate", dlg.sample_rate)
                self.settings.setValue("audio/buffer", dlg.buffer_size)
                self.settings.setValue("audio/device_id", dlg.device_id)
                self.settings.setValue("audio/api_name", dlg.api_name)
            else:
                self.bridge.pause()
                ok = self.bridge.restart(dlg.sample_rate, dlg.buffer_size,
                                         dlg.device_id, dlg.api_name)
                self.settings.setValue("audio/sample_rate", dlg.sample_rate)
                self.settings.setValue("audio/buffer", dlg.buffer_size)
                self.settings.setValue("audio/device_id", dlg.device_id)
                self.settings.setValue("audio/api_name", dlg.api_name)
                self.project.sample_rate = self.bridge.sample_rate
                self._reload_files_and_sync()
                if ok:
                    self._show_audio_status(5000)
                else:
                    QMessageBox.warning(self, tr("Settings"),
                                        tr("Could not open the audio device."))

        if appearance_changed:
            self._soft_restart()

    # -- soft restart (theme / UI scale) --------------------------------------

    def _soft_restart(self) -> None:
        """Recreate the whole UI with new theme/scale, keeping the session.

        The project is stashed to a temp file; app.py sees
        ``restart_requested`` after the event loop returns and boots a
        fresh QApplication (QT_SCALE_FACTOR is only read at startup).
        """
        try:
            self.bridge.capture_vst_states(self.project)
            original_path = self.project.file_path
            self.project.save(SESSION_RESTORE_FILE)
            self.project.file_path = original_path
            self.settings.setValue("session/file_path", original_path or "")
            self.settings.setValue("session/restore", True)
        except Exception as exc:  # noqa: BLE001 - restart must not eat work
            QMessageBox.warning(
                self, tr("Settings"),
                trf("Could not save the session for restart:\n{err}\n"
                    "Theme/scale will apply after a manual restart.",
                    err=exc))
            return
        self.restart_requested = True
        self.close()

    def discard_pending_session(self) -> None:
        """Cold start: always begin with a new project, drop stale restore
        state left behind if a soft restart never came back up."""
        self.settings.setValue("session/restore", False)
        try:
            os.remove(SESSION_RESTORE_FILE)
        except OSError:
            pass

    def restore_session_if_pending(self) -> None:
        """Reload the stashed project after a soft restart (called by app.py)."""
        if str(self.settings.value("session/restore", "")).lower() \
                not in ("true", "1"):
            return
        self.settings.setValue("session/restore", False)
        if not os.path.exists(SESSION_RESTORE_FILE):
            return
        original = str(self.settings.value("session/file_path", ""))
        try:
            self._load_project_from_path(SESSION_RESTORE_FILE)
        finally:
            try:
                os.remove(SESSION_RESTORE_FILE)
            except OSError:
                pass
        self.project.file_path = original or None
        if original:
            self.setWindowTitle(f"PyDAW - {os.path.basename(original)}")
        else:
            self.setWindowTitle("PyDAW")

    def _reload_files_and_sync(self) -> None:
        missing: list[str] = []
        for track in self.project.tracks:
            for clip in list(track.clips):
                try:
                    clip.file_id, frames = load_clip_audio(self.bridge, clip)
                    clip.length = min(clip.length, frames)
                except Exception:  # noqa: BLE001
                    missing.append(clip.file_path)
                    track.clips.remove(clip)
        if missing:
            QMessageBox.warning(
                self, tr("Missing files"),
                tr("Some audio files were not found:\n") + "\n".join(missing))
        self._sync_project_with_vst_progress(self.project)
        self._show_sync_errors()
        self._refresh_all()

    # -- project I/O ---------------------------------------------------------------

    def new_project(self) -> None:
        for dlg in list(self._piano_rolls.values()):
            dlg.close()
        self._close_all_vst_editors()
        self._selected_track_id = None
        self.bridge.engine.clear_all()
        if hasattr(self.bridge.engine, "drain_commands"):
            self.bridge.engine.drain_commands()
        self.project.tracks.clear()
        self.project.master_effects.clear()
        self.project.master_automation_lanes.clear()
        self.project.file_path = None
        self.project.name = "Untitled"
        self.project.bpm = 120.0
        self.project.metronome = False
        self.project.loop_start_beat = 0.0
        self.project.loop_end_beat = 0.0
        self.project.loop_enabled = False
        self._folded_groups.clear()
        self._folded_auto.clear()
        self.transport.set_bpm(120.0)
        self.transport.set_metronome(False)
        self.transport.set_loop(False)
        self.bridge.set_tempo(120.0)
        self.bridge.set_metronome(False)
        self.bridge.sync_loop(self.project)
        self._sync_master_header()
        self._discard_param_gesture()
        self.undo_stack.clear()
        self._refresh_all()
        self.add_track()
        self.add_midi_track()
        self.undo_stack.clear()
        self.setWindowTitle("PyDAW")

    def _show_sync_errors(self) -> None:
        if not self.bridge.last_sync_errors:
            return
        msg = trf("Some VST plugins failed to load:\n{errors}",
                  errors="\n".join(self.bridge.last_sync_errors))
        self.statusBar().showMessage(msg, 8000)
        QMessageBox.warning(self, tr("VST plugins"), msg)

    def save_project(self) -> None:
        if self.project.file_path:
            self._save_project_to(self.project.file_path)
        else:
            self.save_project_as()

    def save_project_as(self) -> None:
        default_name = self.project.name or "Untitled"
        projects_dir = default_projects_dir()
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Save project"),
            os.path.join(projects_dir, default_name), PROJECT_FILTER)
        if not path:
            return
        # The chosen name becomes a project folder with the .pydaw file
        # (and a samples/ subfolder) inside it.
        root, _ext = os.path.splitext(path)
        name = os.path.basename(root) or "Untitled"
        project_dir = root
        file_path = os.path.join(project_dir, name + ".pydaw")
        try:
            os.makedirs(project_dir, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, tr("Save failed"),
                                trf("Cannot create project folder:\n{err}",
                                    err=exc))
            return
        self.project.name = name
        self._save_project_to(file_path)
        self.setWindowTitle(f"PyDAW - {os.path.basename(file_path)}")

    def _save_project_to(self, file_path: str) -> None:
        """Save the project file and copy used audio into samples/."""
        self.bridge.capture_vst_states(self.project)
        try:
            self._collect_samples(os.path.dirname(file_path))
            self.project.save(file_path)
        except OSError as exc:
            QMessageBox.warning(self, tr("Save failed"), str(exc))
            return
        self.statusBar().showMessage(trf("Saved {path}", path=file_path), 3000)

    def _collect_samples(self, project_dir: str) -> None:
        """Copy audio files used by clips into <project>/samples/."""
        samples_dir = os.path.join(project_dir, "samples")
        os.makedirs(samples_dir, exist_ok=True)
        project_abs = os.path.abspath(project_dir)
        copied: dict[str, str] = {}
        for track in self.project.tracks:
            for clip in track.clips:
                src = clip.file_path
                if not src or not os.path.exists(src):
                    continue
                src_abs = os.path.abspath(src)
                try:
                    inside = os.path.commonpath(
                        [src_abs, project_abs]) == project_abs
                except ValueError:
                    inside = False  # different drives
                if inside:
                    continue  # already lives in the project folder
                if src_abs in copied:
                    clip.file_path = copied[src_abs]
                    continue
                base = os.path.basename(src_abs)
                stem, ext = os.path.splitext(base)
                dest = os.path.join(samples_dir, base)
                n = 1
                while os.path.exists(dest) and \
                        os.path.getsize(dest) != os.path.getsize(src_abs):
                    dest = os.path.join(samples_dir, f"{stem}_{n}{ext}")
                    n += 1
                if not os.path.exists(dest):
                    shutil.copy2(src_abs, dest)
                copied[src_abs] = dest
                clip.file_path = dest

    def open_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Open project"), default_projects_dir(), PROJECT_FILTER)
        if not path:
            return
        self._load_project_from_path(path)

    def _load_project_from_path(self, path: str) -> None:
        try:
            project = Project.load(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, tr("Open failed"), str(exc))
            return

        for dlg in list(self._piano_rolls.values()):
            dlg.close()
        self._close_all_vst_editors()
        self._selected_track_id = None

        # Re-register audio files with the engine.
        missing: list[str] = []
        for track in project.tracks:
            for clip in list(track.clips):
                try:
                    clip.file_id, _ = load_clip_audio(self.bridge, clip)
                except Exception:  # noqa: BLE001
                    missing.append(clip.file_path)
                    track.clips.remove(clip)
        if missing:
            QMessageBox.warning(
                self, tr("Missing files"),
                tr("Some audio files were not found:\n") + "\n".join(missing))

        project.sample_rate = self.bridge.sample_rate
        self.project = project
        self.timeline.project = project
        self.track_panel.project = project
        self._folded_groups.clear()
        self._folded_auto.clear()
        if self._mixer is not None:
            self._mixer.project = project
        self._sync_project_with_vst_progress(project)
        self._show_sync_errors()
        self.transport.set_bpm(project.bpm)
        self.transport.set_metronome(project.metronome)
        self.transport.set_loop(project.loop_enabled)
        self._sync_master_header()
        self._discard_param_gesture()
        self.undo_stack.clear()
        self._refresh_all()
        self.setWindowTitle(f"PyDAW - {os.path.basename(path)}")

    def _export_default_basename(self) -> str:
        if self.project.file_path:
            return self.project.name or "Untitled"
        return "Untitled"

    def export_mix(self) -> None:
        if self.project.total_frames <= 0:
            QMessageBox.information(
                self, tr("Export"), tr("Nothing to export yet."))
            return
        has_loop = (self.project.loop_enabled
                    and self.project.loop_end_beat > self.project.loop_start_beat)
        dlg = ExportDialog(self.project, has_loop, self)
        if dlg.exec() != QDialog.Accepted:
            return
        formats = dlg.formats          # [(fmt_key, option)]
        track_ids = dlg.track_ids      # MASTER_ID = full mix
        if not formats or not track_ids:
            return

        export_name = self._export_default_basename()
        base, _ = QFileDialog.getSaveFileName(
            self, tr("Export: base file name"),
            os.path.join(default_export_dir(), export_name),
            tr("Audio files (*.wav *.flac *.mp3)"))
        if not base:
            return
        root, ext = os.path.splitext(base)
        if ext.lower() in (".wav", ".flac", ".mp3"):
            base = root

        start_frame, end_frame = 0, -1
        if dlg.use_loop:
            start_frame = self.project.beats_to_frames(
                self.project.loop_start_beat)
            end_frame = self.project.beats_to_frames(
                self.project.loop_end_beat)

        # Unique file suffix per selected track.
        suffixes: dict[int, str] = {}
        for tid in track_ids:
            if tid == MASTER_ID:
                suffixes[tid] = ""
                continue
            track = self.project.track_by_id(tid)
            name = safe_filename(track.name if track else str(tid))
            suffix = f"_{name}"
            if suffix in suffixes.values():
                suffix = f"_{name}_{tid}"
            suffixes[tid] = suffix

        jobs: list[tuple[int, str, str, str]] = []
        for tid in track_ids:
            for fmt, option in formats:
                jobs.append((tid, fmt, option,
                             f"{base}{suffixes[tid]}.{fmt}"))

        self.bridge.pause()
        # The worker thread is the only command producer while the modal
        # progress dialog blocks the UI; pause the 60 Hz tick so it does
        # not race the SPSC command queue.
        self._timer.stop()
        try:
            done, errors, cancelled = self._run_export_jobs(
                jobs, start_frame, end_frame)
        finally:
            self._timer.start()

        if cancelled:
            self.statusBar().showMessage(
                trf("Export cancelled ({count} file(s) done)",
                    count=len(done)), 7000)
        elif errors:
            QMessageBox.warning(
                self, tr("Export"),
                trf("Done: {done}\nErrors:\n{errors}",
                    done=len(done), errors="\n".join(errors)))
        elif done:
            self.statusBar().showMessage(
                trf("Exported {count} file(s): {first}...",
                    count=len(done), first=done[0]), 7000)

    def _run_export_jobs(self, jobs, start_frame: int, end_frame: int
                         ) -> tuple[list[str], list[str], bool]:
        """Render `jobs` [(track_id, fmt, option, path)] on a worker thread
        while a modal dialog shows engine render progress with Cancel."""
        import threading

        from PySide6.QtWidgets import QProgressDialog

        done: list[str] = []
        errors: list[str] = []
        cancel = threading.Event()
        finished = threading.Event()
        state = {"job": 0}

        def worker() -> None:
            try:
                for i, (tid, fmt, option, path) in enumerate(jobs):
                    state["job"] = i
                    if cancel.is_set():
                        break
                    self._solo_for_export(tid)
                    try:
                        ok = export_mix_to_file(
                            self.bridge, path, fmt, option,
                            start_frame, end_frame)
                    except Exception as exc:
                        errors.append(f"{os.path.basename(path)}: {exc}")
                        continue
                    if ok:
                        done.append(path)
                    elif self.bridge.export_was_cancelled():
                        break
                    else:
                        errors.append(f"{os.path.basename(path)}: "
                                      "рендер не удался")
            finally:
                self._restore_export_routing()
                finished.set()

        progress = QProgressDialog(tr("Exporting..."), tr("Cancel"), 0, 1000, self)
        progress.setWindowTitle(tr("Export"))
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)

        def on_cancel() -> None:
            cancel.set()
            self.bridge.cancel_export()

        progress.canceled.connect(on_cancel)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        poll = QTimer(self)
        poll.setInterval(50)

        def on_poll() -> None:
            if finished.is_set():
                poll.stop()
                # close() may fire canceled() on some styles; the work is
                # already done, so detach the handler first.
                try:
                    progress.canceled.disconnect(on_cancel)
                except (RuntimeError, TypeError):
                    pass
                progress.close()
                return
            n = max(1, len(jobs))
            frac = (state["job"] + self.bridge.export_progress()) / n
            job = jobs[min(state["job"], n - 1)]
            progress.setLabelText(
                f"Экспорт {state['job'] + 1}/{n}: "
                f"{os.path.basename(job[3])}")
            progress.setValue(int(min(1.0, frac) * 1000))

        poll.timeout.connect(on_poll)
        poll.start()
        progress.exec()
        # Dialog closed (finished or cancelled): wait for the worker to
        # wind down so routing is restored before we touch the engine.
        if not finished.is_set():
            cancel.set()
            self.bridge.cancel_export()
        thread.join(timeout=30.0)
        poll.stop()
        poll.deleteLater()
        return done, errors, cancel.is_set()

    def _solo_for_export(self, track_id: int) -> None:
        """Offline-render routing: mute everything except the exported track
        (its group bus stays open so the child is processed by its chain).
        MASTER_ID restores the normal project routing."""
        if track_id == MASTER_ID:
            self._restore_export_routing()
            return
        keep: set[int] = {track_id}
        track = self.project.track_by_id(track_id)
        if track is not None:
            if track.is_group:
                keep |= {t.id for t in self.project.tracks
                         if t.group_id == track_id}
            elif track.group_id:
                keep.add(track.group_id)
        for t in self.project.tracks:
            self.bridge.set_mute(t.id, t.id not in keep)
            self.bridge.set_solo(t.id, False)

    def _restore_export_routing(self) -> None:
        for t in self.project.tracks:
            self.bridge.set_mute(t.id, t.mute)
            self.bridge.set_solo(t.id, t.solo)

    # -- helpers --------------------------------------------------------------------

    def _refresh_all(self) -> None:
        self._sync_master_proxy()
        self.track_panel._folded_groups = set(self._folded_groups)
        self.timeline._folded_groups = set(self._folded_groups)
        self.track_panel._folded_auto = set(self._folded_auto)
        self.timeline._folded_auto = set(self._folded_auto)
        # Drop lane selection if the selected lane no longer exists.
        selected_lane = self.timeline.selected_lane()
        if selected_lane is not None:
            track = self._fx_track(selected_lane[0])
            if track is None or all(l.id != selected_lane[1]
                                    for l in track.automation_lanes):
                self._set_selected_lane(None)
        self.track_panel.rebuild()
        self.timeline.rebuild()
        if self._mixer is not None:
            self._mixer.rebuild()
        if self._selected_track_id is not None:
            track = self._fx_track(self._selected_track_id)
            if track is None:
                self._selected_track_id = None
                self.fx_panel.set_track(None)
            else:
                self.fx_panel.set_track(track)
        elif self.fx_panel.track is not None:
            # Selection was reset (new/open project): drop the stale track.
            self.fx_panel.set_track(None)
        self.master_header.set_selected(
            self._selected_track_id == MASTER_FX_ID)
        self.track_panel.set_selected_track(
            None if self._selected_track_id == MASTER_FX_ID
            else self._selected_track_id)
        self.fx_panel.rebuild()
        # Close piano rolls whose clip no longer exists.
        alive = {c.id for t in self.project.tracks for c in t.midi_clips}
        for clip_id in list(self._piano_rolls):
            if clip_id not in alive:
                self._close_piano_roll(clip_id)

    def closeEvent(self, event) -> None:
        self._timer.stop()
        self._close_all_vst_editors()
        self.bridge.shutdown()
        super().closeEvent(event)
