from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

try:
    from pydaw import pydaw_engine
    HAS_ENGINE = True
except ImportError:
    HAS_ENGINE = False

pytestmark = pytest.mark.skipif(not HAS_ENGINE, reason="pydaw_engine not built")


def test_default_vst_proxy_rejects_missing_path() -> None:
    eng = pydaw_engine.AudioEngine()
    eng.add_track(1, 0)
    assert not eng.add_vst_effect(1, "Z:/no/such.vst3", "00" * 16)
    assert not eng.set_vst_instrument(1, "Z:/no/such.vst3", "00" * 16)
    eng.shutdown()


def test_default_vst_proxy_effect_bypasses_audio(tmp_path: Path) -> None:
    fake = tmp_path / "Fake.vst3"
    fake.mkdir()
    eng = pydaw_engine.AudioEngine()
    eng.add_track(1, 0)
    assert eng.add_vst_effect(1, str(fake), "00" * 16)
    assert eng.vst_param_count(1, 0) == 0
    assert eng.vst_get_state(1, 0) == b""
    assert eng.vst_set_state(1, 0, b"state")
    assert eng.vst_get_state(1, 0) == b"state"
    eng.vst_set_param(1, 0, 3, 0.25)
    assert eng.vst_consume_last_tweak() == (1, 0, 3)
    eng.shutdown()


def test_default_vst_proxy_instrument_renders_silence(tmp_path: Path) -> None:
    fake = tmp_path / "FakeInstrument.vst3"
    fake.mkdir()
    eng = pydaw_engine.AudioEngine()
    eng.add_track(1, 1)
    assert eng.set_vst_instrument(1, str(fake), "00" * 16)
    eng.add_midi_clip(1, 10, 0, 48000)
    eng.add_note(1, 10, 1, 60, 1.0, 0, 24000)
    eng.play()
    eng.drain_commands()
    out = np.asarray(eng.debug_process(1024), dtype=np.float32)
    eng.shutdown()
    assert np.isfinite(out).all()
    assert float(np.max(np.abs(out))) == 0.0
