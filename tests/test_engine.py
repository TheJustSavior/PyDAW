"""End-to-end engine test without GUI: load WAV, add clips, export mix."""

from __future__ import annotations

import math
import struct
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydaw.engine_bridge import EngineBridge, ENGINE_AVAILABLE
from pydaw.model.project import (AutomationLane, AutomationPoint, Clip, Effect,
                                 FORMAT_VERSION, MidiClip, MidiNote, Project,
                                 Track, next_id)


def make_test_wav(path: Path, freq: float = 440.0, seconds: float = 1.0,
                  rate: int = 44100) -> None:
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


def main() -> int:
    assert ENGINE_AVAILABLE, "pydaw_engine module is not built"

    tmp = Path(__file__).parent / "_tmp"
    tmp.mkdir(exist_ok=True)
    wav_a = tmp / "tone_a.wav"
    wav_b = tmp / "tone_b.wav"
    make_test_wav(wav_a, freq=440.0)
    make_test_wav(wav_b, freq=660.0)

    bridge = EngineBridge(48000)
    print("audio device:", "ok" if bridge.audio_ok else "none (offline mode)")
    sr = bridge.sample_rate
    print("sample rate:", sr)

    # Files: loaded and resampled 44100 -> 48000.
    fid_a, frames_a = bridge.load_file(str(wav_a))
    fid_b, frames_b = bridge.load_file(str(wav_b))
    print("file A:", fid_a, frames_a, "frames")
    print("file B:", fid_b, frames_b, "frames")
    assert abs(frames_a - sr) < 100, "1s file should be ~sr frames after resample"

    # Peaks for waveform drawing.
    peaks = bridge.file_peaks(fid_a, 512)
    assert len(peaks) > 0 and max(peaks) > 0.4, "peaks should reflect amplitude"
    print("peaks:", len(peaks) // 2, "blocks, max", round(max(peaks), 3))

    # Project: 2 tracks, second clip offset by 0.5 s.
    project = Project(sample_rate=sr)
    t1 = Track(id=next_id(), name="A")
    t2 = Track(id=next_id(), name="B", pan=0.5, gain=0.8)
    project.tracks += [t1, t2]
    t1.clips.append(Clip(id=next_id(), file_path=str(wav_a), file_id=fid_a,
                         start_frame=0, length=frames_a))
    t2.clips.append(Clip(id=next_id(), file_path=str(wav_b), file_id=fid_b,
                         start_frame=sr // 2, length=frames_b))
    bridge.sync_project(project)

    # Save / load roundtrip.
    proj_path = tmp / "test.pydaw"
    project.save(proj_path)
    loaded = Project.load(proj_path)
    assert len(loaded.tracks) == 2
    assert loaded.tracks[1].clips[0].start_frame == sr // 2
    print("project save/load: ok")

    # Offline export.
    out = tmp / "mix.wav"
    ok = bridge.export_wav(str(out))
    assert ok, "export failed"
    # Float32 WAV is not supported by the stdlib wave module;
    # verify by loading the exported file back through the engine.
    fid_mix, out_frames = bridge.load_file(str(out))
    print("exported:", out_frames, "frames")
    expected = sr // 2 + frames_b
    assert abs(out_frames - expected) < 10, \
        f"mix length {out_frames} != expected {expected}"
    mix_peaks = bridge.file_peaks(fid_mix, 512)
    assert max(mix_peaks) > 0.3, "exported mix should contain audio"

    # -- MIDI: track + clip + notes rendered by the built-in synth ---------
    midi_project = Project(sample_rate=sr, bpm=120.0)
    mt = Track(id=next_id(), name="Synth", type="midi")
    mclip = MidiClip(id=next_id(), start_beat=0.0, length_beats=4.0)
    mclip.notes.append(MidiNote(id=next_id(), pitch=60, start_beat=0.0,
                                length_beats=1.0))
    mclip.notes.append(MidiNote(id=next_id(), pitch=64, start_beat=1.0,
                                length_beats=1.0))
    mclip.notes.append(MidiNote(id=next_id(), pitch=67, start_beat=2.0,
                                length_beats=2.0))
    mt.midi_clips.append(mclip)
    midi_project.tracks.append(mt)
    bridge.sync_project(midi_project)

    midi_out = tmp / "midi_mix.wav"
    ok = bridge.export_wav(str(midi_out))
    assert ok, "midi export failed"
    fid_midi, midi_frames = bridge.load_file(str(midi_out))
    # 4 beats at 120 bpm = 2 seconds.
    expected_midi = midi_project.beats_to_frames(4.0)
    assert abs(midi_frames - expected_midi) < 10, \
        f"midi mix length {midi_frames} != expected {expected_midi}"
    midi_peaks = bridge.file_peaks(fid_midi, 512)
    assert max(midi_peaks) > 0.1, "synth should produce audible output"
    print("midi export:", midi_frames, "frames, peak",
          round(max(midi_peaks), 3))

    # -- BPM change moves MIDI clips in frames -----------------------------
    midi_project.bpm = 60.0
    bridge.sync_midi(midi_project)
    midi_out2 = tmp / "midi_mix_60bpm.wav"  # new name: file cache is path-keyed
    ok = bridge.export_wav(str(midi_out2))
    assert ok
    fid_midi2, midi_frames2 = bridge.load_file(str(midi_out2))
    expected_slow = midi_project.beats_to_frames(4.0)  # 4 beats at 60 = 4 s
    assert abs(midi_frames2 - expected_slow) < 10, \
        f"after bpm change: {midi_frames2} != {expected_slow}"
    print("bpm change: ok,", midi_frames2, "frames at 60 bpm")

    # -- effects: adding a delay extends nothing but must not crash --------
    assert bridge.add_effect(mt.id, "delay"), "delay effect not created"
    assert bridge.add_effect(mt.id, "filter"), "filter effect not created"
    assert not bridge.add_effect(mt.id, "nonexistent"), \
        "unknown effect type must be rejected"
    mt.effects.append(Effect("delay"))
    mt.effects.append(Effect("filter"))
    bridge.set_effect_param(mt.id, 0, 2, 0.5)  # delay mix
    bridge.set_effect_param(mt.id, 1, 0, 800.0)  # filter cutoff
    ok = bridge.export_wav(str(midi_out))
    assert ok, "export with effects failed"
    print("effects: ok")

    # -- loop region: engine command + v3 serialization ---------------------
    midi_project.loop_start_beat = 1.0
    midi_project.loop_end_beat = 3.0
    midi_project.loop_enabled = True
    bridge.sync_loop(midi_project)   # must not crash / block
    bridge.seek(0)
    ok = bridge.export_wav(str(midi_out))  # offline export ignores the loop
    assert ok, "export with loop enabled failed"
    print("loop command: ok")

    # -- v3 serialization roundtrip with MIDI + loop ------------------------
    midi_path = tmp / "midi.pydaw"
    midi_project.save(midi_path)
    loaded2 = Project.load(midi_path)
    assert loaded2.bpm == 60.0
    assert loaded2.tracks[0].is_midi
    assert len(loaded2.tracks[0].midi_clips[0].notes) == 3
    assert loaded2.tracks[0].midi_clips[0].notes[2].pitch == 67
    assert len(loaded2.tracks[0].effects) == 2
    assert loaded2.loop_enabled and loaded2.loop_start_beat == 1.0 \
        and loaded2.loop_end_beat == 3.0
    print("midi project save/load: ok (with loop region)")

    # -- track groups -------------------------------------------------------
    group_export_n = 0

    def export_peak() -> float:
        nonlocal group_export_n
        group_export_n += 1
        p = tmp / f"group_probe_{group_export_n}.wav"
        assert bridge.export_wav(str(p))
        fid, _ = bridge.load_file(str(p))
        return max(bridge.file_peaks(fid, 512))

    # Baseline: single audio track, no group.
    direct = Project(sample_rate=sr)
    dt = Track(id=next_id(), name="Direct")
    direct.tracks.append(dt)
    dt.clips.append(Clip(id=next_id(), file_path=str(wav_a), file_id=fid_a,
                        start_frame=0, length=frames_a))
    bridge.sync_project(direct)
    peak_direct = export_peak()
    assert peak_direct > 0.3, "direct export should have audio"
    print("group baseline peak:", round(peak_direct, 3))

    gid = next_id()
    cid = next_id()
    grouped = Project(sample_rate=sr)
    gt = Track(id=gid, name="Group", type="group", gain=1.0)
    ct = Track(id=cid, name="Child", group_id=gid)
    grouped.tracks += [gt, ct]
    ct.clips.append(Clip(id=next_id(), file_path=str(wav_a), file_id=fid_a,
                         start_frame=0, length=frames_a))
    bridge.sync_project(grouped)
    peak_group_1 = export_peak()
    assert abs(peak_group_1 - peak_direct) / peak_direct < 0.05, \
        f"group gain=1.0 peak {peak_group_1} != direct {peak_direct}"
    print("group gain=1.0 matches direct: ok")

    gt.gain = 0.5
    bridge.set_gain(gid, 0.5)
    peak_group_half = export_peak()
    ratio = peak_group_half / peak_direct
    assert 0.4 < ratio < 0.6, \
        f"group gain=0.5 ratio {ratio} expected ~0.5"
    print("group gain=0.5 halves amplitude: ok")

    gt.mute = True
    bridge.set_mute(gid, True)
    peak_muted = export_peak()
    assert peak_muted < 0.01, f"muted group should be silent, peak={peak_muted}"
    print("group mute: ok")

    gt.mute = False
    gt.gain = 1.0
    bridge.set_mute(gid, False)
    bridge.set_gain(gid, 1.0)
    gt.effects.append(Effect("filter", params=[200.0, 0.0]))
    bridge.sync_project(grouped)
    peak_filtered = export_peak()
    assert abs(peak_filtered - peak_direct) / peak_direct > 0.05, \
        "group filter should change the mix"
    print("group effect: ok")

    group_path = tmp / "group_v5.pydaw"
    grouped.save(group_path)
    loaded_grp = Project.load(group_path)
    assert loaded_grp.tracks[0].is_group and loaded_grp.tracks[0].type == "group"
    assert loaded_grp.tracks[1].group_id == gid
    print("group v5 save/load: ok")

    # -- automation v6: serialization + audible volume ramp ----------------
    auto_proj = Project(sample_rate=sr, bpm=120.0)
    at = Track(id=next_id(), name="Auto", gain=1.0)
    auto_proj.tracks.append(at)
    at.clips.append(Clip(id=next_id(), file_path=str(wav_a), file_id=fid_a,
                         start_frame=0, length=frames_a))
    vol_lane = AutomationLane(
        id=next_id(), target="volume", name="Volume",
        points=[AutomationPoint(0.0, 0.0),
                AutomationPoint(1.0, 2.0 / 3.0)])
    at.automation_lanes.append(vol_lane)
    auto_path = tmp / "auto_v6.pydaw"
    auto_proj.save(auto_path)
    loaded_auto = Project.load(auto_path)
    assert loaded_auto.tracks[0].automation_lanes[0].target == "volume"
    assert len(loaded_auto.tracks[0].automation_lanes[0].points) == 2
    data_auto = auto_path.read_text(encoding="utf-8")
    assert f'"version": {FORMAT_VERSION}' in data_auto
    print("automation v6 save/load: ok")

    bridge.sync_project(auto_proj)
    auto_out = tmp / "auto_ramp.wav"
    assert bridge.export_wav(str(auto_out))
    fid_auto, _ = bridge.load_file(str(auto_out))

    def segment_peak_rms(file_id: int, start_frame: int, length: int) -> float:
        spp = 256
        peaks = bridge.file_peaks(file_id, spp)
        i0 = start_frame // spp
        i1 = max(i0 + 1, (start_frame + length) // spp)
        chunk = peaks[i0 * 2:i1 * 2]
        if not chunk:
            return 0.0
        return math.sqrt(sum(v * v for v in chunk) / len(chunk))

    start_rms = segment_peak_rms(fid_auto, 0, int(0.05 * sr))
    end_rms = segment_peak_rms(fid_auto, int(0.9 * sr), int(0.1 * sr))
    assert start_rms < 0.05, f"start should be near silent, rms={start_rms}"
    assert end_rms > start_rms * 5, \
        f"end rms {end_rms} should be >> start {start_rms}"
    print("automation audible ramp: ok",
          "start_rms", round(start_rms, 4), "end_rms", round(end_rms, 4))

    bridge.remove_automation_lane(at.id, vol_lane.id)
    flat_out = tmp / "auto_flat.wav"
    assert bridge.export_wav(str(flat_out))
    fid_flat, _ = bridge.load_file(str(flat_out))
    flat_start = segment_peak_rms(fid_flat, 0, int(0.05 * sr))
    flat_end = segment_peak_rms(fid_flat, int(0.9 * sr), int(0.1 * sr))
    assert flat_start > 0.05 and flat_end > 0.05
    assert abs(flat_end - flat_start) / flat_end < 0.5, \
        "without automation amplitude should be roughly flat"
    print("remove_automation_lane: ok")

    bridge.shutdown()

    # -- VST3 hosting (engine API) -----------------------------------------
    eng = bridge.engine
    empty = eng.scan_vst_plugins(["Z:/no/such/vst/dir"])
    assert empty == [], "scan of missing dir should return empty list"
    print("vst scan missing dir: ok")

    system_plugins = eng.scan_vst_plugins(["C:/Program Files/Common Files/VST3"])
    assert isinstance(system_plugins, list), "system scan must return a list"
    print("vst system scan:", len(system_plugins), "plugins")
    for p in system_plugins[:5]:
        print(" ", p.name, p.uid, "instrument=" + str(p.is_instrument))

    eng.add_track(999, 1)
    assert not eng.add_vst_effect(999, "Z:/no/such.vst3", "00" * 16)
    assert eng.vst_param_count(999, 0) == 0
    assert eng.vst_get_state(999, 0) == b""
    print("vst invalid load: ok")

    if system_plugins:
        fx = next((p for p in system_plugins if not p.is_instrument), None)
        if fx:
            try:
                eng.add_track(998, 0)
                if eng.add_vst_effect(998, fx.path, fx.uid):
                    eng.add_midi_clip(998, 1, 0, eng.sample_rate())
                    ok = eng.export_wav(str(tmp / "vst_fx.wav"))
                    print("optional vst fx export:", ok)
            except Exception as exc:
                print("optional vst fx test skipped:", exc)

    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
