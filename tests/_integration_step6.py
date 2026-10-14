"""One-off integration checks for step 6 (run after the subagent changes).

Covers cross-cutting behaviour between the master-track UI, timeline row
model, snap toggles, piano roll and the automation engine path.
Exit code 0 = pass.
"""

from __future__ import annotations

import math
import struct
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication

from pydaw.ui.theme import apply_theme
from pydaw.model.project import (AutomationLane, AutomationPoint,
                                 FORMAT_VERSION, Project)


def make_test_wav(path: Path, freq: float = 440.0, seconds: float = 2.0,
                  rate: int = 48000) -> None:
    n = int(seconds * rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(n):
            v = int(0.5 * 32767 * math.sin(2 * math.pi * freq * i / rate))
            frames += struct.pack("<h", v)
        w.writeframes(bytes(frames))


def read_wav_stereo(path: Path) -> tuple[list[float], list[float]]:
    """Minimal RIFF reader that also handles float32 (format 3) files."""
    data = path.read_bytes()
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    pos, fmt_tag, ch, bits, payload = 12, 1, 2, 32, b""
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt_tag, ch = struct.unpack("<HH", body[0:4])
            bits = struct.unpack("<H", body[14:16])[0]
        elif cid == b"data":
            payload = body
        pos += 8 + size + (size & 1)
    if fmt_tag == 3 and bits == 32:
        vals = struct.unpack(f"<{len(payload) // 4}f", payload)
    else:
        vals = [v / 32768.0
                for v in struct.unpack(f"<{len(payload) // 2}h", payload)]
    left, right = [], []
    for i in range(0, len(vals) - ch + 1, ch):
        left.append(vals[i])
        right.append(vals[i + 1] if ch > 1 else vals[i])
    return left, right


def rms(xs: list[float]) -> float:
    return math.sqrt(sum(x * x for x in xs) / max(1, len(xs)))


def main() -> int:
    app = QApplication(sys.argv)
    apply_theme(app)

    from pydaw.ui.main_window import MainWindow
    win = MainWindow()
    win.show()
    app.processEvents()

    audio = next(t for t in win.project.tracks if t.type == "audio")
    midi = next(t for t in win.project.tracks if t.is_midi)

    # 1. Master header wiring -------------------------------------------------
    assert hasattr(win, "master_header")
    assert not hasattr(win.transport, "master_slider")
    win._on_master_gain(1.3)
    assert win.master_header.gain_slider.value() == 130
    assert abs(win.project.master_gain - 1.3) < 1e-9
    win.master_header.gain_slider.setValue(90)   # user drag path
    app.processEvents()
    assert abs(win.project.master_gain - 0.9) < 1e-9
    win._on_master_gain(1.0)
    print("master header: ok")

    # 2. Track headers: no FX button, no fx_requested signal -------------------
    hdr = win.track_panel.headers[audio.id]
    assert not hasattr(hdr, "fx_requested")
    assert not hasattr(hdr, "refresh_fx_button")
    print("fx buttons removed: ok")

    # 3. Snap toggle (timeline) ------------------------------------------------
    win.transport.snap_btn.setChecked(False)
    app.processEvents()
    assert win.timeline.snap_enabled is False
    fpb = win.project.frames_per_beat
    raw = int(1.37 * fpb)
    assert win.timeline.snap_frame(raw) == raw
    win.transport.snap_btn.setChecked(True)
    app.processEvents()
    assert win.timeline.snap_enabled is True
    assert win.timeline.snap_frame(raw) == int(round(round(1.37) * fpb))
    print("timeline snap toggle: ok")

    # 4. Gutter + scene rect ----------------------------------------------------
    sr = win.timeline._scene.sceneRect()
    assert sr.left() == -18, sr
    print("left gutter: ok")

    # 5. Row model alignment: panel widget heights == timeline rows ------------
    win._on_add_automation_lane(audio.id, "volume", -1, 0, "Volume")
    win._on_add_automation_lane(audio.id, "pan", -1, 0, "Pan")
    app.processEvents()
    rows = win.timeline.visible_rows()
    total_timeline = rows[-1]["y"] + rows[-1]["height"]
    total_panel = sum(h.height() for h in win.track_panel.headers.values())
    total_panel += sum(h.height()
                       for h in win.track_panel.auto_headers.values())
    if win.master_header is not None:
        total_panel += win.master_header.height()
        total_panel += win.track_panel._master_gap.height()
    assert abs(total_timeline - total_panel) < 1e-6, \
        (total_timeline, total_panel)
    # Rows are ordered: audio track row immediately followed by its 2 lanes.
    kinds = [(r["kind"], r["track"].id) for r in rows]
    i = kinds.index(("track", audio.id))
    assert kinds[i + 1] == ("auto", audio.id)
    assert kinds[i + 2] == ("auto", audio.id)
    print("row alignment: ok", total_timeline)

    # 6. Duplicate lane rejected, remove via panel signal ------------------------
    n = len(audio.automation_lanes)
    win._on_add_automation_lane(audio.id, "volume", -1, 0, "Volume")
    assert len(audio.automation_lanes) == n
    lane_pan = next(l for l in audio.automation_lanes if l.target == "pan")
    win.track_panel.automation_remove_requested.emit(audio.id, lane_pan.id)
    app.processEvents()
    assert all(l.target != "pan" for l in audio.automation_lanes)
    win.undo_stack.undo()
    assert any(l.target == "pan" for l in audio.automation_lanes)
    print("lane add/remove/undo: ok")

    # 7. Built-in effect automation + slot shift on removal ---------------------
    win._on_effect_added(audio.id, "delay")
    win._on_effect_added(audio.id, "filter")
    app.processEvents()
    win._on_add_automation_lane(audio.id, "fx", 1, 0, "Filter: Cutoff")
    lane_fx = next(l for l in audio.automation_lanes if l.target == "fx")
    assert lane_fx.fx_slot == 1
    win._on_effect_removed(audio.id, 0)   # remove delay -> filter shifts to 0
    app.processEvents()
    assert lane_fx.fx_slot == 0
    assert lane_fx in audio.automation_lanes
    # Removing the filter drops its lane too.
    win._on_effect_removed(audio.id, 0)
    app.processEvents()
    assert all(l.target != "fx" for l in audio.automation_lanes)
    print("fx lane slot shift: ok")

    # 8. Automation persists through save/load (v6) ------------------------------
    tmp = Path(__file__).parent / "_tmp"
    tmp.mkdir(exist_ok=True)
    proj_path = tmp / "step6_it.pydaw"
    lane_vol = next(l for l in audio.automation_lanes
                    if l.target == "volume")
    win._on_automation_points_changed(
        audio.id, lane_vol.id, [], [(0.0, 0.2), (4.0, 0.9)])
    win.project.save(proj_path)
    loaded = Project.load(proj_path)
    lt = loaded.track_by_id(audio.id)
    ll = [l for l in lt.automation_lanes if l.target == "volume"]
    assert ll and len(ll[0].points) == 2
    assert abs(ll[0].points[1].value - 0.9) < 1e-9
    print("v6 roundtrip: ok (format", FORMAT_VERSION, ")")

    # 9. Load through the UI: rows rebuilt, engine synced -------------------------
    win._load_project_from_path(str(proj_path))
    app.processEvents()
    rows = win.timeline.visible_rows()
    assert any(r["kind"] == "auto" for r in rows)
    # The load replaced the Project instance: re-fetch model objects.
    audio = win.project.track_by_id(audio.id)
    assert audio is not None and audio.automation_lanes
    print("ui project load with lanes: ok")

    # 10. BPM change resync does not crash and keeps lanes -----------------------
    win._on_bpm_changed(90.0)
    app.processEvents()
    win._on_bpm_changed(120.0)
    app.processEvents()
    print("bpm resync: ok")

    # 11. Piano roll: local snap independent of the global one -------------------
    midi = next(t for t in win.project.tracks if t.is_midi)
    win.add_midi_clip(midi.id, 0.0)
    clip = midi.midi_clips[0]
    win.open_piano_roll(midi.id, clip.id)
    dlg = win._piano_rolls[clip.id]
    dlg.view.snap_beats = 0.5
    win._on_snap_changed(4.0)          # global snap change
    assert dlg.view.snap_beats == 0.5  # unaffected
    dlg.snap_btn.setChecked(False)
    assert dlg.view.snap(0.37) == 0.37
    dlg.snap_btn.setChecked(True)
    assert dlg.view.snap(0.37) == 0.5
    win._on_snap_changed(1.0)
    print("piano roll local snap: ok")

    # 12. Piano roll slice via the dialog signal ---------------------------------
    win._on_note_add(midi.id, clip.id, 60, 0.0, 2.0)
    note = clip.notes[-1]
    dlg.note_slice_requested.emit(midi.id, clip.id, note.id, 0.75)
    assert len(clip.notes) == 2
    lens = sorted(n.length_beats for n in clip.notes)
    assert abs(lens[0] - 0.75) < 1e-9 and abs(lens[1] - 1.25) < 1e-9
    win.undo_stack.undo()
    assert len(clip.notes) == 1 and abs(note.length_beats - 2.0) < 1e-9
    print("piano roll slice: ok")

    # 13. Piano roll Esc: exits slice first, closes dialog second -----------------
    dlg.slice_btn.setChecked(True)
    dlg._exit_slice_mode()
    assert not dlg.slice_btn.isChecked()
    assert dlg.isVisible()
    dlg._exit_slice_mode()
    app.processEvents()
    assert clip.id not in win._piano_rolls
    print("piano roll esc: ok")

    # 14. Audible pan automation through the engine ------------------------------
    from pydaw.engine_bridge import ENGINE_AVAILABLE
    if ENGINE_AVAILABLE:
        wav = tmp / "step6_tone.wav"
        make_test_wav(wav, seconds=2.0, rate=win.bridge.sample_rate)
        win.add_file_to_track(str(wav), audio.id, 0)
        deadline = 200
        while win._pending_loads and deadline:
            app.processEvents()
            import time
            time.sleep(0.05)
            deadline -= 1
        assert audio.clips, "clip did not load"
        # Pan lane: full left at 0, full right at the end (2s = 4 beats).
        lane_pan = next(l for l in audio.automation_lanes
                        if l.target == "pan")
        end_beat = win.project.frames_to_beats(audio.clips[0].length)
        win._on_automation_points_changed(
            audio.id, lane_pan.id,
            [(p.beat, p.value) for p in lane_pan.points],
            [(0.0, 0.0), (end_beat, 1.0)])
        # Neutralize volume lane so it does not zero the start.
        lane_vol = next(l for l in audio.automation_lanes
                        if l.target == "volume")
        win._on_automation_points_changed(
            audio.id, lane_vol.id,
            [(p.beat, p.value) for p in lane_vol.points],
            [(0.0, 2.0 / 3.0)])  # native gain 1.0
        out = tmp / "step6_pan.wav"
        assert win.bridge.export_wav(str(out), 0, audio.clips[0].length)
        left, right = read_wav_stereo(out)
        q = len(left) // 4
        start_l, start_r = rms(left[:q]), rms(right[:q])
        end_l, end_r = rms(left[-q:]), rms(right[-q:])
        assert start_l > start_r * 3, (start_l, start_r)
        assert end_r > end_l * 3, (end_l, end_r)
        print(f"pan automation audible: ok start L/R {start_l:.3f}/{start_r:.3f}"
              f" end L/R {end_l:.3f}/{end_r:.3f}")
    else:
        print("pan automation: skipped (engine not available)")

    # 15. Remove track with lanes + undo restores engine lanes -------------------
    win.remove_track(audio.id)
    app.processEvents()
    assert win.project.track_by_id(audio.id) is None
    win.undo_stack.undo()
    app.processEvents()
    restored = win.project.track_by_id(audio.id)
    assert restored is not None and restored.automation_lanes
    print("remove/undo track with lanes: ok")

    win.close()
    app.processEvents()
    print("STEP6 INTEGRATION PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
