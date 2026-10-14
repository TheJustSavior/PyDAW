from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from pydaw import audio_join
from pydaw.model.project import Clip


def _write(path: Path, data: np.ndarray, sr: int = 48000) -> None:
    sf.write(str(path), data.astype(np.float32), sr, subtype="FLOAT")


def test_render_join_wav_preserves_gap_and_reuses_cache(
        tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PYDAW_CACHE_DIR", str(tmp_path / "cache"))
    src = tmp_path / "src.wav"
    _write(src, np.full((1000, 2), 0.5, dtype=np.float32))
    clips = [
        Clip(id=1, file_path=str(src), file_id=1, start_frame=0, length=1000),
        Clip(id=2, file_path=str(src), file_id=1, start_frame=3000, length=1000),
    ]

    out1 = audio_join.render_join_wav(clips, 48000)
    out2 = audio_join.render_join_wav(clips, 48000)
    assert out1 == out2
    data, sr = sf.read(out1, dtype="float32", always_2d=True)
    assert sr == 48000
    assert data.shape == (4000, 2)
    assert float(np.max(np.abs(data[1500:2500]))) == 0.0
    assert float(np.max(np.abs(data[:500]))) > 0.4


def test_render_join_wav_applies_source_offset_and_fades(
        tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PYDAW_CACHE_DIR", str(tmp_path / "cache"))
    src = tmp_path / "ramp.wav"
    ramp = np.linspace(0.0, 1.0, 1000, dtype=np.float32)[:, None]
    _write(src, np.repeat(ramp, 2, axis=1))
    clip = Clip(id=1, file_path=str(src), file_id=1, start_frame=0,
                length=400, source_offset=100, fade_in=100, fade_out=100)

    out = audio_join.render_join_wav([clip], 48000)
    data, _ = sf.read(out, dtype="float32", always_2d=True)
    assert data.shape == (400, 2)
    assert abs(float(data[0, 0])) < 1e-6
    assert float(data[200, 0]) > float(data[100, 0])
    assert abs(float(data[-1, 0])) < 1e-3


def test_render_join_wav_resamples_to_engine_rate(
        tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PYDAW_CACHE_DIR", str(tmp_path / "cache"))
    src = tmp_path / "44100.wav"
    _write(src, np.ones((441, 1), dtype=np.float32) * 0.25, sr=44100)
    clip = Clip(id=1, file_path=str(src), file_id=1, start_frame=0,
                length=480)

    out = audio_join.render_join_wav([clip], 48000)
    data, sr = sf.read(out, dtype="float32", always_2d=True)
    assert sr == 48000
    assert data.shape[0] == 480
    assert float(np.max(np.abs(data))) > 0.2


def test_render_join_wav_stereoizes_mono_when_mixed_with_stereo(
        tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PYDAW_CACHE_DIR", str(tmp_path / "cache"))
    mono = tmp_path / "mono.wav"
    stereo = tmp_path / "stereo.wav"
    _write(mono, np.ones((100, 1), dtype=np.float32) * 0.25)
    _write(stereo, np.column_stack([
        np.ones(100, dtype=np.float32) * 0.5,
        np.ones(100, dtype=np.float32) * -0.5,
    ]))
    clips = [
        Clip(id=1, file_path=str(mono), file_id=1, start_frame=0, length=100),
        Clip(id=2, file_path=str(stereo), file_id=2, start_frame=100, length=100),
    ]

    out = audio_join.render_join_wav(clips, 48000)
    data, _ = sf.read(out, dtype="float32", always_2d=True)
    assert data.shape == (200, 2)
    np.testing.assert_allclose(data[:100, 0], data[:100, 1], atol=1e-6)
    assert float(data[150, 0]) > 0.4
    assert float(data[150, 1]) < -0.4
