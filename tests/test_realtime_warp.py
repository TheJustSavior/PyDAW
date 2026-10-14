from __future__ import annotations

import math
import struct
import threading
import wave
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

try:
    from pydaw import pydaw_engine
    HAS_ENGINE = True
except ImportError:
    HAS_ENGINE = False

pytestmark = pytest.mark.skipif(not HAS_ENGINE, reason="pydaw_engine not built")


def _write_tone(path: Path, frames: int = 24000, rate: int = 48000,
                freq: float = 440.0) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        data = bytearray()
        for i in range(frames):
            v = int(0.4 * 32767 * math.sin(2 * math.pi * freq * i / rate))
            data += struct.pack("<h", v)
        wav.writeframes(bytes(data))


def _engine_with_clip(tmp_path: Path, *, method: str = "resample",
                      ratio: float = 2.0):
    wav = tmp_path / f"tone_{method}_{ratio}.wav"
    _write_tone(wav)
    eng = pydaw_engine.AudioEngine()
    file_id = eng.load_file(str(wav))
    frames = eng.file_frames(file_id)
    eng.add_track(1, 0)
    eng.add_clip(1, 10, file_id, 0, 0, int(frames * ratio),
                 pydaw_engine.warp_method_code(method), float(ratio))
    eng.drain_commands()
    return eng, frames


def _debug_audio(eng, frames: int) -> np.ndarray:
    data = np.asarray(eng.debug_process(frames), dtype=np.float32)
    assert data.size == frames * 2
    return data.reshape(-1, 2)


def _export_audio(eng, path: Path, frames: int) -> np.ndarray:
    assert eng.export_wav(str(path), 0, frames)
    data, rate = sf.read(str(path), dtype="float32", always_2d=True)
    assert rate == 48000
    assert data.shape == (frames, 2)
    return data


@pytest.mark.parametrize("method", ("resample", "stretch"))
def test_debug_process_realtime_warp_is_finite_and_audible(
        tmp_path: Path, method: str) -> None:
    eng, src_frames = _engine_with_clip(tmp_path, method=method, ratio=2.0)
    eng.seek(0)
    eng.play()
    eng.drain_commands()

    out = _debug_audio(eng, src_frames)
    assert np.isfinite(out).all()
    assert float(np.max(np.abs(out))) > 0.01
    eng.shutdown()


def test_realtime_warp_seek_resets_continuity(tmp_path: Path) -> None:
    eng, src_frames = _engine_with_clip(tmp_path, method="stretch", ratio=2.0)
    seek_frame = src_frames // 2
    eng.play()
    eng.drain_commands()
    _debug_audio(eng, 1024)
    eng.seek(seek_frame)
    eng.drain_commands()
    jumped = _debug_audio(eng, 2048)

    fresh, _ = _engine_with_clip(tmp_path, method="stretch", ratio=2.0)
    fresh.seek(seek_frame)
    fresh.play()
    fresh.drain_commands()
    expected = _debug_audio(fresh, 2048)

    assert np.isfinite(jumped).all()
    assert float(np.max(np.abs(jumped))) <= 1.5
    assert float(np.max(np.abs(expected))) > 0.01
    eng.shutdown()
    fresh.shutdown()


def test_pause_clears_audio_track_peaks(tmp_path: Path) -> None:
    eng, src_frames = _engine_with_clip(tmp_path, method="resample", ratio=1.0)
    eng.play()
    out = _debug_audio(eng, min(4096, src_frames))
    assert float(np.max(np.abs(out))) > 0.01
    peak_l, peak_r = eng.track_peaks(1)
    assert max(peak_l, peak_r) > 0.01

    eng.pause()
    eng.drain_commands()
    assert eng.track_peaks(1) == (0.0, 0.0)
    eng.shutdown()


