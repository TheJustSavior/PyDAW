from __future__ import annotations

import json
from pathlib import Path

import pytest

from pydaw.model.project import (AutomationLane, AutomationPoint, Clip, Effect,
                                 FORMAT_VERSION, MidiClip, MidiNote, Project,
                                 Track)


def test_project_v8_full_roundtrip(tmp_path: Path) -> None:
    sample = tmp_path / "samples" / "clip.wav"
    sample.parent.mkdir()
    sample.write_bytes(b"fake")

    project = Project(sample_rate=48000, bpm=123.0)
    project.loop_enabled = True
    project.loop_start_beat = 1.0
    project.loop_end_beat = 5.0

    group = Track(id=1, name="Bus", type="group", gain=0.75)
    audio = Track(id=2, name="Audio", group_id=1, stereo_width=0.5)
    audio.clips.append(Clip(id=3, file_path=str(sample), file_id=42,
                            start_frame=128, length=2048,
                            source_offset=64, fade_in=32, fade_out=48,
                            warp="stretch_hd", stretch_ratio=1.5,
                            auto_warp=False))
    audio.effects.append(Effect(type="vst", name="Fx", path="fx.vst3",
                                uid="fxuid", state=b"fx-state"))

    midi = Track(id=4, name="Midi", type="midi")
    midi.instrument.vst_path = "inst.vst3"
    midi.instrument.vst_uid = "instuid"
    midi.instrument.vst_name = "Inst"
    midi.instrument.vst_state = b"inst-state"
    mclip = MidiClip(id=5, name="Part", start_beat=2.0,
                     length_beats=4.0, source_start_beat=0.5,
                     fade_in_beats=0.25, fade_out_beats=0.5)
    mclip.notes.append(MidiNote(id=6, pitch=64, start_beat=0.75,
                                length_beats=1.25, velocity=0.8))
    midi.midi_clips.append(mclip)

    project.tracks.extend([group, audio, midi])
    project.master_effects.append(Effect(type="vst", name="Master",
                                         path="master.vst3", uid="muid",
                                         state=b"master-state"))
    project.master_automation_lanes.append(
        AutomationLane(id=7, target="master_volume", points=[
            AutomationPoint(0.0, 0.5, "linear"),
            AutomationPoint(1.0, 0.8, "bezier", -0.4),
        ]))

    path = tmp_path / "song.pydaw"
    project.save(path)
    loaded = Project.load(path)

    assert loaded.sample_rate == 48000
    assert loaded.bpm == 123.0
    assert loaded.loop_enabled
    assert len(loaded.tracks) == 3
    loaded_audio = loaded.track_by_id(2)
    assert loaded_audio is not None
    assert loaded_audio.group_id == 1
    assert loaded_audio.stereo_width == 0.5
    loaded_clip = loaded_audio.clips[0]
    assert loaded_clip.file_path == str(sample)
    assert loaded_clip.file_id == 0
    assert loaded_clip.warp == "stretch_hd"
    assert loaded_clip.stretch_ratio == 1.5
    assert loaded_clip.auto_warp is False
    assert loaded_audio.effects[0].state == b"fx-state"

    loaded_midi = loaded.track_by_id(4)
    assert loaded_midi is not None
    assert loaded_midi.instrument.vst_state == b"inst-state"
    assert loaded_midi.midi_clips[0].source_start_beat == 0.5
    assert loaded_midi.midi_clips[0].fade_out_beats == 0.5
    assert loaded_midi.midi_clips[0].notes[0].velocity == 0.8
    assert loaded.master_effects[0].state == b"master-state"
    assert loaded.master_automation_lanes[0].points[1].shape == "bezier"
    assert loaded.master_automation_lanes[0].points[1].curve_amount == -0.4


def test_automation_curve_amount_defaults_and_clamps() -> None:
    assert AutomationPoint.from_dict({}).curve_amount == 1.0
    assert AutomationPoint.from_dict({"curve_amount": 4.0}).curve_amount == 1.0
    assert AutomationPoint.from_dict({"curve_amount": -4.0}).curve_amount == -1.0


def test_save_stores_paths_relative_to_project_folder(tmp_path: Path) -> None:
    sample = tmp_path / "samples" / "rel.wav"
    sample.parent.mkdir()
    sample.write_bytes(b"fake")
    project = Project()
    track = Track(id=1, name="Audio")
    track.clips.append(Clip(id=2, file_path=str(sample), file_id=1,
                            start_frame=0, length=1))
    project.tracks.append(track)

    path = tmp_path / "relative.pydaw"
    project.save(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["tracks"][0]["clips"][0]["file_path"] == "samples/rel.wav"
    assert Project.load(path).tracks[0].clips[0].file_path == str(sample)


def test_legacy_channel_mode_migrates_to_stereo_width(tmp_path: Path) -> None:
    path = tmp_path / "legacy.pydaw"
    path.write_text(json.dumps({
        "format": "pydaw-project",
        "version": FORMAT_VERSION,
        "sample_rate": 48000,
        "bpm": 120.0,
        "tracks": [{"id": 1, "name": "Old", "channel_mode": "mono"}],
    }), encoding="utf-8")
    loaded = Project.load(path)
    assert loaded.tracks[0].stereo_width == 0.0


def test_load_rejects_non_pydaw_format(tmp_path: Path) -> None:
    path = tmp_path / "bad.pydaw"
    path.write_text(json.dumps({"format": "other"}), encoding="utf-8")
    with pytest.raises(ValueError):
        Project.load(path)
