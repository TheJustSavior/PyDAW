"""Wall-clock smoothing of the engine playhead for 60 Hz UI updates.

The engine playhead advances once per audio callback (a whole buffer at
a time), so sampling it at UI rate gives uneven, steppy motion. This
helper extrapolates between engine updates by wall-clock, anchored at
the moment the engine value last changed, with two guarantees:

* the estimate never runs more than one audio buffer ahead of the last
  confirmed engine frame (no "sprint" past the real position at play
  start, when the first callbacks are still in flight);
* the estimate is monotonic during normal playback (no visible jerk
  back), while seeks and loop wraps are accepted immediately.

Pure Python, no Qt: unit-testable without a GUI.
"""
from __future__ import annotations

import time


class PlayheadSmoother:
    def __init__(self) -> None:
        self._est: float | None = None
        self._raw_base: int | None = None
        self._raw_time: float = 0.0

    def reset(self) -> None:
        self._est = None
        self._raw_base = None
        self._raw_time = 0.0

    def update(self, raw: int, playing: bool, sample_rate: int,
               buffer_frames: int, now: float | None = None) -> int:
        """Feed one UI tick; returns the frame to display."""
        if now is None:
            now = time.perf_counter()
        sr = max(1, sample_rate)
        buf = max(1, buffer_frames)

        if not playing:
            self.reset()
            return raw

        if self._raw_base is None or raw != self._raw_base:
            # Engine advanced (or first playing tick): re-anchor the
            # wall-clock extrapolation at the fresh engine value.
            self._raw_base = raw
            self._raw_time = now
        est = self._raw_base + (now - self._raw_time) * sr
        est = min(est, float(raw + buf))

        prev = self._est
        if prev is not None and est < prev:
            # Seek / loop wrap: the engine really went backwards -> jump.
            # Otherwise hold the previous value (monotonic playback).
            if not (prev - est > sr * 0.25 or float(raw + buf) < prev):
                est = prev
        self._est = est
        return int(est)
