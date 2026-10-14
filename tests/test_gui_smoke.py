"""GUI smoke test: exercises the main new UI paths without user input.

Creates the main window off-screen, drives clip/note editing, the loop
region, zoom/duplicate, the mixer window and undo/redo, then quits.
Exit code 0 = pass.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QEvent, QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QKeyEvent, QKeySequence, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import (QApplication, QDialog,
                               QGraphicsSceneMouseEvent, QPushButton,
                               QTabWidget)

from pydaw.ui.main_window import MainWindow
from pydaw.ui.settings_dialog import SettingsDialog
from pydaw.ui import theme
from pydaw.ui.theme import apply_theme, set_theme
from pydaw.ui.timeline import (LANE_ALT_COLOR, LANE_COLOR, LANE_GROUP_COLOR,
                               RULER_HEIGHT, lane_fill_color)
from pydaw.ui.track_properties_dialog import TrackPropertiesDialog
from pydaw.ui.plugin_browser import PluginBrowserDialog, PluginInfo
from pydaw.model.commands import Command
from pydaw.model.project import (AutomationPoint, Clip, Effect, FORMAT_VERSION,
                                  MidiNote, Project, Track, bump_id_counter,
                                  next_id)


def _icon_alpha_bounds(icon, size: int = 20) -> tuple[int, int, int, int, int]:
    img = icon.pixmap(size, size).toImage()
    xs: list[int] = []
    ys: list[int] = []
    count = 0
    for y in range(img.height()):
        for x in range(img.width()):
            if img.pixelColor(x, y).alpha() > 0:
                xs.append(x)
                ys.append(y)
                count += 1
    return min(xs), min(ys), max(xs), max(ys), count


def _icon_alpha_at(icon, x: int, y: int, size: int = 20) -> int:
    return icon.pixmap(size, size).toImage().pixelColor(x, y).alpha()


def _icon_alpha_runs(icon, y: int, x0: int, x1: int,
                     size: int = 20) -> int:
    img = icon.pixmap(size, size).toImage()
    runs = 0
    in_run = False
    for x in range(x0, x1 + 1):
        alpha = img.pixelColor(x, y).alpha() > 0
        if alpha and not in_run:
            runs += 1
        in_run = alpha
    return runs


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    apply_theme(app)
    win = MainWindow()
    win.show()
    app.processEvents()

    # Two default tracks: audio + MIDI — each gets a palette color.
    assert len(win.project.tracks) == 2
    for t in win.project.tracks:
        assert t.color in theme.TRACK_COLORS
    midi_track = next(t for t in win.project.tracks if t.is_midi)
    assert not midi_track.instrument.uses_vst
    assert midi_track.instrument.gain == 0.0
    midi_header = win.track_panel.headers[midi_track.id]
    assert midi_header.inst_label.text() == "No instrument"
    assert not hasattr(win.track_panel, "add_btn")
    assert hasattr(win, "master_header")
    assert not hasattr(win.master_header, "_name_label")
    assert not hasattr(win.transport, "master_slider")

    # Master header: gain sync via _on_master_gain.
    win._on_master_gain(1.2)
    assert win.master_header.gain_slider.value() == 120
    assert abs(win.project.master_gain - 1.2) < 1e-9
    # Continuous controls are undoable: the move is one coalesced gesture.
    win._finish_param_gesture()
    win.undo_stack.undo()
    assert abs(win.project.master_gain - 1.0) < 1e-9
    assert win.master_header.gain_slider.value() == 100
    win.undo_stack.redo()
    assert abs(win.project.master_gain - 1.2) < 1e-9
    assert win.master_header.gain_slider.value() == 120

    # Snap toggle: disabled snap returns raw frame; enabled snaps to grid.
    win.timeline.snap_enabled = False
    assert win.timeline.snap_frame(12345) == 12345
    win.timeline.snap_enabled = True
    win.timeline.snap_beats = 1.0
    fpb = win.project.frames_per_beat
    assert win.timeline.snap_frame(int(2.3 * fpb)) == int(2 * fpb)
    win.timeline.snap_beats = 0.0625
    assert win.timeline._grid_step_beats(200.0) == 0.0625
    win.timeline.snap_beats = 0.0
    assert win.timeline._grid_step_beats(200.0) < 1.0
    win.timeline.snap_beats = 1.0

    # Transport ergonomics: no standalone "Snap" label (the button says it),
    # mixer button is the last widget of the toolbar group.
    from PySide6.QtWidgets import QLabel as _QLabel
    assert "Snap" not in [w.text()
                          for w in win.transport.findChildren(_QLabel)]
    _tl = win.transport.layout()
    _tw = [_tl.itemAt(i).widget() for i in range(_tl.count())]
    _tw = [w for w in _tw if w is not None]
    assert _tw.index(win.transport.mixer_btn) \
        > _tw.index(win.transport.slice_btn)
    assert _tw[-1] is win.transport.mixer_btn
    assert hasattr(win.transport, "mouse_btn")
    assert hasattr(win.transport, "select_btn")
    assert win.transport.mouse_btn.isChecked()
    assert not win.transport.select_btn.isChecked()
    assert _tw.index(win.transport.mouse_btn) < _tw.index(win.transport.select_btn) \
        < _tw.index(win.transport.slice_btn)
    # Icon-only tools: Mouse is a filled cursor arrow; Select is a centered,
    # thin, frequent dashed square.
    assert _icon_alpha_at(win.transport.mouse_btn.icon(), 6, 6) > 0
    _sx0, _sy0, _sx1, _sy1, _scount = _icon_alpha_bounds(
        win.transport.select_btn.icon())
    assert abs(((_sx0 + _sx1) / 2.0) - 9.5) <= 0.75
    assert abs(((_sy0 + _sy1) / 2.0) - 9.5) <= 0.75
    assert _icon_alpha_runs(win.transport.select_btn.icon(), _sy0,
                            _sx0, _sx1) >= 4

    # Manual BPM edit commits and leaves edit mode when the user clicks
    # somewhere else in the UI.
    _bpm_edit = win.transport.bpm_spin.lineEdit()
    _bpm_edit.setFocus(Qt.MouseFocusReason)
    _bpm_edit.selectAll()
    _bpm_edit.setText("133.0")
    assert _bpm_edit.hasFocus()
    _target = win.transport.play_btn
    _pos = _target.rect().center()
    _bpm_outside_click = QMouseEvent(
        QEvent.MouseButtonPress, QPointF(_pos),
        QPointF(_target.mapToGlobal(_pos)),
        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    app.sendEvent(_target, _bpm_outside_click)
    app.processEvents()
    assert not _bpm_edit.hasFocus()
    assert abs(win.transport.bpm_spin.value() - 133.0) < 1e-6
    assert abs(win.project.bpm - 133.0) < 1e-6
    win._apply_bpm(120.0)
    win._discard_param_gesture()

    # Snap resolutions: no "Off" entry (the Snap button toggles snapping),
    # finer 1/8 and 1/16 grids are available.
    from pydaw.ui.transport_bar import SNAP_CHOICES
    snap_labels = [label for label, _ in SNAP_CHOICES]
    snap_values = [beats for _, beats in SNAP_CHOICES]
    assert "Off" not in snap_labels
    assert 0.125 in snap_values and 0.0625 in snap_values
    assert all(b > 0 for b in snap_values)

    # Clip fade overlays use a bezier envelope edge (not a straight
    # triangle): the path contains curve elements.
    from pydaw.ui.clip_item import fade_overlay_path, waveform_source_window
    _fade_path = fade_overlay_path(0.0, 24.0, 40.0)
    assert any(_fade_path.elementAt(i).isCurveTo()
               for i in range(_fade_path.elementCount()))
    _wave_clip = Clip(id=-1, file_path="", file_id=0, start_frame=0,
                      length=2000, source_offset=4000, stretch_ratio=2.0)
    assert waveform_source_window(_wave_clip) == (2000, 1000)
    assert waveform_source_window(_wave_clip, 1000) == (2000, 500)

    # Clip Properties dialog: MIDI mode exposes only the name.
    from pydaw.ui.clip_properties import ClipPropertiesDialog
    _mdlg = ClipPropertiesDialog("My MIDI", midi=True)
    assert _mdlg._warp_combo.isHidden()
    _mdlg._name_edit.setText("New name")
    assert _mdlg.values() == ("New name", "", True)
    _mdlg.deleteLater()
    _adlg = ClipPropertiesDialog("Audio", "stretch", 1.0, False)
    assert not _adlg._warp_combo.isHidden()
    # Auto-warp checkbox reflects the clip flag and is reported back.
    assert not _adlg._auto_warp_check.isChecked()
    _adlg._auto_warp_check.setChecked(True)
    assert _adlg.values()[2] is True
    _adlg.deleteLater()

    # Meters expose a 0 dBFS reference (headroom above the mark).
    from pydaw.ui.track_header import METER_HEADROOM
    assert METER_HEADROOM > 1.0

    # Bottom pad below the last track: room for the right-click add-track
    # menu both in the panel and on the timeline.
    from pydaw.ui.track_header import PANEL_BOTTOM_PAD
    from pydaw.ui.timeline import BOTTOM_PAD as TIMELINE_BOTTOM_PAD
    # The pad may be grown a bit so the panel scroll range matches the
    # timeline's (whose viewport is shorter by its horizontal scrollbar).
    assert win.track_panel._bottom_pad.height() >= PANEL_BOTTOM_PAD
    assert win.timeline._scene.sceneRect().height() >= \
        30 + win.timeline._total_rows_height() + TIMELINE_BOTTOM_PAD - 1

    # Selecting an automation lane doubles its height (panel + timeline).
    from pydaw.ui.timeline import (AUTO_LANE_HEIGHT as _ALH,
                                   AUTO_LANE_HEIGHT_SELECTED as _ALHS)
    _atrack = win.project.tracks[0]
    win._on_add_automation_lane(_atrack.id, "volume", -1, 0, "Volume")
    app.processEvents()
    _lane = _atrack.automation_lanes[0]
    _key = (_atrack.id, _lane.id)
    # A freshly created lane is auto-selected -> expanded height.
    assert win.timeline.selected_lane() == _key
    assert win.timeline.row_info_for_lane(*_key)["height"] == _ALHS
    assert win.track_panel.auto_headers[_key].height() == _ALHS
    win._on_automation_lane_clicked(*_key)  # click collapses
    app.processEvents()
    assert win.timeline.row_info_for_lane(*_key)["height"] == _ALH
    win._on_automation_lane_clicked(*_key)  # click expands again
    app.processEvents()
    assert win.timeline.row_info_for_lane(*_key)["height"] == _ALHS
    win._on_automation_lane_clicked(*_key)  # collapse before removal
    app.processEvents()
    win._on_remove_automation_lane(_atrack.id, _lane.id)
    win.undo_stack.clear()
    app.processEvents()

    # Add-track context menu (no + Audio/+ MIDI buttons).
    menu = win.track_panel.build_add_track_menu()
    assert menu.actions()[0].text().startswith("Add audio track")
    assert menu.actions()[1].text().startswith("Add MIDI track")
    assert menu.actions()[2].text() == "Add group track (Ctrl+Shift+G)"
    before = len(win.project.tracks)
    win.track_panel.add_midi_track_requested.emit()
    app.processEvents()
    new_midi = win.project.tracks[-1]
    assert new_midi.is_midi
    assert new_midi.instrument.gain == 0.0
    assert not new_midi.instrument.uses_vst
    assert len(win.project.tracks) == before + 1

    # MIDI clip + piano roll.
    win.add_midi_clip(midi_track.id, 0.0)
    clip = midi_track.midi_clips[0]
    win.open_piano_roll(midi_track.id, clip.id)
    app.processEvents()
    dlg = win._piano_rolls[clip.id]

    # Single note, then a multi-note paste (as Ctrl+V would emit).
    win._on_note_add(midi_track.id, clip.id, 60, 0.0, 1.0)
    win._on_notes_add(midi_track.id, clip.id,
                      [(1.0, 64, 1.0, 0.8), (2.0, 67, 1.0, 0.6)])
    assert len(clip.notes) == 3

    # Group edit: move both pasted notes up a semitone.
    changes = [(n.id, (n.pitch, n.start_beat, n.length_beats),
                (n.pitch + 1, n.start_beat, n.length_beats))
               for n in clip.notes[1:]]
    win._on_notes_change(midi_track.id, clip.id, changes)
    assert clip.notes[1].pitch == 65 and clip.notes[2].pitch == 68

    # Velocity lane edit.
    win._on_velocities_change(midi_track.id, clip.id,
                              [(clip.notes[0].id, 1.0, 0.5)])
    assert abs(clip.notes[0].velocity - 0.5) < 1e-9

    # Piano roll toolbar + local snap.
    assert hasattr(dlg, "snap_btn") and hasattr(dlg, "slice_btn")
    assert hasattr(dlg, "mouse_btn") and hasattr(dlg, "select_btn")
    assert dlg.mouse_btn.isChecked()
    assert _icon_alpha_at(dlg.mouse_btn.icon(), 6, 6) > 0
    assert dlg.view.tool == "mouse"
    assert hasattr(dlg, "help_btn")
    assert not hasattr(dlg, "grid_combo")
    dlg.view.snap_enabled = False
    assert abs(dlg.view.snap(0.37) - 0.37) < 1e-9
    dlg.view.snap_enabled = True
    dlg.view.snap_beats = 0.25
    assert abs(dlg.view.snap(0.37) - 0.25) < 1e-9
    from pydaw.ui.piano_roll import RUBBER_FILL
    assert RUBBER_FILL.alpha() == 0

    # Ctrl-dragging a note moves it freely, bypassing snap.
    dlg.view.snap_enabled = True
    dlg.view.snap_beats = 1.0
    move_note = clip.notes[0]
    old_start = move_note.start_beat
    r = dlg.view.note_rect(move_note)
    start_vp = dlg.view.mapFromScene(r.center())
    press = QMouseEvent(
        QEvent.MouseButtonPress, QPointF(start_vp),
        QPointF(dlg.view.mapToGlobal(start_vp)),
        Qt.LeftButton, Qt.LeftButton, Qt.ControlModifier)
    dlg.view.mousePressEvent(press)
    move_scene = r.center()
    move_scene.setX(move_scene.x() + 0.37 * dlg.view.px_per_beat)
    move_vp = dlg.view.mapFromScene(move_scene)
    move_ev = QMouseEvent(
        QEvent.MouseMove, QPointF(move_vp),
        QPointF(dlg.view.mapToGlobal(move_vp)),
        Qt.LeftButton, Qt.LeftButton, Qt.ControlModifier)
    dlg.view.mouseMoveEvent(move_ev)
    release = QMouseEvent(
        QEvent.MouseButtonRelease, QPointF(move_vp),
        QPointF(dlg.view.mapToGlobal(move_vp)),
        Qt.LeftButton, Qt.NoButton, Qt.ControlModifier)
    dlg.view.mouseReleaseEvent(release)
    app.processEvents()
    assert abs(move_note.start_beat - (old_start + 0.37)) < 0.02, \
        move_note.start_beat

    # Selection helpers on the view itself.
    dlg.view.select_all()
    assert len(dlg.view.selection) == 3
    dlg.view.copy_selection()
    dlg.view.duplicate_selection()
    app.processEvents()
    assert len(clip.notes) == 6

    # Piano roll tools are local: selecting the piano-roll select tool does
    # not change the main-window tool, and the select tool enables rubber-band
    # selection without holding Ctrl.
    win._on_tool_selected("mouse")
    dlg._set_tool("select")
    assert dlg.view.tool == "select"
    assert dlg.select_btn.isChecked()
    assert win.timeline.tool == "mouse"
    dlg.view.selection.clear()
    _notes_for_rubber = clip.notes[:2]
    _r0 = dlg.view.note_rect(_notes_for_rubber[0]).adjusted(-4, -4, 4, 4)
    _r1 = dlg.view.note_rect(_notes_for_rubber[1]).adjusted(-4, -4, 4, 4)
    _rub_scene = _r0.united(_r1)
    _rub_a = dlg.view.mapFromScene(_rub_scene.topLeft())
    _rub_b = dlg.view.mapFromScene(_rub_scene.bottomRight())
    dlg.view.mousePressEvent(QMouseEvent(
        QEvent.MouseButtonPress, QPointF(_rub_a),
        QPointF(dlg.view.mapToGlobal(_rub_a)),
        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    dlg.view.mouseMoveEvent(QMouseEvent(
        QEvent.MouseMove, QPointF(_rub_b),
        QPointF(dlg.view.mapToGlobal(_rub_b)),
        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    dlg.view.mouseReleaseEvent(QMouseEvent(
        QEvent.MouseButtonRelease, QPointF(_rub_b),
        QPointF(dlg.view.mapToGlobal(_rub_b)),
        Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
    assert {n.id for n in _notes_for_rubber} <= dlg.view.selection
    dlg._set_tool("mouse")
    assert dlg.mouse_btn.isChecked()

    # Group delete + undo.
    win._on_notes_remove(midi_track.id, clip.id,
                         [n.id for n in clip.notes[3:]])
    assert len(clip.notes) == 3
    win.undo_stack.undo()
    assert len(clip.notes) == 6
    win.undo_stack.undo()

    # Slice note: single undo restores original.
    clip.notes.clear()
    win._on_note_add(midi_track.id, clip.id, 60, 0.0, 2.0)
    app.processEvents()
    assert len(clip.notes) == 1
    note = clip.notes[0]
    win._on_note_slice(midi_track.id, clip.id, note.id, 1.0)
    app.processEvents()
    assert len(clip.notes) == 2
    left, right = sorted(clip.notes, key=lambda n: n.start_beat)
    assert left.pitch == 60 and abs(left.length_beats - 1.0) < 1e-9
    assert right.pitch == 60 and abs(right.length_beats - 1.0) < 1e-9
    win.undo_stack.undo()
    app.processEvents()
    assert len(clip.notes) == 1
    assert abs(clip.notes[0].length_beats - 2.0) < 1e-9

    # Timeline slice must not reset MIDI note positions in piano-roll space.
    win.add_midi_clip(midi_track.id, 8.0)
    split_clip = midi_track.midi_clips[-1]
    win._on_note_add(midi_track.id, split_clip.id, 60, 0.0, 0.75)
    win._on_note_add(midi_track.id, split_clip.id, 64, 1.5, 0.5)
    win._on_clip_slice(midi_track.id, split_clip.id, 9.0, True)
    app.processEvents()
    split_parts = sorted(
        [c for c in midi_track.midi_clips if c.start_beat in (8.0, 9.0)],
        key=lambda c: c.start_beat)
    assert len(split_parts) == 2
    left_part, right_part = split_parts
    assert abs(left_part.length_beats - 1.0) < 1e-9
    assert abs(right_part.source_start_beat - 1.0) < 1e-9
    assert right_part.notes and abs(right_part.notes[0].start_beat - 1.5) < 1e-9
    for _ in range(4):  # split, two note-adds, add-clip
        win.undo_stack.undo()
    app.processEvents()

    # Piano roll canvas follows clip length and window width.
    ppb = dlg.view.px_per_beat
    win._on_midi_clip_resized(midi_track.id, clip.id, clip.length_beats, 32.0)
    app.processEvents()
    assert dlg.view._scene.sceneRect().width() >= 32 * ppb + 200
    win.undo_stack.undo()
    app.processEvents()
    assert dlg.view._scene.sceneRect().width() >= \
        dlg.view.viewport().width()

    # Stop button: single press pauses in place, double press rewinds to 0.
    def _wait_playhead(frame: int) -> bool:
        # Seeks are applied on the audio thread; give it a moment.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if win.bridge.playhead_frame() == frame:
                return True
            app.processEvents()
            time.sleep(0.01)
        return False

    win.bridge.seek(48000)
    assert _wait_playhead(48000)
    win.stop_playback()
    app.processEvents()
    assert win.bridge.playhead_frame() == 48000
    win.rewind_to_start()
    assert _wait_playhead(0)

    # VST load progress: no lower-left cancel button, and it blocks starting
    # playback while stopped without stopping already-running playback.
    _progress = win._make_vst_load_progress_dialog("Loading")
    assert not _progress.findChildren(QPushButton)
    _progress.close()
    _last_progress = []
    _orig_make_progress = win._make_vst_load_progress_dialog
    _orig_vst_is_loading = win.bridge.vst_is_loading
    _orig_vst_diag = win.bridge.vst_diagnostic
    _orig_is_playing_for_wait = win.bridge.is_playing
    try:
        _load_polls = {"n": 0}

        def _make_progress(label):
            dlg = _orig_make_progress(label)
            _last_progress[:] = [dlg]
            return dlg

        def _fake_loading(_track_id, _slot):
            _load_polls["n"] += 1
            return _load_polls["n"] <= 3

        win._make_vst_load_progress_dialog = _make_progress
        win.bridge.vst_is_loading = _fake_loading
        win.bridge.vst_diagnostic = lambda _track_id, _slot: \
            "VST bridge: loaded in subprocess"
        win.bridge.is_playing = lambda: True
        _apply_saw_progress = []

        def _apply_vst():
            _apply_saw_progress.append(bool(_last_progress)
                                       and _last_progress[0].isVisible())
            return True

        assert win._with_vst_load_progress(
            midi_track.id, -1, "Playback VST", _apply_vst,
            open_editor=False)
        assert _apply_saw_progress == [True]
        assert _load_polls["n"] >= 4
    finally:
        win._make_vst_load_progress_dialog = _orig_make_progress
        win.bridge.vst_is_loading = _orig_vst_is_loading
        win.bridge.vst_diagnostic = _orig_vst_diag
        win.bridge.is_playing = _orig_is_playing_for_wait
    _play_calls = []
    _pause_calls = []
    _orig_play = win.bridge.play
    _orig_pause = win.bridge.pause
    _orig_is_playing = win.bridge.is_playing
    try:
        win.bridge.play = lambda: _play_calls.append("play")
        win.bridge.pause = lambda: _pause_calls.append("pause")
        win.bridge.is_playing = lambda: False
        win._set_vst_load_progress_active(True)
        win.toggle_play()
        assert _play_calls == []
        win.bridge.is_playing = lambda: True
        win.toggle_play()
        assert _pause_calls == []
        win._set_vst_load_progress_active(False)
        win.bridge.is_playing = lambda: False
        win.toggle_play()
        assert _play_calls == ["play"]
    finally:
        win._vst_load_progress_depth = 0
        win.bridge.play = _orig_play
        win.bridge.pause = _orig_pause
        win.bridge.is_playing = _orig_is_playing

    # Clip duplication on the timeline.
    win._on_clip_duplicate(midi_track.id, clip.id, True)
    assert len(midi_track.midi_clips) == 2
    win.undo_stack.undo()
    assert len(midi_track.midi_clips) == 1

    # Export dialog: formats with options, track list, encode via engine.
    import tempfile as _tf
    from pydaw.ui.export_dialog import (ExportDialog, MASTER_ID,
                                        export_mix_to_file, safe_filename)
    edlg = ExportDialog(win.project, has_loop=False)
    assert edlg.formats == [("wav", "FLOAT")]  # default: WAV 32f
    assert edlg.track_ids == [MASTER_ID]       # default: Master only
    assert not edlg.range_loop.isEnabled()     # no loop region set
    edlg._fmt_checks["mp3"].setChecked(True)
    edlg._fmt_combos["mp3"].setCurrentIndex(3)  # 128 kbps
    assert ("mp3", "128") in edlg.formats
    # Check a real track in the list -> two targets selected.
    item = edlg.track_list.item(1)
    item.setCheckState(Qt.Checked)
    assert len(edlg.track_ids) == 2
    edlg.close()
    assert safe_filename("Trk 1/?*") == "Trk_1_"
    with _tf.TemporaryDirectory() as td:
        for fmt, opt in (("wav", "FLOAT"), ("wav", "PCM_16"),
                         ("flac", "PCM_24"), ("mp3", "320")):
            p = Path(td) / f"mix_{opt}.{fmt}"
            assert export_mix_to_file(win.bridge, str(p), fmt, opt)
            assert p.exists() and p.stat().st_size > 0
        # Loop range renders fewer frames than the full arrangement.
        full = Path(td) / "mix_FLOAT.wav"
        part = Path(td) / "part.wav"
        assert export_mix_to_file(
            win.bridge, str(part), "wav", "FLOAT",
            0, win.project.beats_to_frames(1.0))
        assert part.stat().st_size < full.stat().st_size
        # Per-track routing helpers: solo-for-export mutes other tracks
        # in the engine but never touches the model.
        win._solo_for_export(midi_track.id)
        win._restore_export_routing()
        assert not midi_track.mute

    # Loop region via the ruler handler + transport toggle.
    win._on_loop_region(0.0, 4.0)
    assert win.project.loop_enabled
    assert win.transport.loop_btn.isChecked()
    win._on_loop_toggled(False)
    assert not win.project.loop_enabled

    # Loop region via ruler mouse: create, edge-resize, collapse to zero width.
    def _ruler_mouse(timeline, beat, event_type, btn=Qt.LeftButton,
                     buttons=None):
        scene_x = beat * timeline.project.frames_per_beat \
            / timeline.samples_per_px
        vp = timeline.mapFromScene(QPointF(scene_x, -RULER_HEIGHT / 2))
        if buttons is None:
            buttons = (btn if event_type != QEvent.MouseButtonRelease
                       else Qt.NoButton)
        g = timeline.mapToGlobal(QPoint(int(vp.x()), int(vp.y())))
        return QMouseEvent(event_type, QPointF(vp), QPointF(g),
                           btn, buttons, Qt.NoModifier)

    def _ruler_drag(timeline, beat_from, beat_to):
        timeline.mousePressEvent(
            _ruler_mouse(timeline, beat_from, QEvent.MouseButtonPress))
        timeline.mouseMoveEvent(
            _ruler_mouse(timeline, beat_to, QEvent.MouseMove))
        timeline.mouseReleaseEvent(
            _ruler_mouse(timeline, beat_to, QEvent.MouseButtonRelease))
        app.processEvents()

    win.project.loop_start_beat = 0.0
    win.project.loop_end_beat = 0.0
    win.project.loop_enabled = False
    win.transport.set_loop(False)
    win.timeline.snap_beats = 1.0
    _ruler_drag(win.timeline, 2.0, 6.0)
    assert win.project.loop_start_beat == 2.0
    assert win.project.loop_end_beat == 6.0
    assert win.timeline._loop_region_beats() == (2.0, 6.0)
    _ruler_drag(win.timeline, 6.0, 8.0)   # widen right edge
    assert win.project.loop_end_beat == 8.0
    _ruler_drag(win.timeline, 2.0, 4.0)   # move left edge
    assert win.project.loop_start_beat == 4.0
    assert win.project.loop_end_beat == 8.0
    _ruler_drag(win.timeline, 4.0, 8.0)   # collapse to zero width
    assert win.project.loop_start_beat == win.project.loop_end_beat
    assert win.timeline._loop_region_beats() is None

    # Loop edge grab zone: exact at every zoom level and scroll offset
    # (float-precise mapping; mapFromScene used to round to whole pixels).
    win.project.loop_start_beat = 4.0
    win.project.loop_end_beat = 8.0
    win.project.loop_enabled = True
    win.transport.set_loop(True)
    tl = win.timeline
    from pydaw.ui.timeline import LOOP_EDGE_HIT_PX
    for spp in (120.0, 480.0, 1200.0, 3000.0):   # zoom in ... zoom out
        tl.samples_per_px = spp
        tl.refresh_layout()
        for scroll in (0, 37):
            tl.horizontalScrollBar().setValue(scroll)
            app.processEvents()
            ppb = tl.project.frames_per_beat / tl.samples_per_px
            tr = tl.viewportTransform()
            v0 = tr.map(QPointF(4.0 * ppb, 0.0)).x()
            v1 = tr.map(QPointF(8.0 * ppb, 0.0)).x()
            for v, want in ((v0, "left"), (v1, "right")):
                assert tl._ruler_loop_edge_hit(v) == want, (spp, scroll)
                assert tl._ruler_loop_edge_hit(
                    v - LOOP_EDGE_HIT_PX + 0.5) == want, (spp, scroll)
                assert tl._ruler_loop_edge_hit(
                    v + LOOP_EDGE_HIT_PX - 0.5) == want, (spp, scroll)
            mid = (v0 + v1) / 2
            if mid - v0 > LOOP_EDGE_HIT_PX:
                assert tl._ruler_loop_edge_hit(mid) is None, (spp, scroll)
            assert tl._ruler_loop_edge_hit(
                v0 - LOOP_EDGE_HIT_PX - 2) is None, (spp, scroll)
            assert tl._ruler_loop_edge_hit(
                v1 + LOOP_EDGE_HIT_PX + 2) is None, (spp, scroll)
    # Narrow region: both edges inside the tolerance — the nearer one wins.
    tl.samples_per_px = 3000.0
    tl.refresh_layout()
    tl.horizontalScrollBar().setValue(0)
    app.processEvents()
    win.project.loop_start_beat = 4.0
    win.project.loop_end_beat = 4.125
    ppb = tl.project.frames_per_beat / tl.samples_per_px
    tr = tl.viewportTransform()
    v0 = tr.map(QPointF(4.0 * ppb, 0.0)).x()
    v1 = tr.map(QPointF(4.125 * ppb, 0.0)).x()
    if v1 - v0 <= LOOP_EDGE_HIT_PX:
        assert tl._ruler_loop_edge_hit(v0 - 1) == "left"
        assert tl._ruler_loop_edge_hit(v1 + 1) == "right"

    # Click (no drag) on a loop edge neither seeks nor changes the region.
    tl.samples_per_px = 480.0
    tl.refresh_layout()
    tl.horizontalScrollBar().setValue(0)
    app.processEvents()
    win.project.loop_start_beat = 4.0
    win.project.loop_end_beat = 8.0
    seeks: list[int] = []
    conn = tl.seek_requested.connect(seeks.append)
    tl.mousePressEvent(_ruler_mouse(tl, 4.0, QEvent.MouseButtonPress))
    assert tl._loop_drag_mode == "left"
    tl.mouseReleaseEvent(_ruler_mouse(tl, 4.0, QEvent.MouseButtonRelease))
    app.processEvents()
    assert seeks == []
    assert (win.project.loop_start_beat, win.project.loop_end_beat) == (4.0, 8.0)
    # A plain ruler click away from the edges still seeks.
    tl.mousePressEvent(_ruler_mouse(tl, 6.0, QEvent.MouseButtonPress))
    assert tl._loop_drag_mode == "create"
    tl.mouseReleaseEvent(_ruler_mouse(tl, 6.0, QEvent.MouseButtonRelease))
    app.processEvents()
    assert len(seeks) == 1
    tl.seek_requested.disconnect(conn)
    # Hover over an edge shows the resize cursor even after the base class
    # cursor bookkeeping runs (update happens after super().mouseMoveEvent).
    tl.mouseMoveEvent(_ruler_mouse(tl, 8.0, QEvent.MouseMove,
                                   btn=Qt.NoButton, buttons=Qt.NoButton))
    assert tl.viewport().cursor().shape() == Qt.SizeHorCursor
    tl.mouseMoveEvent(_ruler_mouse(tl, 6.0, QEvent.MouseMove,
                                   btn=Qt.NoButton, buttons=Qt.NoButton))
    assert tl.viewport().cursor().shape() != Qt.SizeHorCursor
    win.project.loop_start_beat = 0.0
    win.project.loop_end_beat = 0.0
    win.project.loop_enabled = False
    win.transport.set_loop(False)

    # Clip trim/resize zones: full 8px on normal clips, scaled down on
    # narrow ones so the middle always stays a draggable "move" zone.
    # Pure logical-pixel geometry — identical at any QT_SCALE_FACTOR.
    from pydaw.ui.clip_item import (AudioClipItem, EDGE_PX, MidiClipItem,
                                    edge_zone_px)
    assert edge_zone_px(200.0) == EDGE_PX
    assert edge_zone_px(4 * EDGE_PX + 1) == EDGE_PX
    assert edge_zone_px(16.0) == 4.0          # narrow: width / 4
    assert edge_zone_px(2.0) == 1.0           # floor
    from pydaw.model.project import MidiClip as _MidiClip
    zone_clip = _MidiClip(id=next_id(), name="Z", start_beat=0.0,
                          length_beats=4.0)
    zone_item = MidiClipItem(zone_clip, midi_track.id, win.timeline)
    audio_zone_clip = Clip(id=next_id(), file_path="z.wav", file_id=0,
                           start_frame=0, length=96000)
    audio_zone_item = AudioClipItem(audio_zone_clip, 1, win.timeline)
    for spp in (120.0, 480.0, 1200.0, 6000.0):
        win.timeline.samples_per_px = spp
        w = zone_item.boundingRect().width()
        edge = edge_zone_px(w)
        # MIDI clips resize from both edges; fades were removed, so the
        # top strip at the edges is just the resize zone.
        assert zone_item._zone(QPointF(w - edge + 0.5, 10)) == "resize", spp
        assert zone_item._zone(QPointF(edge - 0.5, 10)) == "resize_left", spp
        assert zone_item._zone(QPointF(w / 2, 10)) == "move", spp
        assert zone_item._zone(QPointF(0.0, 4.0)) == "resize_left", spp
        assert zone_item._zone(QPointF(w, 4.0)) == "resize", spp
        aw = audio_zone_item.boundingRect().width()
        aedge = edge_zone_px(aw)
        ay = 20.0  # below the fade-handle strip
        assert audio_zone_item._zone(QPointF(aedge - 0.5, ay)) == "trim_left", spp
        assert audio_zone_item._zone(QPointF(aw - aedge + 0.5, ay)) == "trim_right", spp
        assert audio_zone_item._zone(QPointF(aw / 2, ay)) == "move", spp
    win.timeline.samples_per_px = 480.0
    win.timeline.refresh_layout()

    # MIDI clip left-edge trim: undoable, shifts the note window with the
    # edge (right edge anchored).
    trim_track = win.project.track_by_id(midi_track.id)
    win.add_midi_clip(trim_track.id, 4.0)
    trim_clip = trim_track.midi_clips[-1]
    win._on_note_add(trim_track.id, trim_clip.id, 60, 0.0, 1.0)
    assert trim_clip.length_beats == 4.0
    win._on_midi_clip_trimmed(
        trim_track.id, trim_clip.id,
        (4.0, 0.0, 4.0), (6.0, 2.0, 2.0))     # drag left edge right by 2
    assert (trim_clip.start_beat, trim_clip.source_start_beat,
            trim_clip.length_beats) == (6.0, 2.0, 2.0)
    win.undo_stack.undo()
    assert (trim_clip.start_beat, trim_clip.source_start_beat,
            trim_clip.length_beats) == (4.0, 0.0, 4.0)
    win.undo_stack.redo()
    assert trim_clip.source_start_beat == 2.0
    # Extending left past the note origin is allowed (empty space).
    win._on_midi_clip_trimmed(
        trim_track.id, trim_clip.id,
        (6.0, 2.0, 2.0), (3.0, -1.0, 5.0))
    assert trim_clip.source_start_beat == -1.0

    # MIDI fade editing was removed from the UI: no signal, no handler,
    # no fade zones. Legacy projects with saved fades still apply their
    # velocity ramps in the bridge.
    assert not hasattr(win.timeline, "midi_clip_fades_changed")
    assert not hasattr(win.timeline, "request_midi_fades")
    assert not hasattr(win, "_on_midi_fades")
    from pydaw.model.project import MidiNote as _MidiNote
    from pydaw.engine_bridge import EngineBridge as _EB
    ramp_clip = _MidiClip(id=next_id(), length_beats=4.0,
                          fade_in_beats=2.0, fade_out_beats=1.0)
    n0 = _MidiNote(id=1, pitch=60, start_beat=0.0, length_beats=1.0)
    n1 = _MidiNote(id=2, pitch=60, start_beat=1.0, length_beats=1.0)
    n2 = _MidiNote(id=3, pitch=60, start_beat=3.5, length_beats=0.5)
    n3 = _MidiNote(id=4, pitch=60, start_beat=2.5, length_beats=0.5)
    assert _EB._midi_fade_scale(ramp_clip, n0) == 0.0
    assert abs(_EB._midi_fade_scale(ramp_clip, n1) - 0.5) < 1e-9
    assert abs(_EB._midi_fade_scale(ramp_clip, n2) - 0.5) < 1e-9
    assert _EB._midi_fade_scale(ramp_clip, n3) == 1.0
    assert _EB._midi_fade_scale(None, n3) == 1.0
    for _ in range(4):   # unwind trim edits + note + clip
        win.undo_stack.undo()
    app.processEvents()
    win.undo_stack.clear()

    # The transport clock stylesheet is scoped to the class: a selector-less
    # rule would restyle the tooltip too (QTipLabel inherits the widget's
    # stylesheet) — the tooltip ballooned to the 20px monospace clock font.
    assert "TimeDisplayLabel {" in win.transport.time_label.styleSheet()

    # Moving the mouse over the lanes must not wipe an item cursor set by
    # the scene (the hand over a clip used to flicker: every view move
    # called viewport().unsetCursor()).
    win.timeline.viewport().setCursor(Qt.OpenHandCursor)
    lane_vp = win.timeline.mapFromScene(QPointF(100.0, 30.0))
    lane_g = win.timeline.mapToGlobal(lane_vp)
    win.timeline.mouseMoveEvent(QMouseEvent(
        QEvent.MouseMove, QPointF(lane_vp), QPointF(lane_g),
        Qt.NoButton, Qt.NoButton, Qt.NoModifier))
    assert win.timeline.viewport().cursor().shape() == Qt.OpenHandCursor
    win.timeline.viewport().unsetCursor()

    # Tooltips must not inherit widget fonts (the 20px monospace transport
    # clock made its tooltip huge): the theme pins the QToolTip font, which
    # is in logical px and therefore consistent at any interface scale.
    from pydaw.ui.theme import _qss
    qss = _qss()
    tip_block = qss.split("QToolTip {")[1].split("}")[0]
    assert "font-size: 11px" in tip_block
    assert "font-family" in tip_block
    for widget in (win.transport.time_label, win.transport.play_btn,
                   win.transport.loop_btn, win.transport.metro_btn):
        style = widget.styleSheet()
        assert "QToolTip" not in style   # nothing overrides the global font

    # Timeline zoom API (wheel/keyboard use the same entry points).
    spp = win.timeline.samples_per_px
    win.timeline.zoom(1.25)
    assert win.timeline.samples_per_px < spp
    win.timeline.zoom_to_fit()

    # Zoom in/out menu actions carry layout-independent key aliases
    # (Ctrl++, Ctrl+= and the Shift variants all zoom).
    _zin = win._menu_actions["view.zoom_in"].shortcuts()
    _zout = win._menu_actions["view.zoom_out"].shortcuts()
    assert QKeySequence("Ctrl++") in _zin and QKeySequence("Ctrl+=") in _zin
    assert QKeySequence("Ctrl+-") in _zout
    _spp0 = win.timeline.samples_per_px
    win._menu_actions["view.zoom_in"].trigger()
    assert win.timeline.samples_per_px < _spp0
    win._menu_actions["view.zoom_out"].trigger()

    # F: zoom to fit the selection when clips are selected, else the
    # whole project. Bare zoom keys bubble through the main window, so
    # they work even when the timeline is not the focus widget.
    win.add_midi_clip(midi_track.id, 32.0)
    fit_clip = midi_track.midi_clips[-1]
    app.processEvents()
    win.timeline._scene.clearSelection()
    from pydaw.ui.clip_item import MidiClipItem as _FitMCI
    for it in win.timeline._scene.items():
        if isinstance(it, _FitMCI) and it.clip.id == fit_clip.id:
            it.setSelected(True)
    span = win.timeline._selection_frame_span()
    assert span is not None
    assert span[0] == win.project.beats_to_frames(fit_clip.start_beat)
    f_ev = QKeyEvent(QEvent.KeyPress, Qt.Key_F, Qt.NoModifier)
    win.keyPressEvent(f_ev)   # bubbled path (focus not on the timeline)
    assert f_ev.isAccepted()
    # The selected clip fills most of the view and is scrolled into it.
    view_w = win.timeline.viewport().width()
    span_px = (span[1] - span[0]) / win.timeline.samples_per_px
    assert span_px <= view_w
    assert span_px >= min(view_w - 60, span[1] - span[0]) * 0.5
    x0 = win.timeline.x_at_frame(span[0])
    assert win.timeline.horizontalScrollBar().value() <= x0 + 1
    # No selection -> F falls back to fitting the whole project.
    win.timeline._scene.clearSelection()
    assert win.timeline._selection_frame_span() is None
    win.timeline.zoom_to_fit_selection()
    assert win.timeline.horizontalScrollBar().value() == 0
    win._on_clip_remove(midi_track.id, fit_clip.id, True)
    win.undo_stack.clear()
    app.processEvents()

    # -- Select menu (between Edit and Track) + selection actions -----------
    _menu_titles = [a.text() for a in win.menuBar().actions()]
    assert _menu_titles.index("&Select") == _menu_titles.index("&Edit") + 1
    assert _menu_titles.index("&Track") == _menu_titles.index("&Select") + 1
    for _aid in ("select.all", "select.none", "select.invert",
                 "select.track"):
        assert _aid in win._menu_actions
    win.add_midi_clip(midi_track.id, 0.0)
    win.add_midi_clip(midi_track.id, 8.0)
    app.processEvents()
    _sel_clips = [midi_track.midi_clips[-2], midi_track.midi_clips[-1]]
    win._menu_actions["select.all"].trigger()
    assert len(win.timeline._selected_clip_refs()) >= 2
    win._menu_actions["select.none"].trigger()
    assert not win.timeline._selected_clip_refs()
    # Invert: empty selection becomes everything and back.
    win._menu_actions["select.invert"].trigger()
    assert len(win.timeline._selected_clip_refs()) >= 2
    win._menu_actions["select.invert"].trigger()
    assert not win.timeline._selected_clip_refs()
    # Select all on track: one selected clip expands to its whole lane.
    win.timeline.select_clip(_sel_clips[0].id)
    win._menu_actions["select.track"].trigger()
    _refs = win.timeline._selected_clip_refs()
    assert {c.id for c in _sel_clips} <= {cid for _, cid, _ in _refs}
    assert all(tid == midi_track.id for tid, _, _ in _refs)
    # Main-window tools are independent from piano roll tools. The selection
    # tool selects timeline clips by dragging a rectangle.
    assert win.transport.mouse_btn.isChecked()
    win._menu_actions["edit.selection_tool"].trigger()
    assert win.timeline.tool == "select"
    assert win.transport.select_btn.isChecked()
    win.timeline.deselect_all_clips()
    _items = [win.timeline._clip_items[c.id] for c in _sel_clips]
    _rub_scene = _items[0].sceneBoundingRect().united(
        _items[1].sceneBoundingRect()).adjusted(-4, -4, 4, 4)
    _rub_a = win.timeline.mapFromScene(_rub_scene.topLeft())
    _rub_b = win.timeline.mapFromScene(_rub_scene.bottomRight())
    win.timeline.mousePressEvent(QMouseEvent(
        QEvent.MouseButtonPress, QPointF(_rub_a),
        QPointF(win.timeline.mapToGlobal(_rub_a)),
        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    win.timeline.mouseMoveEvent(QMouseEvent(
        QEvent.MouseMove, QPointF(_rub_b),
        QPointF(win.timeline.mapToGlobal(_rub_b)),
        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
    win.timeline.mouseReleaseEvent(QMouseEvent(
        QEvent.MouseButtonRelease, QPointF(_rub_b),
        QPointF(win.timeline.mapToGlobal(_rub_b)),
        Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
    _refs = win.timeline._selected_clip_refs()
    assert {c.id for c in _sel_clips} <= {cid for _, cid, _ in _refs}
    win._menu_actions["edit.mouse_tool"].trigger()
    assert win.timeline.tool == "mouse"
    assert win.transport.mouse_btn.isChecked()
    # Ctrl+A with a focused text field selects the text, not clips.
    from PySide6.QtWidgets import QLineEdit as _QLE
    _le = _QLE("hello", win)
    _le.show()
    _le.setFocus()
    app.processEvents()
    if QApplication.focusWidget() is _le:
        win.timeline.deselect_all_clips()
        win._menu_actions["select.all"].trigger()
        assert _le.selectedText() == "hello"
        assert not win.timeline._selected_clip_refs()
    _le.deleteLater()
    for _c in list(_sel_clips):
        win._on_clip_remove(midi_track.id, _c.id, True)
    win.timeline.deselect_all_clips()   # don't leak selection downstream
    win.undo_stack.clear()
    app.processEvents()

    # -- Localization: language combo + tr() dictionaries -------------------
    from pydaw.ui import i18n
    _sdlg = SettingsDialog(win.bridge, win._menu_actions,
                           win._default_shortcuts, win.settings, win)
    _lang_values = [_sdlg.language_combo.itemData(i)
                    for i in range(_sdlg.language_combo.count())]
    assert _lang_values == ["en", "ru"]
    assert _sdlg.language in ("en", "ru")
    _sdlg.deleteLater()
    _prev_lang = i18n.language()
    i18n.set_language("ru")
    assert i18n.tr("&File") == "&Файл"
    assert i18n.tr("Appearance") == "Внешний вид"
    assert i18n.tr("Curve to next point") == "Кривая к следующей точке"
    assert [i18n.tr(label) for label in
            ("Linear", "Step", "Exponential", "Bezier")] == [
                "Линейная", "Ступенчатая", "Экспоненциальная", "Безье"]
    assert i18n.tr("__unknown_key__") == "__unknown_key__"
    win.transport.retranslate()
    assert "Играть" in win.transport.play_btn.toolTip()
    win._retranslate_ui()
    assert win.menuBar().actions()[0].text() == "&Файл"
    i18n.set_language(_prev_lang)
    assert i18n.tr("&File") == "&File"
    i18n.set_language("en")
    assert [i18n.tr(label) for label in
            ("Linear", "Step", "Exponential", "Bezier")] == [
                "Linear", "Step", "Exponential", "Bezier"]
    i18n.set_language(_prev_lang)
    assert i18n.normalize("bogus") in ("en", "ru")

    # Mixer window: open, tweak, meters, close.
    win.open_mixer()
    app.processEvents()
    assert win._mixer is not None

    # Master section: leftmost strip of the mixer, with its own FX button
    # routed to the master insert chain.
    _mx_layout = win._mixer.layout()
    _master_frame = _mx_layout.itemAt(0).widget()
    assert _master_frame is win._mixer.master_fx_btn.parentWidget()
    assert win._mixer.master_slider.parentWidget() is _master_frame
    from pydaw.engine_bridge import MASTER_FX_ID as _MFX
    win._mixer.fx_requested.emit(_MFX)
    app.processEvents()
    assert win._selected_track_id == _MFX
    # Reset the selection so the FxPanel section below starts clean.
    win.master_header.set_selected(False)
    win._selected_track_id = None
    strip = win._mixer.strips[midi_track.id]
    strip.gain_slider.setValue(80)
    assert abs(midi_track.gain - 0.8) < 1e-9
    win._on_gain(midi_track.id, 1.0)   # header/mixer stay in sync
    assert strip.gain_slider.value() == 100
    win._finish_param_gesture()  # 0.8 -> 1.0 within one gesture: no-op

    # Gain gesture undo: several moves coalesce into one undo step.
    win._on_gain(midi_track.id, 0.6)
    win._on_gain(midi_track.id, 0.9)
    win._finish_param_gesture()
    win.undo_stack.undo()
    assert abs(midi_track.gain - 1.0) < 1e-9
    assert strip.gain_slider.value() == 100
    win.undo_stack.redo()
    assert abs(midi_track.gain - 0.9) < 1e-9
    assert strip.gain_slider.value() == 90
    win.undo_stack.undo()
    assert abs(midi_track.gain - 1.0) < 1e-9

    # Undo with a PENDING gesture (no explicit finish): the stack flushes
    # the gesture first, so Ctrl+Z right after a knob move undoes that
    # move instead of the previous command.
    win._on_gain(midi_track.id, 0.7)
    assert win._param_gesture is not None
    win.undo_stack.undo()
    assert win._param_gesture is None
    assert abs(midi_track.gain - 1.0) < 1e-9
    assert strip.gain_slider.value() == 100
    win.undo_stack.redo()
    assert abs(midi_track.gain - 0.7) < 1e-9
    win.undo_stack.undo()
    assert abs(midi_track.gain - 1.0) < 1e-9

    # A pending gesture is also flushed before any other command is
    # pushed, keeping history in chronological order.
    win._on_gain(midi_track.id, 0.5)
    _n_tracks = len(win.project.tracks)
    win.add_track()  # flush -> [gain gesture, Add track]
    assert len(win.project.tracks) == _n_tracks + 1
    win.undo_stack.undo()  # removes the added track first
    assert len(win.project.tracks) == _n_tracks
    assert abs(midi_track.gain - 0.5) < 1e-9
    win.undo_stack.undo()  # then the gain move
    assert abs(midi_track.gain - 1.0) < 1e-9
    win.undo_stack.clear()

    # The Undo action is enabled as soon as a gesture is pending (before
    # the 500 ms finalize timer fires): otherwise Ctrl+Z right after a
    # tweak hits a disabled action and "works only the second time".
    assert not win.undo_action.isEnabled()  # stack is empty
    win._on_gain(midi_track.id, 0.7)
    assert win.undo_action.isEnabled()      # pending gesture counts
    win.undo_stack.undo()
    assert abs(midi_track.gain - 1.0) < 1e-9
    assert not win.undo_action.isEnabled() or win.undo_stack.can_undo
    win.undo_stack.clear()

    # Undo/redo shortcuts work while the mixer window has focus
    # (routed through MixerWindow.undo_requested/redo_requested).
    win._on_gain(midi_track.id, 0.8)
    win._finish_param_gesture()
    win._mixer.undo_requested.emit()
    assert abs(midi_track.gain - 1.0) < 1e-9
    win._mixer.redo_requested.emit()
    assert abs(midi_track.gain - 0.8) < 1e-9
    win._mixer.undo_requested.emit()
    assert abs(midi_track.gain - 1.0) < 1e-9
    win.undo_stack.clear()

    # VST param-list refresh poll: no plugins -> nothing to refresh,
    # and the rebuild path is a no-op without VST cards/editors.
    assert win.bridge.vst_refresh_changed_params() is False
    win._on_vst_params_changed()

    # Mixer sliders expose "reset to default" (right-click):
    # gain 0 dB / pan center / master 0 dB defaults.
    # (add_track/undo rebuilt the mixer -> re-fetch the strip)
    strip = win._mixer.strips[midi_track.id]
    from pydaw.ui.knob import ResetSlider as _ResetSlider
    assert isinstance(strip.gain_slider, _ResetSlider)
    assert strip.gain_slider._default_value == 100
    assert isinstance(strip.pan_slider, _ResetSlider)
    assert strip.pan_slider._default_value == 0
    assert isinstance(win._mixer.master_slider, _ResetSlider)
    assert win._mixer.master_slider._default_value == 100
    # Reset goes through the same undoable path as a normal move.
    strip.pan_slider.setValue(40)
    win._finish_param_gesture()
    assert abs(midi_track.pan - 0.4) < 1e-9
    strip.pan_slider.setValue(strip.pan_slider._default_value)
    win._finish_param_gesture()
    assert abs(midi_track.pan) < 1e-9
    win.undo_stack.undo()   # undo the reset
    assert abs(midi_track.pan - 0.4) < 1e-9
    win.undo_stack.undo()   # undo the move
    assert abs(midi_track.pan) < 1e-9
    win.undo_stack.clear()

    win._mixer.update_meters(win.bridge)
    win._mixer.close()
    app.processEvents()
    assert win._mixer is None

    # -- FxPanel: track selection, effects, instrument card ----------------
    assert win._selected_track_id is None
    win._select_track_for_fx(midi_track.id)
    app.processEvents()
    assert win._selected_track_id == midi_track.id
    assert win.fx_panel.track is midi_track
    assert win.fx_panel._type_badge.text() == "MIDI"
    assert win.track_panel.headers[midi_track.id]._selected

    # MIDI track shows instrument card as first device.
    first = win.fx_panel._strip.itemAt(0).widget()
    assert first is not None
    assert first.__class__.__name__ == "_InstrumentCard"

    # Add built-in delay via panel signal.
    win.fx_panel.effect_added.emit(midi_track.id, "delay")
    app.processEvents()
    assert len(midi_track.effects) == 1
    assert midi_track.effects[0].type == "delay"

    # Change a parameter.
    win.fx_panel.param_changed.emit(midi_track.id, 0, 0, 0.5)
    assert abs(midi_track.effects[0].params[0] - 0.5) < 1e-9

    # Built-in FX param gesture undo: 0.5 then 0.7 = one undo step back
    # to the effect's default value.
    from pydaw.model.project import EFFECT_DEFS as _FXDEFS
    win.fx_panel.param_changed.emit(midi_track.id, 0, 0, 0.7)
    win._finish_param_gesture()
    delay_default = _FXDEFS["delay"][1][0][3]
    win.undo_stack.undo()
    assert abs(midi_track.effects[0].params[0] - delay_default) < 1e-9
    win.undo_stack.redo()
    assert abs(midi_track.effects[0].params[0] - 0.7) < 1e-9

    # Builtin FX card sliders are ResetSliders whose default matches
    # EFFECT_DEFS; reset lands on the default value and is undoable.
    from pydaw.ui.knob import ResetSlider as _RSlider
    _card = win.fx_panel._builtin_cards[0]
    for _i, (_lab, _lo, _hi, _dflt) in enumerate(_FXDEFS["delay"][1]):
        _s = _card._sliders[_i]
        assert isinstance(_s, _RSlider)
        assert _s._default_value == int(round((_dflt - _lo)
                                              / (_hi - _lo) * 1000))
    _s0 = _card._sliders[0]
    _lo0, _hi0 = _FXDEFS["delay"][1][0][1:3]
    _step = (_hi0 - _lo0) / 1000  # slider quantization step
    _s0.setValue(_s0._default_value)   # same as the right-click reset
    win._finish_param_gesture()
    assert abs(midi_track.effects[0].params[0] - delay_default) <= _step
    win.undo_stack.undo()              # back to 0.7
    assert abs(midi_track.effects[0].params[0] - 0.7) < 1e-9
    win.undo_stack.redo()
    assert abs(midi_track.effects[0].params[0] - delay_default) <= _step
    win.undo_stack.clear()

    # Effect cards are parented into the strip at construction time
    # (a parentless card can flash as a tiny top-level window).
    for _w in ([_card] + win.fx_panel._strip_host.effect_cards):
        assert _w.parent() is win.fx_panel._strip_host

    # Remove effect.
    win.fx_panel.effect_removed.emit(midi_track.id, 0)
    app.processEvents()
    assert len(midi_track.effects) == 0

    # Audio track selection + add filter.
    audio_track = next(t for t in win.project.tracks if t.type == "audio")
    win._select_track_for_fx(audio_track.id)
    app.processEvents()
    assert win.fx_panel._type_badge.text() == "AUDIO"
    win.fx_panel.effect_added.emit(audio_track.id, "filter")
    app.processEvents()
    assert len(audio_track.effects) == 1
    win.fx_panel.effect_removed.emit(audio_track.id, 0)
    assert len(audio_track.effects) == 0

    # Drag-and-drop reorder: delay + filter, move slot 0 -> 1; the
    # automation lane bound to the delay follows its effect.
    win.fx_panel.effect_added.emit(audio_track.id, "delay")
    win.fx_panel.effect_added.emit(audio_track.id, "filter")
    app.processEvents()
    assert [f.type for f in audio_track.effects] == ["delay", "filter"]
    win._on_add_automation_lane(audio_track.id, "fx", 0, 0, "Delay: Time")
    app.processEvents()
    win._on_effect_moved(audio_track.id, 0, 1)
    app.processEvents()
    assert [f.type for f in audio_track.effects] == ["filter", "delay"]
    _fx_lane = next(l for l in audio_track.automation_lanes
                    if l.target == "fx")
    assert _fx_lane.fx_slot == 1
    win.undo_stack.undo()  # reorder is undoable
    app.processEvents()
    assert [f.type for f in audio_track.effects] == ["delay", "filter"]
    assert _fx_lane.fx_slot == 0
    win._on_remove_automation_lane(audio_track.id, _fx_lane.id)
    win.fx_panel.effect_removed.emit(audio_track.id, 1)
    win.fx_panel.effect_removed.emit(audio_track.id, 0)
    app.processEvents()
    assert len(audio_track.effects) == 0
    win.undo_stack.clear()

    # Drop targeting: the dragged card takes the place of the card under
    # the cursor. Purely geometric (logical px) -> identical behavior at
    # any interface scale; verified across the settings' scale factors.
    from PySide6.QtCore import QRect
    from pydaw.ui.fx_panel import CARD_WIDTH, fx_drop_target_slot
    for scale in (0.75, 1.0, 1.25, 1.5, 1.75, 2.0):
        w, gap = int(CARD_WIDTH * scale), max(1, int(8 * scale))
        rects = [QRect(i * (w + gap), 0, w, 60) for i in range(4)]
        for i, r in enumerate(rects):
            assert fx_drop_target_slot(r.center().x(), rects) == i, scale
            assert fx_drop_target_slot(r.left() + 1, rects) == i, scale
            assert fx_drop_target_slot(r.right() - 1, rects) == i, scale
        # Gaps belong to the card on the right; outside clamps to ends.
        assert fx_drop_target_slot(rects[0].right() + gap // 2, rects) == 1
        assert fx_drop_target_slot(-50, rects) == 0
        assert fx_drop_target_slot(rects[-1].right() + 500, rects) == 3
    assert fx_drop_target_slot(10, []) is None

    # Real QDropEvent onto the strip: drop card 0 over card 1's left half
    # (used to be a dead zone with the old midpoint rule).
    from PySide6.QtCore import QMimeData
    from PySide6.QtGui import QDropEvent
    from pydaw.ui.fx_panel import FX_SLOT_MIME
    win._select_track_for_fx(audio_track.id)
    top_before = set(app.topLevelWidgets())
    win.fx_panel.effect_added.emit(audio_track.id, "delay")
    win.fx_panel.effect_added.emit(audio_track.id, "filter")
    app.processEvents()
    # Rebuilding the panel must not spawn stray top-level windows (every
    # card child now gets a parent at construction time).
    assert set(app.topLevelWidgets()) - top_before == set()
    strip = win.fx_panel._strip_host
    assert len(strip.effect_cards) == 2
    drops: list[tuple[int, int, int]] = []
    win.fx_panel.effect_moved.connect(
        lambda tid, f, t: drops.append((tid, f, t)))
    mime = QMimeData()
    mime.setData(FX_SLOT_MIME, b"0")
    target = strip.effect_cards[1].geometry()
    pos = QPointF(target.left() + 5, target.center().y())
    drop = QDropEvent(pos, Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier)
    strip.dropEvent(drop)
    assert drops and drops[-1] == (audio_track.id, 0, 1)
    win.fx_panel.effect_removed.emit(audio_track.id, 1)
    win.fx_panel.effect_removed.emit(audio_track.id, 0)
    app.processEvents()
    win.undo_stack.clear()

    # Effect cards are registered as drop targets on the strip.
    win._select_track_for_fx(midi_track.id)
    win.fx_panel.effect_added.emit(midi_track.id, "delay")
    app.processEvents()
    assert len(win.fx_panel._strip_host.effect_cards) == 1

    # Drag preview is slightly brighter than the card itself, in every theme
    # (the overlay color comes from the theme palette).
    from pydaw.ui.fx_panel import drag_card_pixmap
    drag_card = win.fx_panel._strip_host.effect_cards[0]
    for theme_name in ("dark", "light"):
        set_theme(theme_name)
        plain = drag_card.grab().toImage()
        bright = drag_card_pixmap(drag_card).toImage()
        c = QPoint(plain.width() // 2, plain.height() // 2)
        p0, p1 = plain.pixelColor(c), bright.pixelColor(c)
        assert p1.lightness() > p0.lightness(), theme_name
        assert bright.size() == plain.size()
    set_theme("dark")

    win.fx_panel.effect_removed.emit(midi_track.id, 0)
    app.processEvents()

    # Instrument card: remove button only for a loaded VST instrument.
    inst_card = win.fx_panel._strip.itemAt(0).widget()
    assert inst_card.__class__.__name__ == "_InstrumentCard"
    assert not inst_card._remove_btn.isVisibleTo(inst_card)
    win.fx_panel.instrument_remove_requested.emit(midi_track.id)  # no-op
    app.processEvents()
    assert not midi_track.instrument.uses_vst

    # View menu: hide/show FX panel (Ctrl+B action).
    assert win.fx_panel.isVisible()
    win._set_fx_panel_visible(False)
    app.processEvents()
    assert not win.fx_panel.isVisible()
    win._set_fx_panel_visible(True)
    app.processEvents()
    assert win.fx_panel.isVisible()

    # BPM change with loop enabled must re-sync without errors.
    win._on_loop_region(0.0, 4.0)
    win._on_bpm_changed(90.0)

    # Transport time display: toggle time <-> bars.
    win.transport.set_bars_mode(False)  # mode persists in QSettings
    win.transport.set_time(90.0)
    assert win.transport.time_label.text().startswith("01:30.")
    win.transport.toggle_time_display_mode()
    bars_text = win.transport.time_label.text()
    assert bars_text.count(":") == 2 and "." not in bars_text
    win.transport.toggle_time_display_mode()
    assert win.transport.time_label.text().startswith("01:30.")

    # Split at playhead: MIDI clip with note distribution.
    clip.notes.clear()
    clip.notes.extend([
        MidiNote(id=next_id(), pitch=60, start_beat=0.0, length_beats=1.0),
        MidiNote(id=next_id(), pitch=64, start_beat=3.0, length_beats=1.0),
        MidiNote(id=next_id(), pitch=67, start_beat=1.0, length_beats=2.0),
    ])
    win.timeline.rebuild()
    split_frame = win.project.beats_to_frames(2.0)
    win._on_seek(split_frame)
    win.split_at_playhead()
    assert len(midi_track.midi_clips) == 2
    left, right = sorted(midi_track.midi_clips, key=lambda c: c.start_beat)
    assert len(left.notes) == 2
    assert len(right.notes) == 2
    assert abs(right.source_start_beat - 2.0) < 1e-9
    assert sorted(n.pitch for n in right.notes) == [64, 67]
    continued = next(n for n in right.notes if n.pitch == 67)
    assert abs(continued.start_beat - 2.0) < 1e-9
    assert abs(continued.length_beats - 1.0) < 1e-9
    win.undo_stack.undo()
    assert len(midi_track.midi_clips) == 1
    assert len(clip.notes) == 3

    # Split at playhead: audio clip.
    audio_track = next(t for t in win.project.tracks if t.type == "audio")
    audio_clip = Clip(id=next_id(), file_path="dummy.wav", file_id=0,
                      start_frame=0, length=48000, name="Loop")
    audio_track.clips.append(audio_clip)
    win.bridge.add_clip(audio_track.id, audio_clip)
    win.timeline.rebuild()
    audio_clip.warp = "stretch_hd"
    audio_clip.stretch_ratio = 1.5
    audio_clip.auto_warp = False
    win._on_seek(24000)
    win.split_at_playhead()
    assert len(audio_track.clips) == 2
    _split_right = max(audio_track.clips, key=lambda c: c.start_frame)
    assert _split_right.name == "Loop_2"
    assert _split_right.source_offset == 24000
    assert _split_right.warp == "stretch_hd"
    assert abs(_split_right.stretch_ratio - 1.5) < 1e-9
    assert _split_right.auto_warp is False
    win._on_seek(36000)
    win.split_at_playhead()
    assert len(audio_track.clips) == 3
    _third = max(audio_track.clips, key=lambda c: c.start_frame)
    assert _third.name == "Loop_3"
    assert _third.source_offset == 36000
    win.undo_stack.undo()
    assert len(audio_track.clips) == 2
    win.undo_stack.undo()
    assert len(audio_track.clips) == 1
    audio_clip.warp = "stretch"
    audio_clip.stretch_ratio = 1.0
    audio_clip.auto_warp = True
    win.bridge.set_clip_warp(audio_track.id, audio_clip)

    # BPM change re-warps audio clips: musical position AND musical
    # length survive the tempo change (realtime C++ warp, no debounce).
    bpm_before = win.project.bpm
    audio_clip.start_frame = win.project.beats_to_frames(2.0)
    win.bridge.move_clip(audio_track.id, audio_clip.id,
                         audio_clip.start_frame)
    len0 = audio_clip.length
    win.timeline.snap_enabled = True
    win._apply_bpm(bpm_before * 2)
    assert audio_clip.start_frame == win.project.beats_to_frames(2.0)
    assert abs(audio_clip.length - len0 / 2) <= 1
    assert abs(audio_clip.stretch_ratio - 0.5) < 1e-6
    win._apply_bpm(bpm_before)
    assert audio_clip.start_frame == win.project.beats_to_frames(2.0)
    assert abs(audio_clip.length - len0) <= 1
    assert abs(audio_clip.stretch_ratio - 1.0) < 1e-6
    audio_clip.start_frame = 0
    audio_clip.length = len0
    audio_clip.stretch_ratio = 1.0
    win.bridge.move_clip(audio_track.id, audio_clip.id, 0)
    win.bridge.set_clip_warp(audio_track.id, audio_clip)

    # Auto-warp gating: with Snap off a BPM change leaves audio clips
    # untouched; a clip with auto_warp=False is skipped even with Snap on.
    win.timeline.snap_enabled = False
    win._apply_bpm(bpm_before * 2)
    assert audio_clip.length == len0
    assert abs(audio_clip.stretch_ratio - 1.0) < 1e-6
    win._apply_bpm(bpm_before)
    win.timeline.snap_enabled = True
    audio_clip.auto_warp = False
    win._apply_bpm(bpm_before * 2)
    assert audio_clip.length == len0
    win._apply_bpm(bpm_before)
    audio_clip.auto_warp = True

    # One-time warp fit on insertion: a 1.83-beat file is stretched to
    # exactly 2 beats (nearest grid step) via the real render path.
    import tempfile as _wtf
    import numpy as _np
    import soundfile as _wsf
    _wav_path = Path(_wtf.gettempdir()) / "pydaw_warp_fit.wav"
    _fpb = win.project.frames_per_beat
    _frames0 = int(_fpb * 1.83)
    _wsf.write(str(_wav_path),
               _np.zeros((_frames0, 2), dtype=_np.float32),
               win.bridge.sample_rate)
    _fid, _fr = win.bridge.load_file(str(_wav_path))
    warp_clip = Clip(id=next_id(), file_path=str(_wav_path), file_id=_fid,
                     start_frame=0, length=_fr)
    win.timeline.snap_enabled = True
    win.timeline.snap_beats = 1.0
    win._warp_new_clip_to_grid(warp_clip)
    assert abs(warp_clip.length - int(round(2 * _fpb))) <= 2
    assert abs(warp_clip.stretch_ratio - warp_clip.length / _fr) < 1e-3

    # Grid fit of an existing clip (Snap re-enabled) — all scenarios.
    grid_track = next(t for t in win.project.tracks if t.type == "audio")
    grid_clip = Clip(id=next_id(), file_path=str(_wav_path), file_id=_fid,
                     start_frame=0, length=_fr)
    grid_track.clips.append(grid_clip)
    win.bridge.add_clip(grid_track.id, grid_clip)
    win.timeline.rebuild()

    # Clip properties: changing only the warp method must still sync the
    # engine even at unity stretch_ratio, so model/engine state cannot diverge.
    import pydaw.ui.clip_properties as _clip_props
    _orig_clip_props_dialog = _clip_props.ClipPropertiesDialog
    _orig_set_clip_warp = win.bridge.set_clip_warp
    _warp_sync_calls = []

    class _UnityWarpDialog:
        def __init__(self, *_args, **_kwargs):
            pass

        def exec(self):
            return QDialog.Accepted

        def values(self):
            return (grid_clip.display_name, "stretch_hd", True)

    def _record_set_clip_warp(track_id, clip):
        _warp_sync_calls.append((track_id, clip.id, clip.warp,
                                 clip.stretch_ratio))
        return _orig_set_clip_warp(track_id, clip)

    grid_clip.warp = "stretch"
    grid_clip.stretch_ratio = 1.0
    _clip_props.ClipPropertiesDialog = _UnityWarpDialog
    win.bridge.set_clip_warp = _record_set_clip_warp
    try:
        win._on_clip_properties(grid_track.id, grid_clip.id)
    finally:
        _clip_props.ClipPropertiesDialog = _orig_clip_props_dialog
        win.bridge.set_clip_warp = _orig_set_clip_warp
    assert grid_clip.warp == "stretch_hd"
    assert any(call[:3] == (grid_track.id, grid_clip.id, "stretch_hd")
               for call in _warp_sync_calls)
    win.undo_stack.undo()
    win.undo_stack.clear()
    grid_clip.warp = "stretch"

    def _fit() -> bool:
        return win._warp_existing_clip_to_grid(grid_track.id, grid_clip)

    # (a) Left edge dragged off-grid with Snap off (right edge on grid):
    # the LEFT edge snaps back; the right edge keeps its grid position.
    _beat2 = int(round(2 * _fpb))
    grid_clip.start_frame = int(round(0.3 * _fpb))   # 0.3 beats
    grid_clip.length = _beat2 - grid_clip.start_frame  # right edge at 2.0
    grid_clip.stretch_ratio = grid_clip.length / _fr
    assert _fit()
    assert grid_clip.start_frame == 0                # left edge on grid
    assert abs(grid_clip.end_frame - _beat2) <= 2    # right edge unchanged
    assert abs(grid_clip.stretch_ratio - grid_clip.length / _fr) < 1e-3

    # (b) Clip merely moved off-grid (length already whole steps): both
    # edges snap via a plain move, no re-render needed.
    grid_clip.length = _beat2  # drop the render's ±2-sample rounding
    ratio_b = grid_clip.stretch_ratio
    grid_clip.start_frame = int(round(1.4 * _fpb))
    assert _fit()
    assert grid_clip.start_frame == int(round(1 * _fpb))
    assert abs(grid_clip.stretch_ratio - ratio_b) < 1e-9  # no warp
    assert abs(grid_clip.end_frame - int(round(3 * _fpb))) <= 2

    # (c) Clip already on the grid: nothing changes.
    start_c, len_c = grid_clip.start_frame, grid_clip.length
    assert not _fit()
    assert (grid_clip.start_frame, grid_clip.length) == (start_c, len_c)

    # (d) Both edges off-grid (moved + stretched with Snap off): the
    # start snaps and the length warps to whole steps in one pass.
    grid_clip.start_frame = int(round(0.6 * _fpb))
    grid_clip.length = int(round(1.4 * _fpb))
    grid_clip.stretch_ratio = grid_clip.length / _fr
    assert _fit()
    assert grid_clip.start_frame == int(round(1 * _fpb))
    assert abs(grid_clip.length - int(round(1 * _fpb))) <= 2

    # (e) The Snap toggle itself runs the same fit on auto-warp clips
    # (and skips clips with auto_warp off).
    grid_clip.start_frame = int(round(0.3 * _fpb))
    win.bridge.move_clip(grid_track.id, grid_clip.id, grid_clip.start_frame)
    warp_clip.auto_warp = False
    win._on_snap_toggled(True)
    assert grid_clip.start_frame == 0
    grid_track.clips.remove(grid_clip)
    win.bridge.remove_clip(grid_track.id, grid_clip.id)
    win.timeline.rebuild()

    # Slice mode toggle API.
    assert not win.timeline.slice_mode
    win._toggle_slice_mode()
    assert win.timeline.slice_mode
    assert win.transport.slice_btn.isChecked()
    win._exit_slice_mode()
    assert not win.timeline.slice_mode

    # -- VST model v4 roundtrip + v3 backward compatibility ----------------
    tmp = Path(__file__).parent / "_tmp_gui"
    tmp.mkdir(exist_ok=True)
    vst_project = Project(sample_rate=win.bridge.sample_rate, bpm=120.0)
    audio_t = Track(id=next_id(), name="A")
    midi_t = Track(id=next_id(), name="M", type="midi")
    fake_state = b"vst-state-bytes"
    audio_t.effects.append(Effect(
        type="vst", path="Z:/fake/effect.vst3", uid="ab" * 16,
        name="Fake FX", state=fake_state))
    midi_t.instrument.vst_path = "Z:/fake/synth.vst3"
    midi_t.instrument.vst_uid = "cd" * 16
    midi_t.instrument.vst_name = "Fake Synth"
    midi_t.instrument.vst_state = fake_state
    vst_project.tracks.extend([audio_t, midi_t])
    v4_path = tmp / "vst_v4.pydaw"
    vst_project.save(v4_path)
    loaded_v4 = Project.load(v4_path)
    assert loaded_v4.tracks[0].effects[0].type == "vst"
    assert loaded_v4.tracks[0].effects[0].state == fake_state
    assert loaded_v4.tracks[0].color == audio_t.color
    assert loaded_v4.tracks[1].instrument.vst_name == "Fake Synth"
    assert loaded_v4.tracks[1].instrument.vst_state == fake_state
    data = v4_path.read_text(encoding="utf-8")
    assert f'"version": {FORMAT_VERSION}' in data

    # v3 project without VST fields still loads.
    v3_path = tmp / "legacy_v3.pydaw"
    v3_path.write_text(
        '{"format":"pydaw-project","version":3,"name":"Legacy",'
        '"sample_rate":48000,"bpm":120.0,"metronome":false,'
        '"master_gain":1.0,"loop_start_beat":0.0,"loop_end_beat":0.0,'
        '"loop_enabled":false,"tracks":[{"id":1,"name":"T","type":"audio",'
        '"gain":1.0,"pan":0.0,"mute":false,"solo":false,"clips":[],'
        '"midi_clips":[],"instrument":{"preset":"Saw Lead","wave":2,'
        '"attack":0.005,"decay":0.08,"sustain":0.75,"release":0.2,'
        '"gain":0.5},"effects":[{"type":"delay","params":[0.35,0.35,0.3]}]}]}',
        encoding="utf-8")
    loaded_v3 = Project.load(v3_path)
    assert loaded_v3.tracks[0].effects[0].type == "delay"
    assert loaded_v3.tracks[0].instrument.vst_path == ""
    assert loaded_v3.tracks[0].color in theme.TRACK_COLORS

    # Bridge: bogus VST effect stays in model, sync reports error.
    sync_proj = Project(sample_rate=win.bridge.sample_rate)
    st = Track(id=next_id(), name="Sync")
    st.effects.append(Effect(
        type="vst", path="Z:/missing.vst3", uid="00" * 16, name="Missing"))
    sync_proj.tracks.append(st)
    win.bridge.sync_project(sync_proj)
    assert any("Missing" in e or "missing" in e.lower()
               for e in win.bridge.last_sync_errors)
    assert win.bridge.resolve_fx_slot(st.id, 0) is None

    # Plugin browser from injected cache (no scan).
    cached = [
        PluginInfo(path="/p/a.vst3", name="Alpha", vendor="V",
                   category="Fx", sub_categories="", uid="aa" * 16,
                   is_instrument=False),
        PluginInfo(path="/p/b.vst3", name="Beta Inst", vendor="V",
                   category="Inst", sub_categories="", uid="bb" * 16,
                   is_instrument=True),
    ]
    browser = PluginBrowserDialog(
        win.bridge, win, instruments_only=False, cached_plugins=cached)
    app.processEvents()
    assert browser._model.rowCount() == 2
    browser._filter_edit.setText("alpha")
    browser._apply_filter()
    assert browser._model.rowCount() == 1
    browser.close()

    # -- Track groups: create, assign, reorder, fold, remove, undo/redo ------
    # Project.load() in earlier checks resets the global id counter; resync it.
    all_ids = [t.id for t in win.project.tracks]
    for t in win.project.tracks:
        for c in t.clips:
            all_ids.append(c.id)
        for mc in t.midi_clips:
            all_ids.append(mc.id)
            all_ids.extend(n.id for n in mc.notes)
    bump_id_counter(max(all_ids))

    win.add_group_track()
    app.processEvents()
    group = next(t for t in win.project.tracks if t.is_group)
    audio_track = next(t for t in win.project.tracks if t.type == "audio")
    assert win._menu_actions["track.add_group"].shortcut().toString() \
        == "Ctrl+Shift+G"
    assert win._menu_actions["track.group_selected"].shortcut().toString() \
        == "Ctrl+G"

    win._on_move_to_group(audio_track.id, group.id)
    assert audio_track.group_id == group.id
    g_idx = win.project.tracks.index(group)
    assert win.project.tracks.index(audio_track) == g_idx + 1

    win._on_group_fold_toggled(group.id)
    assert group.id in win._folded_groups
    assert audio_track.id not in win.track_panel.headers
    visible_ids = {t.id for t in win.timeline.visible_tracks()}
    assert audio_track.id not in visible_ids
    assert group.id in visible_ids
    win.undo_stack.undo()
    assert group.id not in win._folded_groups
    assert audio_track.id in win.track_panel.headers

    win._on_track_reordered(audio_track.id, 0)
    assert audio_track.group_id == 0
    assert win.project.tracks.index(audio_track) == 0
    win.undo_stack.undo()
    assert audio_track.group_id == group.id
    win.undo_stack.redo()
    assert audio_track.group_id == 0

    win.undo_stack.undo()
    gid = group.id
    win.remove_track(gid)
    assert win.project.track_by_id(gid) is None
    assert audio_track.group_id == 0
    assert win.project.track_by_id(audio_track.id) is not None
    win.undo_stack.undo()
    assert win.project.track_by_id(gid) is not None
    assert audio_track.group_id == group.id

    win._on_remove_from_group(audio_track.id)
    assert audio_track.group_id == 0
    win.undo_stack.undo()
    assert audio_track.group_id == group.id

    # Delete removes the selected left track header and its contents.
    # (Clear the header selection first: a selected header now anchors
    # where win.add_track() inserts the new track.)
    win.track_panel.set_selected_tracks([])
    _ids_before = {t.id for t in win.project.tracks}
    win.add_track()
    app.processEvents()
    delete_track = next(t for t in win.project.tracks
                        if t.id not in _ids_before)
    assert win.project.tracks[-1] is delete_track
    delete_track.clips.append(Clip(id=next_id(), file_path="delete.wav",
                                   file_id=0, start_frame=0, length=1200))
    win.track_panel.set_selected_track(delete_track.id)
    key_ev = QKeyEvent(QEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier)
    win.track_panel.keyPressEvent(key_ev)
    app.processEvents()
    assert win.project.track_by_id(delete_track.id) is None
    win.undo_stack.undo()
    assert win.project.track_by_id(delete_track.id) is not None
    win.undo_stack.undo()  # undo add_track
    assert win.project.track_by_id(delete_track.id) is None

    # Ctrl+G groups multiple selected non-group tracks into one new group.
    groupable = [t for t in win.project.tracks if not t.is_group][:2]
    win.track_panel.set_selected_tracks([t.id for t in groupable])
    before_group_ids = {t.id for t in win.project.tracks if t.is_group}
    win.group_selected_tracks()
    app.processEvents()
    new_groups = [t for t in win.project.tracks
                  if t.is_group and t.id not in before_group_ids]
    assert len(new_groups) == 1
    new_group = new_groups[0]
    assert all(t.group_id == new_group.id for t in groupable)
    assert win.project.tracks.index(new_group) \
        < min(win.project.tracks.index(t) for t in groupable)
    win.undo_stack.undo()
    assert all(t.group_id != new_group.id for t in groupable)
    assert win.project.track_by_id(new_group.id) is None

    # -- Automation lanes: add, edit points, undo, remove, fold ------------
    audio_track = next(t for t in win.project.tracks if t.type == "audio")
    win._on_add_automation_lane(audio_track.id, "volume", -1, 0, "Volume")
    app.processEvents()
    rows = win.timeline.visible_rows()
    assert any(r["kind"] == "auto" for r in rows)
    assert (audio_track.id, audio_track.automation_lanes[0].id) \
        in win.track_panel.auto_headers
    lane = audio_track.automation_lanes[0]
    # The new lane opens auto-selected (expanded): collapse it first so the
    # click/double-click toggling below starts from the compact state.
    assert win.timeline.selected_lane() == (audio_track.id, lane.id)
    win._on_automation_lane_clicked(audio_track.id, lane.id)
    app.processEvents()

    # Automation lane height is toggled only by double-clicking the left
    # lane header. Timeline clicks/editing must not collapse the lane.
    key = (audio_track.id, lane.id)
    lane_header = win.track_panel.auto_headers[key]
    press = QMouseEvent(QEvent.MouseButtonPress, QPointF(8, 8),
                        QPointF(lane_header.mapToGlobal(QPoint(8, 8))),
                        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    lane_header.mousePressEvent(press)
    app.processEvents()
    assert win.timeline.row_info_for_lane(*key)["height"] == _ALH
    dbl = QMouseEvent(QEvent.MouseButtonDblClick, QPointF(8, 8),
                      QPointF(lane_header.mapToGlobal(QPoint(8, 8))),
                      Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    lane_header.mouseDoubleClickEvent(dbl)
    app.processEvents()
    assert win.timeline.row_info_for_lane(*key)["height"] == _ALHS

    lane_item = win.timeline._auto_items[key]
    row = win.timeline.row_info_for_lane(*key)

    # Automation handles directly under the current playhead keep edit priority.
    # The playhead remains draggable from free parts of its vertical line.
    win.timeline.snap_enabled = False
    win.timeline.set_playhead(0)
    seek_frames: list[int] = []
    win.timeline.seek_requested.connect(seek_frames.append)
    point_start_scene = lane_item.mapToScene(QPointF(
        lane_item._beat_to_x(lane.points[0].beat),
        lane_item._value_to_y(lane.points[0].value)))
    point_start = win.timeline.mapFromScene(point_start_scene)
    point_end = QPoint(point_start.x() + 35, point_start.y() - 12)
    point_press = QMouseEvent(
        QEvent.MouseButtonPress, QPointF(point_start),
        QPointF(win.timeline.mapToGlobal(point_start)),
        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    win.timeline.mousePressEvent(point_press)
    point_move = QMouseEvent(
        QEvent.MouseMove, QPointF(point_end),
        QPointF(win.timeline.mapToGlobal(point_end)),
        Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
    win.timeline.mouseMoveEvent(point_move)
    point_release = QMouseEvent(
        QEvent.MouseButtonRelease, QPointF(point_end),
        QPointF(win.timeline.mapToGlobal(point_end)),
        Qt.LeftButton, Qt.NoButton, Qt.NoModifier)
    win.timeline.mouseReleaseEvent(point_release)
    app.processEvents()
    assert len(lane.points) == 1
    assert lane.points[0].beat > 0.0
    assert not seek_frames
    win.undo_stack.undo()
    assert lane.points[0].beat == 0.0
    win.timeline.snap_enabled = True

    vp = win.timeline.mapFromScene(8, row["y"] + 8)
    timeline_press = QMouseEvent(
        QEvent.MouseButtonPress, QPointF(vp),
        QPointF(win.timeline.mapToGlobal(vp)), Qt.LeftButton, Qt.LeftButton,
        Qt.NoModifier)
    win.timeline.mousePressEvent(timeline_press)
    app.processEvents()
    assert win.timeline.row_info_for_lane(*key)["height"] == _ALHS
    # Single click on the lane adds an automation point (undoable).
    timeline_release = QMouseEvent(
        QEvent.MouseButtonRelease, QPointF(vp),
        QPointF(win.timeline.mapToGlobal(vp)), Qt.LeftButton, Qt.NoButton,
        Qt.NoModifier)
    win.timeline.mouseReleaseEvent(timeline_release)
    app.processEvents()
    # The lane is created with one initial point (current slider value);
    # the click adds a second one.
    assert len(lane.points) == 2
    win.undo_stack.undo()
    assert len(lane.points) == 1 and lane.points[0].beat == 0.0

    # Single click adds a point; Ctrl bypasses snap.
    win.timeline.snap_enabled = True
    win.timeline.snap_beats = 1.0
    free_beat = 0.37
    pos = QPointF(win.timeline.x_at_frame(
        win.project.beats_to_frames(free_beat)), 20)
    ev = QGraphicsSceneMouseEvent(QEvent.GraphicsSceneMousePress)
    ev.setButton(Qt.LeftButton)
    ev.setButtons(Qt.LeftButton)
    ev.setModifiers(Qt.ControlModifier)
    ev.setPos(pos)
    lane_item.mousePressEvent(ev)
    rel = QGraphicsSceneMouseEvent(QEvent.GraphicsSceneMouseRelease)
    rel.setButton(Qt.LeftButton)
    rel.setModifiers(Qt.ControlModifier)
    rel.setPos(pos)
    lane_item.mouseReleaseEvent(rel)
    app.processEvents()
    assert any(abs(p.beat - free_beat) < 0.02 for p in lane.points)
    win.undo_stack.undo()
    assert len(lane.points) == 1

    # Hover/hit geometry targets the source point of a segment. Ctrl+wheel
    # changes its curve mode, while vertical dragging changes signed amount.
    lane.points = [AutomationPoint(0.0, 0.2, "exp"),
                   AutomationPoint(2.0, 0.8)]
    lane_item.update()
    segment_points = lane_item._segment_polyline(lane.points[0], lane.points[1])
    segment_pos = segment_points[len(segment_points) // 2]
    assert lane_item._segment_at(segment_pos) == 0
    assert lane_item._segment_at(QPointF(segment_pos.x(), 0.0)) is None

    scene_segment_pos = lane_item.mapToScene(segment_pos)
    viewport_segment_pos = QPointF(win.timeline.mapFromScene(scene_segment_pos))
    wheel = QWheelEvent(
        viewport_segment_pos,
        QPointF(win.timeline.viewport().mapToGlobal(
            viewport_segment_pos.toPoint())),
        QPoint(), QPoint(0, 120), Qt.NoButton, Qt.ControlModifier,
        Qt.ScrollPhase.ScrollUpdate, False)
    win.timeline.wheelEvent(wheel)
    app.processEvents()
    assert lane.points[0].shape == "bezier"
    win.undo_stack.undo()
    assert lane.points[0].shape == "exp"

    press_curve = QGraphicsSceneMouseEvent(QEvent.GraphicsSceneMousePress)
    press_curve.setButton(Qt.LeftButton)
    press_curve.setButtons(Qt.LeftButton)
    press_curve.setPos(segment_pos)
    lane_item.mousePressEvent(press_curve)
    move_curve = QGraphicsSceneMouseEvent(QEvent.GraphicsSceneMouseMove)
    move_curve.setButtons(Qt.LeftButton)
    move_curve.setPos(QPointF(segment_pos.x(), segment_pos.y() - 40.0))
    lane_item.mouseMoveEvent(move_curve)
    release_curve = QGraphicsSceneMouseEvent(QEvent.GraphicsSceneMouseRelease)
    release_curve.setButton(Qt.LeftButton)
    release_curve.setPos(QPointF(segment_pos.x(), segment_pos.y() - 40.0))
    lane_item.mouseReleaseEvent(release_curve)
    app.processEvents()
    assert abs(lane.points[0].curve_amount - 0.5) < 1e-9
    win.undo_stack.undo()
    assert lane.points[0].curve_amount == 1.0

    lane.points[0].curve_amount = 0.0
    segment_points = lane_item._segment_polyline(lane.points[0], lane.points[1])
    segment_pos = segment_points[len(segment_points) // 2]
    press_curve.setPos(segment_pos)
    lane_item.mousePressEvent(press_curve)
    move_curve.setPos(QPointF(segment_pos.x(), segment_pos.y() - 40.0))
    lane_item.mouseMoveEvent(move_curve)
    release_curve.setPos(QPointF(segment_pos.x(), segment_pos.y() - 40.0))
    lane_item.mouseReleaseEvent(release_curve)
    app.processEvents()
    assert abs(lane.points[0].curve_amount + 0.5) < 1e-9
    win.undo_stack.undo()
    assert lane.points[0].curve_amount == 0.0

    # The same physical drag has the opposite numeric sign on a descending
    # segment, so its visual bending direction stays consistent.
    lane.points = [AutomationPoint(0.0, 0.8, "exp", 0.0),
                   AutomationPoint(2.0, 0.2)]
    segment_points = lane_item._segment_polyline(lane.points[0], lane.points[1])
    segment_pos = segment_points[len(segment_points) // 2]
    press_curve.setPos(segment_pos)
    lane_item.mousePressEvent(press_curve)
    move_curve.setPos(QPointF(segment_pos.x(), segment_pos.y() - 40.0))
    lane_item.mouseMoveEvent(move_curve)
    release_curve.setPos(QPointF(segment_pos.x(), segment_pos.y() - 40.0))
    lane_item.mouseReleaseEvent(release_curve)
    app.processEvents()
    assert abs(lane.points[0].curve_amount - 0.5) < 1e-9
    win.undo_stack.undo()
    assert lane.points[0].curve_amount == 0.0

    win._on_automation_lane_clicked(*key)  # collapse via left header behavior
    app.processEvents()
    assert win.timeline.row_info_for_lane(*key)["height"] == _ALH

    win._on_automation_points_changed(
        audio_track.id, lane.id, [], [(0.0, 0.0), (1.0, 1.0)])
    assert len(lane.points) == 2
    win.undo_stack.undo()
    assert len(lane.points) == 0
    win.undo_stack.redo()
    assert len(lane.points) == 2
    win._on_remove_automation_lane(audio_track.id, lane.id)
    assert not audio_track.automation_lanes
    win.undo_stack.undo()
    assert audio_track.automation_lanes
    win._on_group_fold_toggled(group.id)
    folded_rows = win.timeline.visible_rows()
    assert not any(r["kind"] == "auto" and r["track"].id == audio_track.id
                   for r in folded_rows)
    win.undo_stack.undo()

    # Mixer shows group strip without crashing; child strips sit in a cluster.
    win.open_mixer()
    app.processEvents()
    assert gid in win._mixer.strips
    from pydaw.ui.mixer_window import GroupStripCluster
    _grp_cluster = None
    for tid, strip in win._mixer.strips.items():
        t = win.project.track_by_id(tid)
        if t is not None and t.group_id == gid:
            parent = strip.parentWidget()
            assert isinstance(parent, GroupStripCluster)
            assert parent.group.id == gid
            _grp_cluster = parent
        assert strip._fx_btn.isVisible()
        assert strip._fx_btn.text() == "FX"
    assert _grp_cluster is not None
    assert win._mixer.strips[gid]._fx_btn.isVisible()
    win._mixer.close()
    app.processEvents()

    # -- Track colors, rename (Ctrl+R), properties, lane tints ------------
    win.add_track()
    app.processEvents()
    assert win.project.tracks[-1].color in theme.TRACK_COLORS

    panel = win.track_panel
    audio_track = next(t for t in win.project.tracks if t.type == "audio")
    hdr = panel.headers[audio_track.id]
    assert hdr._accent == audio_track.color

    midi_t = next(t for t in win.project.tracks if t.is_midi)
    group_t = next(t for t in win.project.tracks if t.is_group)
    assert lane_fill_color(midi_t, 0) == LANE_COLOR
    assert lane_fill_color(midi_t, 1) == LANE_ALT_COLOR
    # Group lane is tinted with the group's own color (not the neutral
    # lane and not the fixed LANE_GROUP tone).
    group_lane = lane_fill_color(group_t, 0)
    assert group_lane != LANE_COLOR
    from pydaw.ui.timeline import _mix as _lane_mix
    from PySide6.QtGui import QColor as _QC
    assert group_lane == _lane_mix(LANE_COLOR, _QC(group_t.color), 0.16)

    # Ctrl+Shift+R opens a small rename dialog (same style as clip
    # rename); patch it out so the test stays non-modal.
    panel.set_selected_track(audio_track.id)
    assert hdr.name_edit.isReadOnly()
    import pydaw.ui.main_window as _mw
    _orig_get_text = _mw.QInputDialog.getText
    _mw.QInputDialog.getText = staticmethod(
        lambda *a, **k: ("Renamed track", True))
    try:
        win._rename_selected_track()
    finally:
        _mw.QInputDialog.getText = _orig_get_text
    app.processEvents()
    assert audio_track.name == "Renamed track"
    assert hdr.name_edit.isReadOnly()
    win.undo_stack.undo()
    assert audio_track.name != "Renamed track"
    # With several headers selected the shortcut does nothing.
    panel.set_selected_tracks([audio_track.id, midi_t.id])
    _mw.QInputDialog.getText = staticmethod(
        lambda *a, **k: ("Should not apply", True))
    try:
        win._rename_selected_track()
    finally:
        _mw.QInputDialog.getText = _orig_get_text
    assert audio_track.name != "Should not apply"
    assert midi_t.name != "Should not apply"

    # Header context menu "Rename" routes through the same dialog for
    # the clicked track (works regardless of the current selection).
    _mw.QInputDialog.getText = staticmethod(
        lambda *a, **k: ("Menu renamed", True))
    try:
        hdr.rename_requested.emit(audio_track.id)
    finally:
        _mw.QInputDialog.getText = _orig_get_text
    app.processEvents()
    assert audio_track.name == "Menu renamed"
    win.undo_stack.undo()
    assert audio_track.name != "Menu renamed"

    # Double-click no longer starts rename.
    panel.set_selected_track(audio_track.id)
    local = hdr.rect().center()
    dbl = QMouseEvent(
        QEvent.MouseButtonDblClick, QPointF(local),
        QPointF(hdr.mapToGlobal(local)), Qt.LeftButton, Qt.LeftButton,
        Qt.NoModifier)
    hdr.mouseDoubleClickEvent(dbl)
    assert hdr.name_edit.isReadOnly()

    # Properties dialog: name + color as one undoable step (no modal exec).
    old_name, old_color = audio_track.name, audio_track.color
    new_color = theme.TRACK_COLORS[(theme.TRACK_COLORS.index(old_color) + 3)
                                   % len(theme.TRACK_COLORS)]
    props = TrackPropertiesDialog(audio_track, win)
    props._name_edit.setText("Props Name")
    props._pick_palette(new_color)
    pname, pcolor = props.result_values()
    win.undo_stack.push(Command(
        lambda: win._apply_track_properties(audio_track.id, pname, pcolor),
        lambda: win._apply_track_properties(audio_track.id, old_name, old_color),
        "Track properties"))
    app.processEvents()
    assert audio_track.name == "Props Name"
    assert audio_track.color == new_color
    assert panel.headers[audio_track.id]._accent == new_color
    win.undo_stack.undo()
    app.processEvents()
    assert audio_track.name == old_name
    assert audio_track.color == old_color

    # -- LMB drag: reorder + drop into group + no nested groups -------------
    group = group_t
    if audio_track.group_id == group.id:   # left inside by the undo above
        win._on_remove_from_group(audio_track.id)
        app.processEvents()
    ghdr = panel.headers[group.id]
    gp_mid = panel._container.mapToGlobal(ghdr.geometry().center())
    panel._start_drag(audio_track.id)
    panel._on_drag_moved(audio_track.id, gp_mid)
    assert panel._drop_group_id == group.id
    assert ghdr._drop_target
    panel._on_drag_dropped(audio_track.id, gp_mid)
    app.processEvents()
    assert audio_track.group_id == group.id
    assert panel.headers[audio_track.id]._is_child
    win.undo_stack.undo()
    app.processEvents()

    win.add_group_track()
    app.processEvents()
    g2 = [t for t in win.project.tracks if t.is_group][-1]
    win._on_move_to_group(g2.id, group.id)   # groups cannot be nested
    assert g2.group_id == 0
    panel._start_drag(g2.id)
    panel._on_drag_moved(g2.id, gp_mid)
    assert panel._drop_group_id is None
    panel._on_drag_dropped(g2.id, gp_mid)
    win.remove_track(g2.id)
    app.processEvents()

    # -- Playhead seek snaps to the grid (Alt bypasses) ----------------------
    win.timeline.snap_beats = 1.0
    seeks: list[int] = []
    win.timeline.seek_requested.connect(seeks.append)
    fpb = win.project.frames_per_beat
    x = win.timeline.x_at_frame(int(2.3 * fpb))
    # Click below the rows: lanes now consume clicks to add points.
    vp = win.timeline.mapFromScene(x, win.timeline._total_rows_height() + 10)
    for mods, expect_snap in ((Qt.NoModifier, True), (Qt.AltModifier, False)):
        ev = QMouseEvent(QEvent.MouseButtonPress, QPointF(vp),
                         QPointF(win.timeline.mapToGlobal(vp)),
                         Qt.LeftButton, Qt.LeftButton, mods)
        win.timeline.mousePressEvent(ev)
        if expect_snap:
            assert seeks and seeks[-1] == int(round(2 * fpb)), \
                f"seek not snapped: {seeks}"
        else:
            assert abs(seeks[-1] - int(2.3 * fpb)) \
                <= win.timeline.samples_per_px + 1

    # -- Track panel / timeline scroll alignment ---------------------------
    if win.timeline.verticalScrollBar().maximum() <= 20:
        for _ in range(4):
            win.add_track()
        app.processEvents()
    tbar = win.timeline.verticalScrollBar()
    pbar = win.track_panel.verticalScrollBar()
    assert tbar.maximum() > 20 and pbar.maximum() > 20

    def _assert_row_alignment() -> None:
        for row in win.timeline.visible_rows():
            if row["kind"] == "track" and row["track"].id \
                    in win.track_panel.headers:
                widget = win.track_panel.headers[row["track"].id]
                scene_y = win.timeline.mapFromScene(0, row["y"]).y()
                panel_y = widget.mapTo(
                    win.track_panel.viewport(), widget.rect().topLeft()).y()
                assert abs(scene_y - panel_y) <= 1, \
                    (scene_y, panel_y, row)
                return
        raise AssertionError("no comparable visible row")

    tbar.setValue(20)
    app.processEvents()
    _assert_row_alignment()
    pbar.setValue(55)
    app.processEvents()
    _assert_row_alignment()

    # -- Add-effect menu anchored to its button ------------------------------
    from pydaw.ui.fx_panel import anchored_menu_pos
    win._select_track_for_fx(audio_track.id)
    app.processEvents()
    card = None
    for i in range(win.fx_panel._strip.count()):
        w = win.fx_panel._strip.itemAt(i).widget()
        if w is not None and w.__class__.__name__ == "_AddEffectCard":
            card = w
    assert card is not None
    from PySide6.QtWidgets import QMenu
    m = QMenu()
    m.addAction("a")
    pos = anchored_menu_pos(m, card._btn)
    screen = card._btn.screen().availableGeometry()
    assert screen.contains(pos)
    assert screen.contains(QPoint(pos.x() + m.sizeHint().width() - 1,
                                  pos.y() + m.sizeHint().height() - 1))

    # -- Settings dialog: tabs, theme, shortcuts ---------------------------
    dark_bg = theme.BG
    dark_playhead, dark_loop = theme.PLAYHEAD, theme.LOOP_REGION
    set_theme("light")
    assert theme.BG != dark_bg
    assert theme.BG == theme._LIGHT["BG"]
    # Playhead and loop-region colors are per-theme (visibly different
    # between light and dark).
    assert theme.PLAYHEAD != dark_playhead
    assert theme.LOOP_REGION != dark_loop
    set_theme("dark")
    assert theme.BG == dark_bg
    assert theme.PLAYHEAD == dark_playhead

    settings = QSettings("PyDAW", "PyDAW")
    sdlg = SettingsDialog(win.bridge, win._menu_actions,
                          win._default_shortcuts, settings, win)
    app.processEvents()
    assert sdlg.windowTitle() == "Settings"
    tab_widget = sdlg.findChild(QTabWidget)
    assert tab_widget is not None
    assert tab_widget.count() == 4  # appearance, audio, shortcuts, help
    for idx in range(4):
        tab_widget.setCurrentIndex(idx)
        app.processEvents()
    assert hasattr(sdlg, "api_combo")

    # Shortcut override applied to QAction
    undo_action = win._menu_actions["edit.undo"]
    default_undo = win._default_shortcuts["edit.undo"]
    sdlg._shortcut_values["edit.undo"] = "Ctrl+Shift+Z"
    overrides = sdlg.shortcut_overrides
    assert "edit.undo" in overrides
    undo_action.setShortcut(QKeySequence(overrides["edit.undo"]))
    assert undo_action.shortcut().toString() == "Ctrl+Shift+Z"
    undo_action.setShortcut(default_undo)

    sdlg.close()
    app.processEvents()

    # -- TWEAK mode: touch a control -> confirm dialog -> automation lane ----
    from PySide6.QtCore import QTimer as _QTimer

    from pydaw.ui.tweak_dialog import TweakDialog as _TweakDlg

    win.add_track()
    app.processEvents()
    tweak_track = win.project.tracks[-1]
    assert win.transport.tweak_btn.isCheckable()
    assert not win._tweak_mode
    win.transport.tweak_btn.setChecked(True)  # arm TWEAK from the toolbar
    app.processEvents()
    assert win._tweak_mode

    def _click_box(accept: bool) -> None:
        clicked = []

        def _try() -> None:
            for w in app.topLevelWidgets():
                if isinstance(w, _TweakDlg) and w.isVisible():
                    # The themed dialog window title is "Tweak" (not TWEAK).
                    assert w.windowTitle() == "Tweak"
                    (w.accept if accept else w.reject)()
                    clicked.append(True)
                    return
            _QTimer.singleShot(10, _try)
        _QTimer.singleShot(10, _try)

    # Ok path: gain tweak arms the candidate, dialog creates the lane.
    win._on_gain(tweak_track.id, 1.1)
    win._discard_param_gesture()  # keep the undo stack deterministic
    assert win._tweak_candidate is not None
    assert win._tweak_candidate[0] == tweak_track.id
    assert win._tweak_candidate[1] == "volume"
    assert win._tweak_timer.isActive()  # 1 s debounce armed
    win._tweak_timer.stop()
    _click_box(True)
    win._show_tweak_dialog()
    app.processEvents()
    assert not win._tweak_mode  # Ok disarms TWEAK
    assert not win.transport.tweak_btn.isChecked()
    volume_lane = next(
        l for l in tweak_track.automation_lanes if l.target == "volume")
    assert abs(tweak_track.gain - 1.1) < 1e-9
    assert abs(volume_lane.points[0].value - (1.1 / 1.5)) < 1e-9

    # Cancel path: pan tweak, dialog rejected -> no lane, TWEAK disarmed.
    win.transport.tweak_btn.setChecked(True)
    app.processEvents()
    win._on_pan(tweak_track.id, 0.4)
    win._discard_param_gesture()
    assert win._tweak_candidate is not None and \
        win._tweak_candidate[1] == "pan"
    win._tweak_timer.stop()
    _click_box(False)
    win._show_tweak_dialog()
    app.processEvents()
    assert not any(l.target == "pan" for l in tweak_track.automation_lanes)
    assert not win._tweak_mode
    assert not win.transport.tweak_btn.isChecked()
    assert abs(tweak_track.pan - 0.4) < 1e-9

    # Tweaks are ignored while TWEAK is off.
    win._on_pan(tweak_track.id, -0.2)
    win._discard_param_gesture()
    assert win._tweak_candidate is None
    assert not win._tweak_timer.isActive()

    # Undo/redo does not conflict with TWEAK: undoing a control move is
    # a programmatic write and must not (re)arm a tweak candidate.
    win.undo_stack.clear()
    win.transport.tweak_btn.setChecked(True)
    app.processEvents()
    assert win._tweak_mode
    _g0 = tweak_track.gain
    win._on_gain(tweak_track.id, 0.7)       # user tweak arms a candidate
    assert win._tweak_candidate is not None
    win._tweak_candidate = None             # simulate: not answered yet
    win._tweak_timer.stop()
    win.undo_stack.undo()                   # flush + undo the gain move
    assert abs(tweak_track.gain - _g0) < 1e-9
    assert win._tweak_candidate is None     # undo armed nothing
    assert not win._tweak_timer.isActive()
    win.undo_stack.redo()
    assert abs(tweak_track.gain - 0.7) < 1e-9
    assert win._tweak_candidate is None     # redo armed nothing
    win.transport.tweak_btn.setChecked(False)
    app.processEvents()
    win.undo_stack.clear()

    # -- Undoable add/remove of effects ------------------------------------
    fx_track = tweak_track
    assert not fx_track.effects
    win._on_effect_added(fx_track.id, "delay")
    app.processEvents()
    assert len(fx_track.effects) == 1 and fx_track.effects[0].type == "delay"
    win.undo_stack.undo()
    assert not fx_track.effects            # undo removes the added effect
    win.undo_stack.redo()
    assert len(fx_track.effects) == 1      # redo brings it back

    # Remove effect together with its automation lane; undo restores both.
    win._on_add_automation_lane(fx_track.id, "fx", 0, 0, "Delay: Time (s)")
    app.processEvents()
    assert any(l.target == "fx" and l.fx_slot == 0
               for l in fx_track.automation_lanes)
    n_lanes = len(fx_track.automation_lanes)
    win._on_effect_removed(fx_track.id, 0)
    app.processEvents()
    assert not fx_track.effects
    assert not any(l.target == "fx" for l in fx_track.automation_lanes)
    win.undo_stack.undo()                  # restore effect + its lane
    assert len(fx_track.effects) == 1
    assert len(fx_track.automation_lanes) == n_lanes
    assert any(l.target == "fx" and l.fx_slot == 0
               for l in fx_track.automation_lanes)
    win.undo_stack.redo()
    assert not fx_track.effects
    # Unwind this section: redo-removed state -> undo everything back.
    for _ in range(3):
        win.undo_stack.undo()
    assert not fx_track.effects
    win.undo_stack.clear()

    # FX chain order: effects are applied left-to-right, i.e. the engine
    # chain slot always equals the model index (the mixer processes the
    # chain vector in order). Holds after adds and after a reorder.
    win._on_effect_added(fx_track.id, "delay")
    win._on_effect_added(fx_track.id, "filter")
    win._on_effect_added(fx_track.id, "delay")
    app.processEvents()
    assert [fx.type for fx in fx_track.effects] == \
        ["delay", "filter", "delay"]
    for slot in range(3):
        assert win.bridge.resolve_fx_slot(fx_track.id, slot) == slot
    win._on_effect_moved(fx_track.id, 0, 2)  # drag delay to the far right
    app.processEvents()
    assert [fx.type for fx in fx_track.effects] == \
        ["filter", "delay", "delay"]
    for slot in range(3):
        assert win.bridge.resolve_fx_slot(fx_track.id, slot) == slot
    win.undo_stack.undo()                    # reorder back
    assert [fx.type for fx in fx_track.effects] == \
        ["delay", "filter", "delay"]
    for slot in range(3):
        assert win.bridge.resolve_fx_slot(fx_track.id, slot) == slot
    for _ in range(3):
        win.undo_stack.undo()                # remove the three effects
    assert not fx_track.effects
    win.undo_stack.clear()

    # -- Releasing a control closes the undo gesture immediately ------------
    from pydaw.ui import knob as _knob
    g0 = fx_track.gain
    win._on_gain(fx_track.id, 0.8)
    _knob._notify_gesture_end()            # simulate slider release
    assert win._param_gesture is None      # gesture pushed, not pending
    win._on_gain(fx_track.id, 0.5)         # second drag right away
    _knob._notify_gesture_end()
    assert win.undo_stack.can_undo
    win.undo_stack.undo()                  # two separate steps, not one
    assert abs(fx_track.gain - 0.8) < 1e-9
    win.undo_stack.undo()
    assert abs(fx_track.gain - g0) < 1e-9
    win.undo_stack.clear()

    # FX cards expose the full VST parameter list (no 8-slider cap).
    import inspect as _inspect
    from pydaw.ui.vst_widgets import VstParamPanel as _VPP
    assert _inspect.signature(_VPP.__init__).parameters[
        "max_params"].default >= 128

    # -- Undoable mute/solo/tempo/loop --------------------------------------
    win._on_mute(fx_track.id, True)
    assert fx_track.mute
    win.undo_stack.undo()
    assert not fx_track.mute
    win.undo_stack.redo()
    assert fx_track.mute
    win._on_mute(fx_track.id, False)

    win._on_solo(fx_track.id, True)
    assert fx_track.solo
    win.undo_stack.undo()
    assert not fx_track.solo

    bpm0 = win.project.bpm
    win._on_bpm_changed(140.0)
    win._finish_param_gesture()            # close the coalesced gesture
    assert abs(win.project.bpm - 140.0) < 1e-9
    win.undo_stack.undo()
    assert abs(win.project.bpm - bpm0) < 1e-9
    assert abs(win.transport.bpm_spin.value() - bpm0) < 1e-6

    win._on_loop_region(2.0, 6.0)
    assert win.project.loop_enabled
    assert win.project.loop_start_beat == 2.0
    win._on_loop_region(2.0, 2.0)          # collapse removes the region
    assert not win.project.loop_enabled
    win.undo_stack.undo()                  # back to 2..6, enabled
    assert win.project.loop_enabled and win.project.loop_end_beat == 6.0
    win._on_loop_toggled(False)
    assert not win.project.loop_enabled
    win.undo_stack.undo()
    assert win.project.loop_enabled
    assert win.transport.loop_btn.isChecked()
    for _ in range(3):
        win.undo_stack.undo()              # unwind loop edits
    win.undo_stack.clear()

    # -- Ctrl+L: loop region spans the selected clip(s) ----------------------
    from pydaw.ui.clip_item import MidiClipItem as _MCI
    win.add_midi_clip(midi_track.id, 8.0)
    c1 = midi_track.midi_clips[-1]
    win.add_midi_clip(midi_track.id, 16.0)
    c2 = midi_track.midi_clips[-1]
    app.processEvents()

    def _select_clips(ids) -> None:
        win.timeline._scene.clearSelection()
        for it in win.timeline._scene.items():
            if isinstance(it, _MCI) and it.clip.id in ids:
                it.setSelected(True)

    # One selected clip -> region matches its bounds and looping turns on.
    win._apply_loop(0.0, 0.0, False)
    _select_clips({c1.id})
    win._on_loop_shortcut()
    assert win.project.loop_enabled
    assert win.project.loop_start_beat == c1.start_beat
    assert win.project.loop_end_beat == c1.end_beat
    # Multiple selected clips -> region covers them all.
    _select_clips({c1.id, c2.id})
    win._on_loop_shortcut()
    assert win.project.loop_start_beat == c1.start_beat
    assert win.project.loop_end_beat == c2.end_beat
    assert win.project.loop_enabled
    # The region command is undoable like a ruler drag.
    win.undo_stack.undo()
    assert win.project.loop_end_beat == c1.end_beat
    win.undo_stack.redo()
    assert win.project.loop_end_beat == c2.end_beat
    # No selection -> plain loop on/off toggle (old behaviour).
    win.timeline._scene.clearSelection()
    was_on = win.transport.loop_btn.isChecked()
    win._on_loop_shortcut()
    assert win.transport.loop_btn.isChecked() != was_on
    win._on_loop_shortcut()
    assert win.transport.loop_btn.isChecked() == was_on
    # The beat span helper is pure model math -> identical at any UI scale.
    assert win._selected_clips_beat_span() is None
    _select_clips({c1.id, c2.id})
    assert win._selected_clips_beat_span() == (c1.start_beat, c2.end_beat)
    win.timeline._scene.clearSelection()
    win._on_clip_remove(midi_track.id, c2.id, True)
    win._on_clip_remove(midi_track.id, c1.id, True)
    win.undo_stack.clear()
    app.processEvents()

    # -- Track panel rebuild must not flash stray top-level windows ---------
    # (headers/lane headers are parented at construction now).
    win._on_add_automation_lane(midi_track.id, "volume", -1, 0, "Volume")
    app.processEvents()
    top_before_panel = set(app.topLevelWidgets())
    win.track_panel.rebuild()
    app.processEvents()
    assert set(app.topLevelWidgets()) - top_before_panel == set()
    for header in win.track_panel.headers.values():
        assert header.parent() is win.track_panel._container
    for ah in win.track_panel.auto_headers.values():
        assert ah.parent() is win.track_panel._container
    win._on_remove_automation_lane(
        midi_track.id, midi_track.automation_lanes[0].id)
    win.undo_stack.clear()
    app.processEvents()

    # -- A switched-off loop region disappears from the timeline -------------
    win._on_loop_region(2.0, 6.0)
    assert win.timeline._loop_region_beats() == (2.0, 6.0)
    win._on_loop_toggled(False)            # toolbar button / Ctrl+L
    # Nothing is drawn (no gray remnant) and edges are not grabbable.
    assert win.timeline._loop_region_beats() is None
    ppb2 = win.timeline.project.frames_per_beat / win.timeline.samples_per_px
    tr2 = win.timeline.viewportTransform()
    edge_x = tr2.map(QPointF(2.0 * ppb2, 0.0)).x()
    assert win.timeline._ruler_loop_edge_hit(edge_x) is None
    win._on_loop_toggled(True)             # back on: region reappears
    assert win.timeline._loop_region_beats() == (2.0, 6.0)
    # Collapsing an edge to zero width removes the region and unchecks
    # the transport Loop button.
    _ruler_drag(win.timeline, 2.0, 6.0)    # drag left edge onto the right
    assert not win.project.loop_enabled
    assert not win.transport.loop_btn.isChecked()
    assert win.timeline._loop_region_beats() is None
    win.undo_stack.clear()

    # -- Collapsing a fresh create-drag to zero width switches the loop off --
    win._on_loop_region(2.0, 6.0)
    assert win.project.loop_enabled
    # Drag on empty ruler far to the right, then release back at the
    # anchor beat: the preview collapsed to a line -> loop off, region gone.
    tl2 = win.timeline
    tl2.mousePressEvent(_ruler_mouse(tl2, 10.0, QEvent.MouseButtonPress))
    assert tl2._loop_drag_mode == "create"
    tl2.mouseMoveEvent(_ruler_mouse(tl2, 14.0, QEvent.MouseMove))
    tl2.mouseMoveEvent(_ruler_mouse(tl2, 10.0, QEvent.MouseMove))
    tl2.mouseReleaseEvent(
        _ruler_mouse(tl2, 10.0, QEvent.MouseButtonRelease))
    app.processEvents()
    assert not win.project.loop_enabled
    assert not win.transport.loop_btn.isChecked()
    assert tl2._loop_region_beats() is None
    # A plain click (<= 3 px, no drag) still seeks and never touches loop.
    win._on_loop_region(2.0, 6.0)
    seeks2: list[int] = []
    conn2 = tl2.seek_requested.connect(seeks2.append)
    tl2.mousePressEvent(_ruler_mouse(tl2, 12.0, QEvent.MouseButtonPress))
    tl2.mouseReleaseEvent(
        _ruler_mouse(tl2, 12.0, QEvent.MouseButtonRelease))
    app.processEvents()
    assert len(seeks2) == 1
    assert win.project.loop_enabled
    tl2.seek_requested.disconnect(conn2)

    # -- Resize arrows reset after an edge drag (no stuck/flickering cursor) --
    _ruler_drag(tl2, 6.0, 8.0)             # right edge 6 -> 8
    assert win.project.loop_end_beat == 8.0
    # The drag marked the cursor as ruler-owned; a move far from the edges
    # must clear the resize arrows.
    tl2.mouseMoveEvent(_ruler_mouse(tl2, 4.0, QEvent.MouseMove,
                                    btn=Qt.NoButton, buttons=Qt.NoButton))
    assert tl2.viewport().cursor().shape() != Qt.SizeHorCursor
    # And hovering an edge shows them again.
    tl2.mouseMoveEvent(_ruler_mouse(tl2, 8.0, QEvent.MouseMove,
                                    btn=Qt.NoButton, buttons=Qt.NoButton))
    assert tl2.viewport().cursor().shape() == Qt.SizeHorCursor
    tl2.mouseMoveEvent(_ruler_mouse(tl2, 4.0, QEvent.MouseMove,
                                    btn=Qt.NoButton, buttons=Qt.NoButton))
    win._on_loop_toggled(False)
    win.undo_stack.clear()
    app.processEvents()

    # Hover works without any button held: viewport mouse tracking is on,
    # so buttonless moves reach mouseMoveEvent and show the arrows.
    assert tl2.hasMouseTracking()
    assert tl2.viewport().hasMouseTracking()

    # -- Grouped tracks keep their own color on the left strip ---------------
    child_track = win.project.tracks[0]
    win.add_group_track()
    app.processEvents()
    grp_track = next(t for t in win.project.tracks if t.is_group)
    win._on_move_to_group(child_track.id, grp_track.id)
    app.processEvents()
    child_header = win.track_panel.headers[child_track.id]
    style = child_header.styleSheet()
    from pydaw.ui.track_header import GROUP_CHILD_INDENT
    # The wider child strip carries the track's own color, not a fixed
    # amber recolor. (The random palette color may itself be amber, so
    # assert on the border-left value specifically.)
    assert f"border-left: {GROUP_CHILD_INDENT}px solid " \
           f"{child_track.color}" in style
    win._on_remove_from_group(child_track.id)
    win.undo_stack.clear()
    app.processEvents()

    # -- Settings -> Help: global tooltip switch ------------------------------
    from PySide6.QtCore import QEvent as _QEvent, QPoint as _QPoint
    from PySide6.QtGui import QHelpEvent as _QHelpEvent
    from pydaw.ui.settings_dialog import SettingsDialog as _SD

    from PySide6.QtCore import QObject as _QObject

    class _ToolTipSpy(_QObject):
        """Widget-level filter: sees ToolTip events only when the
        app-level suppressor (which runs first) lets them through."""
        def __init__(self) -> None:
            super().__init__()
            self.seen = 0

        def eventFilter(self, obj, event) -> bool:
            if event.type() == _QEvent.ToolTip:
                self.seen += 1
            return False

    def _tooltip_reaches_widget(widget) -> bool:
        spy = _ToolTipSpy()
        widget.installEventFilter(spy)
        ev = _QHelpEvent(_QEvent.ToolTip, _QPoint(5, 5),
                         widget.mapToGlobal(_QPoint(5, 5)))
        app.sendEvent(widget, ev)
        widget.removeEventFilter(spy)
        return spy.seen > 0

    tip_target = win.transport.loop_btn          # has a tooltip
    assert win._tooltips_enabled                 # default: tooltips on
    assert _tooltip_reaches_widget(tip_target)
    win._apply_tooltips_enabled(False)           # Settings -> Help unchecked
    assert not win._tooltips_enabled
    # The app-level suppressor swallows every ToolTip event (any window).
    ev = _QHelpEvent(_QEvent.ToolTip, _QPoint(5, 5),
                     tip_target.mapToGlobal(_QPoint(5, 5)))
    assert win._tooltip_suppressor.eventFilter(tip_target, ev)
    assert not _tooltip_reaches_widget(tip_target)
    assert not _tooltip_reaches_widget(win.track_panel)  # any window/widget
    win._apply_tooltips_enabled(True)            # back on
    assert _tooltip_reaches_widget(tip_target)
    # The dialog exposes the checkbox on its Help tab and reads QSettings.
    win.settings.setValue("help/tooltips", False)
    sdlg = _SD(win.bridge, win._menu_actions, win._default_shortcuts,
               win.settings, win)
    assert not sdlg.tooltips_check.isChecked()
    sdlg.tooltips_check.setChecked(True)
    assert sdlg.tooltips_enabled
    sdlg.deleteLater()
    win.settings.setValue("help/tooltips", True)
    app.processEvents()

    # -- Ctrl+J: join clips on one lane ---------------------------------------
    from pydaw.model.project import MidiClip as _MidiClip
    # Earlier Project.load calls rewind the global id counter to the
    # loaded project's max id; make sure fresh ids cannot collide with
    # tracks/clips still alive in this window.
    _max_id = max([t.id for t in win.project.tracks]
                  + [c.id for t in win.project.tracks for c in t.clips]
                  + [c.id for t in win.project.tracks for c in t.midi_clips]
                  + [0])
    bump_id_counter(_max_id + 1000)
    # Clear both selections: a selected clip or header now anchors where
    # new tracks are inserted, and this section assumes append-at-end.
    win.timeline._scene.clearSelection()
    win.track_panel.set_selected_tracks([])
    _ids0 = {t.id for t in win.project.tracks}
    win.add_midi_track()
    _jt = next(t for t in win.project.tracks if t.id not in _ids0)
    assert _jt.is_midi
    _jc1 = _MidiClip(id=next_id(), start_beat=0.0, length_beats=2.0,
                     notes=[MidiNote(id=next_id(), pitch=60,
                                     start_beat=0.0, length_beats=1.0)])
    _jc2 = _MidiClip(id=next_id(), start_beat=4.0, length_beats=2.0,
                     notes=[MidiNote(id=next_id(), pitch=62,
                                     start_beat=0.5, length_beats=1.0)])
    _jt.midi_clips.extend([_jc1, _jc2])
    win.bridge.add_midi_clip(win.project, _jt.id, _jc1)
    win.bridge.add_midi_clip(win.project, _jt.id, _jc2)
    win._refresh_all()
    # Clips from different lanes: rejected, nothing changes.
    win._on_clips_join([(_jt.id, _jc1.id, True),
                        (win.project.tracks[0].id, 12345, True)])
    assert len(_jt.midi_clips) == 2
    # Overlapping clips: rejected, nothing changes.
    _jc2.start_beat = 1.5
    win._on_clips_join([(_jt.id, _jc1.id, True), (_jt.id, _jc2.id, True)])
    assert len(_jt.midi_clips) == 2
    _jc2.start_beat = 4.0
    # Same lane with a gap: one clip spanning both, no notes appear in the gap.
    win._on_clips_join([(_jt.id, _jc1.id, True), (_jt.id, _jc2.id, True)])
    assert len(_jt.midi_clips) == 1
    _joined = _jt.midi_clips[0]
    assert _joined.start_beat == 0.0 and _joined.length_beats == 6.0
    assert sorted(n.pitch for n in _joined.notes) == [60, 62]
    assert all(not (2.0 <= n.start_beat < 4.0) for n in _joined.notes)
    _n62 = next(n for n in _joined.notes if n.pitch == 62)
    assert abs(_n62.start_beat - 4.5) < 1e-9
    win.undo_stack.undo()
    assert len(_jt.midi_clips) == 2
    win.undo_stack.redo()
    assert len(_jt.midi_clips) == 1

    # Adjacent MIDI clips (no gap): join must succeed; sub-frame slop allowed.
    win.undo_stack.undo()
    assert len(_jt.midi_clips) == 2
    _jc2.start_beat = _jc1.end_beat
    win._on_clips_join([(_jt.id, _jc1.id, True), (_jt.id, _jc2.id, True)])
    assert len(_jt.midi_clips) == 1
    _adj = _jt.midi_clips[0]
    assert _adj.start_beat == 0.0 and abs(_adj.length_beats - 4.0) < 1e-9
    win.undo_stack.undo()
    _jc2.start_beat = _jc1.end_beat - 0.4 / win.project.frames_per_beat
    win._on_clips_join([(_jt.id, _jc1.id, True), (_jt.id, _jc2.id, True)])
    assert len(_jt.midi_clips) == 1
    win.undo_stack.undo()
    assert len(_jt.midi_clips) == 2

    # Audio join: the gap is baked as silence into the rendered file.
    import tempfile as _jtf
    import numpy as _jnp
    import soundfile as _jsf
    win.timeline._scene.clearSelection()
    win.track_panel.set_selected_tracks([])
    _ids1 = {t.id for t in win.project.tracks}
    win.add_track()
    _at = next(t for t in win.project.tracks if t.id not in _ids1)
    _jwav = Path(_jtf.gettempdir()) / "pydaw_join_src.wav"
    _jsf.write(str(_jwav),
               _jnp.full((1000, 2), 0.5, dtype=_jnp.float32),
               win.bridge.sample_rate)
    _jfid, _jfr = win.bridge.load_file(str(_jwav))
    _ac1 = Clip(id=next_id(), file_path=str(_jwav), file_id=_jfid,
                start_frame=0, length=1000)
    _ac2 = Clip(id=next_id(), file_path=str(_jwav), file_id=_jfid,
                start_frame=3000, length=1000)
    _at.clips.extend([_ac1, _ac2])
    win.bridge.add_clip(_at.id, _ac1)
    win.bridge.add_clip(_at.id, _ac2)
    win._refresh_all()
    # Overlapping audio clips: rejected.
    _ac2.start_frame = 500
    win._on_clips_join([(_at.id, _ac1.id, False), (_at.id, _ac2.id, False)])
    assert len(_at.clips) == 2
    _ac2.start_frame = 3000
    win._on_clips_join([(_at.id, _ac1.id, False), (_at.id, _ac2.id, False)])
    assert len(_at.clips) == 1
    _aj = _at.clips[0]
    assert _aj.start_frame == 0 and _aj.length == 4000
    _jdata, _ = _jsf.read(_aj.file_path, dtype="float32", always_2d=True)
    assert _jdata.shape[0] == 4000
    assert _jnp.abs(_jdata[1500:2500]).max() == 0.0        # gap = silence
    assert _jnp.abs(_jdata[100] - 0.5).max() < 1e-6
    assert _jnp.abs(_jdata[3500] - 0.5).max() < 1e-6
    from pydaw.audio_stretch import NEW_CLIP_WARP as _NEW_CLIP_WARP
    assert _aj.warp == _NEW_CLIP_WARP
    assert abs(_aj.stretch_ratio - 1.0) < 1e-9
    assert _aj.auto_warp is False
    win.undo_stack.undo()
    assert len(_at.clips) == 2

    # Adjacent audio clips (no gap) and one-frame placement slop.
    _ac2.start_frame = _ac1.end_frame
    win._on_clips_join([(_at.id, _ac1.id, False), (_at.id, _ac2.id, False)])
    assert len(_at.clips) == 1
    win.undo_stack.undo()
    _ac2.start_frame = _ac1.end_frame - 1
    win._on_clips_join([(_at.id, _ac1.id, False), (_at.id, _ac2.id, False)])
    assert len(_at.clips) == 1
    win.undo_stack.undo()
    assert len(_at.clips) == 2
    win.undo_stack.redo()
    assert len(_at.clips) == 1

    # -- Ctrl+T/Ctrl+Shift+T insert below the selected clip's lane -----------
    win.timeline._scene.clearSelection()
    _jt_item = win.timeline._clip_items.get(_jc1.id)
    assert _jt_item is not None
    _jt_item.setSelected(True)
    # The selection must survive a full rebuild (every edit triggers one):
    # otherwise the anchor is silently lost before the hotkey fires.
    win._refresh_all()
    assert win.timeline._selected_clip_refs() == [(_jt.id, _jc1.id, True)]
    win.add_track()
    _tracks = win.project.tracks
    _new = _tracks[_tracks.index(_jt) + 1]
    assert _new.type == "audio" and _new.group_id == 0
    assert win.undo_stack._undo[-1].text == "Add track"
    # Anchor lane inside a group: the new track joins the same group.
    win.track_panel.set_selected_tracks([_jt.id])
    win.group_selected_tracks()
    _grp = next(t for t in win.project.tracks
                if t.is_group and _jt.group_id == t.id)
    win.timeline._scene.clearSelection()
    _jt_item = win.timeline._clip_items.get(_jc1.id)
    _jt_item.setSelected(True)
    win.add_midi_track()
    _tracks = win.project.tracks
    _new2 = _tracks[_tracks.index(_jt) + 1]
    assert _new2.type == "midi" and _new2.group_id == _grp.id
    # Selected ordinary track header (no clip selection): the new track
    # goes right below the selected track, inheriting its group.
    win.timeline._scene.clearSelection()
    win.track_panel.set_selected_tracks([_jt.id])   # _jt is inside _grp
    win.add_track()
    _tracks = win.project.tracks
    _below = _tracks[_tracks.index(_jt) + 1]
    assert _below.type == "audio" and _below.group_id == _grp.id
    _first_track = win.project.tracks[0]            # ungrouped lane
    win.track_panel.set_selected_tracks([_first_track.id])
    win.add_midi_track()
    _tracks = win.project.tracks
    _below2 = _tracks[_tracks.index(_first_track) + 1]
    assert _below2.type == "midi" and _below2.group_id == 0

    # A header click clears the clip selection: otherwise a stale clip
    # selection (clips now survive rebuilds) would shadow the header as
    # the insertion anchor (regression: group-header anchoring stopped
    # working after a clip had ever been selected).
    _jt_item = win.timeline._clip_items.get(_jc1.id)
    _jt_item.setSelected(True)
    win.track_panel._on_header_clicked(_grp.id)
    assert win.timeline._selected_clip_refs() == []
    win.add_track()
    _tracks = win.project.tracks
    _fc = _tracks[_tracks.index(_grp) + 1]
    assert _fc.type == "audio" and _fc.group_id == _grp.id

    # Selected group header (no clip selection): the new track becomes
    # the first child inside that group.
    win.timeline._scene.clearSelection()
    win.track_panel.set_selected_tracks([_grp.id])
    win.add_track()
    _tracks = win.project.tracks
    _first_child = _tracks[_tracks.index(_grp) + 1]
    assert _first_child.type == "audio" and _first_child.group_id == _grp.id
    # No selection: tracks append at the end, as before.
    win.timeline._scene.clearSelection()
    win.track_panel.set_selected_tracks([])
    win.add_track()
    assert win.project.tracks[-1].type == "audio"
    assert win.project.tracks[-1].group_id == 0
    # Right-click "Add ... track" menus ignore the anchor: even with a
    # clip selected they append at the very end (before the master row).
    _jt_item = win.timeline._clip_items.get(_jc1.id)
    _jt_item.setSelected(True)
    win.timeline.add_track_requested.emit()
    assert win.project.tracks[-1].type == "audio"
    assert win.project.tracks[-1].group_id == 0
    win.track_panel.add_midi_track_requested.emit()
    assert win.project.tracks[-1].type == "midi"
    assert win.project.tracks[-1].group_id == 0
    win.timeline._scene.clearSelection()

    # -- The app starts maximized --------------------------------------------
    import pydaw.app as _app_mod
    import inspect as _inspect
    assert "showMaximized()" in _inspect.getsource(_app_mod._run_once)

    # -- Soft restart: session stash + restore (closes the window) ----------
    from pydaw.ui.main_window import SESSION_RESTORE_FILE
    n_tracks = len(win.project.tracks)
    win._soft_restart()
    app.processEvents()
    assert win.restart_requested
    assert Path(SESSION_RESTORE_FILE).exists()
    win2 = MainWindow()
    win2.restore_session_if_pending()
    app.processEvents()
    assert len(win2.project.tracks) == n_tracks
    assert not Path(SESSION_RESTORE_FILE).exists()
    win2.close()
    app.processEvents()

    # -- Cold start always begins with a new project ------------------------
    # Simulate a soft restart that never came back: stale flag + stash file.
    QSettings("PyDAW", "PyDAW").setValue("session/restore", True)
    Path(SESSION_RESTORE_FILE).write_text("{}", encoding="utf-8")
    win3 = MainWindow()
    win3.discard_pending_session()
    app.processEvents()
    assert str(QSettings("PyDAW", "PyDAW").value("session/restore")).lower() \
        in ("false", "0")
    assert not Path(SESSION_RESTORE_FILE).exists()
    win3.close()
    app.processEvents()

    dlg.close()
    win.close()
    app.processEvents()
    print("GUI SMOKE TEST PASSED")
    return 0


def test_gui_smoke() -> None:
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