@pytest.mark.parametrize("method", ("stretch", "stretch_hd"))
def test_non_hop_aligned_warp_seek_matches_fresh_render(
        tmp_path: Path, method: str) -> None:
    seek_frame = 7777
    frames = 3072
    jumped_eng, _ = _engine_with_clip(tmp_path, method=method, ratio=1.5)
    fresh_eng, _ = _engine_with_clip(tmp_path, method=method, ratio=1.5)
    try:
        jumped_eng.play()
        jumped_eng.drain_commands()
        _debug_audio(jumped_eng, 4096)
        jumped_eng.seek(seek_frame)
        jumped_eng.drain_commands()
        jumped = _debug_audio(jumped_eng, frames)

        fresh_eng.seek(seek_frame)
        fresh_eng.play()
        fresh_eng.drain_commands()
        expected = _debug_audio(fresh_eng, frames)
    finally:
        jumped_eng.shutdown()
        fresh_eng.shutdown()

    assert np.isfinite(jumped).all()
    assert np.isfinite(expected).all()
    assert float(np.max(np.abs(expected))) > 0.01
    assert np.allclose(jumped, expected, atol=2e-4, rtol=2e-4)


def test_set_clip_warp_after_processing_stays_bounded(tmp_path: Path) -> None:
    eng, src_frames = _engine_with_clip(tmp_path, method="resample", ratio=1.0)
    eng.play()
    eng.drain_commands()
    before = _debug_audio(eng, 4096)

    eng.set_clip_warp(1, 10, pydaw_engine.warp_method_code("resample"), 2.0)
    eng.seek(0)
    eng.drain_commands()
    after = _debug_audio(eng, 4096)

    assert np.isfinite(after).all()
    assert float(np.max(np.abs(after))) <= 1.5
    assert not np.allclose(before, after)
    assert src_frames > 0
    eng.shutdown()


def test_warped_clip_tail_plays_past_source_frame_count(tmp_path: Path) -> None:
    eng, src_frames = _engine_with_clip(tmp_path, method="resample", ratio=2.0)
    # Regression: addClip/trimClip used to clamp timeline length to the raw
    # source file frame count, so the stretched tail was silently cut off.
    eng.seek(src_frames + 1024)
    eng.play()
    eng.drain_commands()
    tail = _debug_audio(eng, 2048)
    eng.shutdown()

    assert np.isfinite(tail).all()
    assert float(np.max(np.abs(tail))) > 0.01


def test_left_trimmed_resample_warp_uses_timeline_source_offset(
        tmp_path: Path) -> None:
    wav = tmp_path / "left_trimmed_ramp.wav"
    frames = 20000
    ramp = np.linspace(-0.45, 0.45, frames, dtype=np.float32)
    sf.write(str(wav), np.column_stack((ramp, -ramp)), 48000,
             subtype="FLOAT")
    eng = pydaw_engine.AudioEngine()
    file_id = eng.load_file(str(wav))
    source_offset = 4000
    ratio = 2.0
    try:
        eng.add_track(1, 0)
        eng.add_clip(1, 10, file_id, 0, source_offset, 4096,
                     pydaw_engine.warp_method_code("resample"), ratio)
        eng.drain_commands()
        eng.seek(0)
        eng.play()
        eng.drain_commands()
        out = _debug_audio(eng, 1024)
    finally:
        eng.shutdown()

    src_pos = (source_offset + np.arange(1024, dtype=np.float64)) / ratio
    expected_l = np.interp(src_pos, np.arange(frames), ramp)
    # Audio tracks use equal-power center pan before the master bus.
    pan_gain = math.cos(math.pi * 0.25)
    expected = (np.column_stack((expected_l, -expected_l))
                * pan_gain).astype(np.float32)
    assert np.isfinite(out).all()
    assert np.max(np.abs(out - expected)) < 1e-4


