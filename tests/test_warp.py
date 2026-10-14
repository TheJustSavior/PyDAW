"""Tests for C++ warp DSP (offline) vs NumPy reference."""

from __future__ import annotations

import numpy as np
import pytest

from pydaw.audio_stretch import (MAX_RATIO, MIN_RATIO, WARP_METHODS,
                                 reference_stretch_audio)

try:
    from pydaw import pydaw_engine
    HAS_ENGINE = True
except ImportError:
    HAS_ENGINE = False

pytestmark = pytest.mark.skipif(not HAS_ENGINE, reason="pydaw_engine not built")


def _sine(frames: int, sr: int = 48000, freq: float = 440.0) -> np.ndarray:
    t = np.arange(frames, dtype=np.float32) / sr
    s = np.sin(2 * np.pi * freq * t).astype(np.float32)
    return np.stack([s, s * 0.8], axis=1)


def _cpp_stretch(data: np.ndarray, ratio: float, method: str) -> np.ndarray:
    flat = pydaw_engine.stretch_buffer(
        np.ascontiguousarray(data, np.float32),
        data.shape[0], data.shape[1], float(ratio), method)
    return np.array(flat, dtype=np.float32).reshape(-1, 2)


@pytest.mark.parametrize("method", WARP_METHODS)
@pytest.mark.parametrize("ratio", (0.5, 2.0))
def test_cpp_offline_matches_reference(method: str, ratio: float) -> None:
    data = _sine(48000 if method != "stretch_hd" else 96000)
    ref = reference_stretch_audio(data, ratio, method)
    cpp = _cpp_stretch(data, ratio, method)
    assert ref.shape == cpp.shape
    err = float(np.max(np.abs(ref - cpp)))
    limit = 0.05 if method == "stretch_hd" else 1e-3
    assert err < limit, f"{method} ratio={ratio} max err {err}"


def test_ratio_bounds() -> None:
    data = _sine(4096)
    out = _cpp_stretch(data, 0.05, "stretch")
    assert out.shape[0] >= 1
    out2 = _cpp_stretch(data, 20.0, "stretch")
    assert out2.shape[0] <= data.shape[0] * MAX_RATIO + 4


def test_unity_passthrough() -> None:
    data = _sine(8192)
    out = _cpp_stretch(data, 1.0, "stretch")
    np.testing.assert_allclose(out, data, atol=1e-5)


def test_warp_method_codes() -> None:
    assert pydaw_engine.warp_method_code("stretch") == 0
    assert pydaw_engine.warp_method_code("stretch_hd") == 1
    assert pydaw_engine.warp_method_code("resample") == 2
