"""Unit tests for VST editor zoom/sizing helpers."""

from __future__ import annotations

import pytest

from pydaw.ui.vst_zoom import (
    ZOOM_MAX,
    ZOOM_MIN,
    ZOOM_STEP,
    ZoomState,
    clamp_zoom,
    sizes_close,
    to_logical,
    to_physical,
)


def test_clamp_zoom_defaults_and_limits() -> None:
    assert clamp_zoom(1.0) == 1.0
    assert clamp_zoom(0.1) == ZOOM_MIN
    assert clamp_zoom(99.0) == ZOOM_MAX
    assert clamp_zoom("bad") == 1.0
    assert clamp_zoom(float("nan")) == 1.0


def test_dpr_conversions_round_trip() -> None:
    assert to_logical(1920, 1080, 2.0) == (960, 540)
    assert to_physical(960, 540, 2.0) == (1920, 1080)
    assert to_logical(800, 600, 1.0) == (800, 600)


def test_sizes_close_scales_tolerance_with_dpr() -> None:
    assert sizes_close((100, 100), (103, 103), dpr=1.0)
    assert not sizes_close((100, 100), (110, 110), dpr=1.0)
    # tolerance is 4 logical px * dpr
    assert sizes_close((200, 200), (207, 207), dpr=2.0)
    assert not sizes_close((200, 200), (215, 215), dpr=2.0)


def test_zoom_state_step_target() -> None:
    z = ZoomState(scale=1.0, base_w=800, base_h=600)
    assert z.step_target(1) == pytest.approx(1.1)
    assert z.step_target(-1) == pytest.approx(0.9)

    z.scale = ZOOM_MAX
    assert z.step_target(1) is None

    z.scale = ZOOM_MIN
    assert z.step_target(-1) is None


def test_zoom_state_init_from_open_content_scaled() -> None:
    z = ZoomState()
    z.init_from_open(1100, 880, content_scaled=True, saved_scale=1.1)
    assert z.scale == pytest.approx(1.1)
    assert z.base_w == pytest.approx(1000.0)
    assert z.base_h == pytest.approx(800.0)


def test_zoom_state_init_from_open_plain() -> None:
    z = ZoomState()
    z.init_from_open(1024, 768, content_scaled=False, saved_scale=1.5)
    assert z.scale == 1.0
    assert z.base_w == 1024.0
    assert z.base_h == 768.0


def test_zoom_state_target_size() -> None:
    z = ZoomState(base_w=1000, base_h=800)
    assert z.target_size(1.5) == (1500, 1200)
    assert z.has_base()

    empty = ZoomState()
    assert not empty.has_base()
    assert empty.target_size(2.0) == (1, 1)