@pytest.mark.parametrize("method", ("resample", "stretch"))
def test_warped_clip_export_matches_realtime_debug_audio(
        tmp_path: Path, method: str) -> None:
    export_eng, src_frames = _engine_with_clip(
        tmp_path, method=method, ratio=1.5)
    realtime_eng, _ = _engine_with_clip(tmp_path, method=method, ratio=1.5)
    frames = int(src_frames * 1.5)
    export_path = tmp_path / f"export_{method}.wav"
    try:
        exported = _export_audio(export_eng, export_path, frames)
        realtime_eng.seek(0)
        realtime_eng.play()
        realtime_eng.drain_commands()
        realtime = _debug_audio(realtime_eng, frames)
    finally:
        export_eng.shutdown()
        realtime_eng.shutdown()

    assert np.isfinite(exported).all()
    assert np.isfinite(realtime).all()
    assert float(np.max(np.abs(exported))) > 0.01
    assert np.allclose(exported, realtime, atol=2e-4, rtol=2e-4)


def test_warped_clip_loop_wrap_matches_fresh_loop_start_render(
        tmp_path: Path) -> None:
    loop_start = 4096
    loop_end = 12288
    pre_wrap = 256
    post_wrap = 768
    wrapped_eng, _ = _engine_with_clip(tmp_path, method="stretch", ratio=2.0)
    fresh_eng, _ = _engine_with_clip(tmp_path, method="stretch", ratio=2.0)
    try:
        wrapped_eng.set_loop(loop_start, loop_end, True)
        wrapped_eng.seek(loop_end - pre_wrap)
        wrapped_eng.play()
        wrapped_eng.drain_commands()
        wrapped = _debug_audio(wrapped_eng, pre_wrap + post_wrap)
        wrapped_playhead = wrapped_eng.playhead_frame()

        fresh_eng.set_loop(loop_start, loop_end, True)
        fresh_eng.seek(loop_start)
        fresh_eng.play()
        fresh_eng.drain_commands()
        expected = _debug_audio(fresh_eng, post_wrap)
    finally:
        wrapped_eng.shutdown()
        fresh_eng.shutdown()

    after_wrap = wrapped[pre_wrap:]
    assert np.isfinite(wrapped).all()
    assert float(np.max(np.abs(after_wrap))) > 0.01
    assert np.allclose(after_wrap, expected, atol=2e-4, rtol=2e-4)
    assert wrapped_playhead == loop_start + post_wrap


def test_invalid_clip_geometry_is_ignored_or_clamped(tmp_path: Path) -> None:
    wav = tmp_path / "invalid_geometry.wav"
    _write_tone(wav, frames=4096)
    eng = pydaw_engine.AudioEngine()
    file_id = eng.load_file(str(wav))
    frames = eng.file_frames(file_id)
    eng.add_track(1, 0)

    eng.add_clip(1, 1, file_id, 0, frames + 1000, 0, 2, 2.0)
    eng.add_clip(1, 2, file_id, 0, -100, 256, 2, 1.0)
    eng.drain_commands()
    eng.trim_clip(1, 2, 0, frames + 1000, -1)
    eng.drain_commands()
    eng.play()
    out = _debug_audio(eng, 512)
    eng.shutdown()

    assert np.isfinite(out).all()
    assert float(np.max(np.abs(out))) <= 1.5


    wav = tmp_path / "concurrent.wav"
    _write_tone(wav)
    eng = pydaw_engine.AudioEngine()
    file_id = eng.load_file(str(wav))
    frames = eng.file_frames(file_id)
    errors: list[BaseException] = []

    def worker(seed: int) -> None:
        try:
            for i in range(20):
                track_id = 100 + seed * 100 + i
                clip_id = 1000 + seed * 100 + i
                eng.add_track(track_id, 0)
                eng.add_clip(track_id, clip_id, file_id, i * 8,
                             0, frames // 8, 2, 1.5)
                eng.track_peaks(track_id)
                eng.track_latency(track_id)
                if i % 3 == 0:
                    eng.drain_commands()
        except BaseException as exc:  # noqa: BLE001 - preserve thread failure
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    eng.drain_commands()
    out = _debug_audio(eng, 512)
    eng.shutdown()
    assert not errors
    assert np.isfinite(out).all()
