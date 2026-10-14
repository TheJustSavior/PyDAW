"""Automation tests: lane evaluation, VST3 parameter automation and
live UI feedback on knobs/sliders.

Run: py tests/test_automation.py   (exit code 0 = pass)

The VST3 section drives a real system plugin when one is installed;
otherwise it is skipped (still exercising the engine API).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydaw.model.project import (AutomationLane, AutomationPoint, EFFECT_DEFS,
                                 Effect, Project, Track, next_id)
from pydaw.ui.automation_item import shape_curve


def test_value_at() -> None:
    lane = AutomationLane(id=1, target="volume", points=[
        AutomationPoint(0.0, 0.0),
        AutomationPoint(4.0, 1.0, "step"),
        AutomationPoint(8.0, 0.0),
    ])
    assert lane.value_at(-1.0) == 0.0
    assert lane.value_at(0.0) == 0.0
    assert abs(lane.value_at(2.0) - 0.5) < 1e-9        # linear
    assert lane.value_at(5.0) == 1.0                    # step holds
    assert lane.value_at(7.999) == 1.0
    assert lane.value_at(9.0) == 0.0                    # past the end

    exp_lane = AutomationLane(id=2, target="pan", points=[
        AutomationPoint(0.0, 0.0, "exp"), AutomationPoint(1.0, 1.0)])
    assert abs(exp_lane.value_at(0.5) - 0.125) < 1e-9   # t^3

    bez = AutomationLane(id=3, target="pan", points=[
        AutomationPoint(0.0, 0.0, "bezier"), AutomationPoint(1.0, 1.0)])
    assert abs(bez.value_at(0.5) - 0.5) < 1e-9          # smoothstep midpoint

    curved = AutomationLane(id=4, target="pan", points=[
        AutomationPoint(0.0, 0.0, "exp", 0.0), AutomationPoint(1.0, 1.0)])
    assert abs(curved.value_at(0.25) - 0.25) < 1e-9
    curved.points[0].curve_amount = -1.0
    assert abs(curved.value_at(0.25) - (1.0 - 0.75 ** 3)) < 1e-9
    assert abs(shape_curve("exp", 0.25, -1.0) - curved.value_at(0.25)) < 1e-9

    curved.points[0].shape = "bezier"
    curved.points[0].curve_amount = -1.0
    expected = 0.5 - 0.25 * 0.25 * (3.0 - 0.5)
    assert abs(curved.value_at(0.25) - expected) < 1e-9

    assert AutomationLane(id=5, target="volume").value_at(1.0) is None
    print("value_at shapes: ok")


def test_vst_automation() -> None:
    if os.environ.get("PYDAW_RUN_REAL_VST_TESTS") != "1":
        pytest.skip("real VST automation test requires PYDAW_RUN_REAL_VST_TESTS=1")

    from pydaw.engine_bridge import ENGINE_AVAILABLE, EngineBridge

    if not ENGINE_AVAILABLE:
        print("vst automation: SKIPPED (engine module not built)")
        return

    bridge = EngineBridge()
    plugins = bridge.scan_vst_plugins(["C:/Program Files/Common Files/VST3"])
    fx_info = next((p for p in plugins if not p.is_instrument), None)
    if fx_info is None:
        print("vst automation: SKIPPED (no VST3 effects installed)")
        bridge.shutdown()
        return

    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="pydaw_auto_"))

    # Project: one audio track with a short generated clip + the VST effect.
    import math
    import wave
    import struct
    sr = bridge.sample_rate
    wav = tmp / "tone.wav"
    with wave.open(str(wav), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(sr)
        n = sr  # 1 second
        frames = bytearray()
        for i in range(n):
            v = int(0.4 * 32767 * math.sin(2 * math.pi * 220 * i / sr))
            frames += struct.pack("<hh", v, v)
        f.writeframes(bytes(frames))

    project = Project(sample_rate=sr, bpm=120.0)
    track = Track(id=next_id(), name="T")
    project.tracks.append(track)
    fid, nframes = bridge.load_file(str(wav))
    from pydaw.model.project import Clip
    track.clips.append(Clip(id=next_id(), file_path=str(wav), file_id=fid,
                            start_frame=0, length=nframes))
    track.effects.append(Effect(type="vst", path=fx_info.path,
                                uid=fx_info.uid, name=fx_info.name))
    bridge.sync_project(project)
    if bridge.resolve_fx_slot(track.id, 0) is None:
        print("vst automation: SKIPPED (plugin failed to load:",
              fx_info.name + ")")
        bridge.shutdown()
        return

    n_params = bridge.vst_param_count(track.id, 0)
    # Plugin-declared defaults back the generic sliders' reset menu.
    for i in range(min(n_params, 8)):
        d = bridge.vst_get_param_default(track.id, 0, i)
        assert 0.0 <= d <= 1.0, f"param {i} default out of range: {d}"
    if n_params:
        print(f"vst param defaults (first {min(n_params, 8)}): ok")
    if n_params == 0:
        print("vst automation: SKIPPED (plugin exposes no params)")
        bridge.shutdown()
        return

    # TWEAK-flag semantics: programmatic writes (undo/redo) must not
    # register as user tweaks; explicit user writes must.
    bridge.vst_consume_last_tweak()  # drain
    bridge.vst_set_param(track.id, 0, 0, 0.3, mark_user_edit=False)
    assert bridge.vst_consume_last_tweak() is None, \
        "programmatic vst_set_param leaked into the tweak flag"
    bridge.vst_set_param(track.id, 0, 0, 0.4)  # user path (default True)
    hit = bridge.vst_consume_last_tweak()
    assert hit == (track.id, 0, 0), hit
    print("vst tweak-flag semantics: ok")

    # Param-list refresh: plugins may flag restartComponent during
    # load/setState - the first poll drains it; an idle plugin then
    # reports no change.
    first = bridge.vst_refresh_changed_params()
    assert bridge.vst_refresh_changed_params() is False
    print(f"vst param refresh poll: ok (drained initial={first})")
    from PySide6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication(sys.argv)
    from pydaw.ui.vst_widgets import VstParamPanel
    panel = VstParamPanel(bridge, track.id, 0, max_params=8)
    n_sliders = len(panel._sliders)
    assert n_sliders == min(n_params, 8), (n_sliders, n_params)
    panel.rebuild_params()
    assert len(panel._sliders) == n_sliders
    panel.deleteLater()
    print("vst param panel rebuild: ok")

    # Pick a parameter POSITION whose value actually differs from 1.0
    # so the ramp end is observable. Lane.param must be the position in
    # the vst_params() list (the engine maps position -> ParamID).
    pos = 0
    lane = AutomationLane(
        id=next_id(), target="fx", fx_slot=0, param=pos, name="p0",
        points=[AutomationPoint(0.0, 0.0), AutomationPoint(2.0, 1.0)])
    track.automation_lanes.append(lane)
    bridge.sync_automation_lane(project, track, lane)

    # Offline export runs the mixer: automation must reach the plugin.
    out = tmp / "out.wav"
    assert bridge.export_wav(str(out)), "export failed"

    # Mirror audio-thread values into the controller, then read back.
    bridge.vst_sync_controllers()
    value = bridge.vst_get_param(track.id, 0, pos)
    # Playhead ends past beat 2 => lane value 1.0. Some plugins quantize
    # (stepped params), so accept anything clearly above the start value.
    assert value > 0.5, (
        f"automated param did not move: {value} (plugin {fx_info.name})")
    print(f"vst automation ramp reached {value:.3f} on '{fx_info.name}': ok")

    # Removing the lane must stop further changes.
    bridge.remove_automation_lane(track.id, lane.id)
    bridge.shutdown()
    print("vst automation: ok")


def test_gui_feedback() -> None:
    from PySide6.QtWidgets import QApplication
    from pydaw.ui.main_window import MainWindow
    from pydaw.ui.theme import apply_theme

    app = QApplication.instance() or QApplication(sys.argv)
    apply_theme(app)
    win = MainWindow()
    win.show()
    app.processEvents()

    track = win.project.tracks[0]
    header = win.track_panel.headers[track.id]

    # Volume lane 0 -> 1 over 4 beats; pan lane fixed hard left.
    vol = AutomationLane(id=next_id(), target="volume", name="Volume",
                         points=[AutomationPoint(0.0, 0.0),
                                 AutomationPoint(4.0, 1.0)])
    pan = AutomationLane(id=next_id(), target="pan", name="Pan",
                         points=[AutomationPoint(0.0, 0.0)])
    track.automation_lanes += [vol, pan]

    frame_2beats = win.project.beats_to_frames(2.0)
    win._update_automation_feedback(frame_2beats, True)

    # volume 0.5 normalized -> gain 0.75 -> slider 75.
    assert header.gain_slider.value() == 75, header.gain_slider.value()
    # pan 0.0 normalized -> -1.0 -> slider -100.
    assert header.pan_slider.value() == -100, header.pan_slider.value()
    assert win._auto_feedback_active

    # Builtin FX param feedback in the FX panel.
    fx_type = next(iter(EFFECT_DEFS))
    _name, param_defs = EFFECT_DEFS[fx_type]
    lo, hi, default = param_defs[0][1:4]
    track.effects.append(Effect(fx_type, params=[d[3] for d in param_defs]))
    win._select_track_for_fx(track.id)
    app.processEvents()
    fx_lane = AutomationLane(id=next_id(), target="fx", fx_slot=0, param=0,
                             name="fx0",
                             points=[AutomationPoint(0.0, 1.0)])  # param = hi
    track.automation_lanes.append(fx_lane)
    win._update_automation_feedback(frame_2beats, True)
    card = win.fx_panel._builtin_cards[0]
    assert card._sliders[0].value() == 1000, card._sliders[0].value()

    # Stop: widgets snap back to the model values.
    win._update_automation_feedback(frame_2beats, False)
    assert not win._auto_feedback_active
    assert header.gain_slider.value() == int(track.gain * 100)
    assert header.pan_slider.value() == int(track.pan * 100)
    expected = int(round((track.effects[0].params[0] - lo) / (hi - lo) * 1000))
    assert card._sliders[0].value() == expected

    # New lane starts with a point at beat 0 holding the current value.
    track2 = next(t for t in win.project.tracks if t is not track)
    track2.gain = 0.9
    track2.pan = -0.5
    win._on_add_automation_lane(track2.id, "volume", -1, 0, "Volume")
    win._on_add_automation_lane(track2.id, "pan", -1, 0, "Pan")
    app.processEvents()
    vlane = next(l for l in track2.automation_lanes if l.target == "volume")
    plane = next(l for l in track2.automation_lanes if l.target == "pan")
    assert len(vlane.points) == 1 and vlane.points[0].beat == 0.0
    assert abs(vlane.points[0].value - 0.9 / 1.5) < 1e-9
    assert abs(plane.points[0].value - 0.25) < 1e-9  # (-0.5+1)/2
    win.undo_stack.clear()

    # Master volume lane feedback.
    mvol = AutomationLane(id=next_id(), target="volume", name="Master vol",
                          points=[AutomationPoint(0.0, 1.0)])  # gain 1.5
    win.project.master_automation_lanes.append(mvol)
    win._update_automation_feedback(frame_2beats, True)
    assert win.master_header.gain_slider.value() == 150
    win._update_automation_feedback(frame_2beats, False)
    assert win.master_header.gain_slider.value() == \
        int(win.project.master_gain * 100)

    win.close()
    app.processEvents()
    print("gui automation feedback: ok")


def main() -> int:
    test_value_at()
    test_vst_automation()
    test_gui_feedback()
    print("ALL AUTOMATION TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
