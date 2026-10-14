"""Pure zoom/sizing logic for the VST editor window.

Kept free of Qt so the math is unit-testable: scale stepping/clamping,
derivation of the plugin's 100% ("base") size from the size reported at
open time, and logical/physical pixel conversions for a given device
pixel ratio.
"""

from __future__ import annotations

from dataclasses import dataclass

ZOOM_MIN = 0.5
ZOOM_MAX = 2.0
ZOOM_STEP = 0.1

# Plugin-initiated resizes within this many logical pixels of our last
# request are treated as "same size" (plugins often round internally).
SIZE_TOLERANCE = 4


def clamp_zoom(value: float,
               lo: float = ZOOM_MIN, hi: float = ZOOM_MAX) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 1.0
    if value != value:  # NaN
        return 1.0
    return min(hi, max(lo, value))


def to_logical(w: int, h: int, dpr: float) -> tuple[int, int]:
    """Physical plugin pixels -> Qt logical pixels."""
    dpr = max(1.0, float(dpr))
    return (max(1, round(w / dpr)), max(1, round(h / dpr)))


def to_physical(w: int, h: int, dpr: float) -> tuple[int, int]:
    """Qt logical pixels -> physical plugin pixels."""
    dpr = max(1.0, float(dpr))
    return (max(1, round(w * dpr)), max(1, round(h * dpr)))


def sizes_close(a: tuple[int, int], b: tuple[int, int], dpr: float,
                tolerance: int = SIZE_TOLERANCE) -> bool:
    tol = tolerance * max(1.0, float(dpr))
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


@dataclass
class ZoomState:
    """Current zoom factor plus the plugin's 100% base size (physical px)."""

    scale: float = 1.0
    base_w: float = 0.0
    base_h: float = 0.0

    def step_target(self, direction: int) -> float | None:
        """Next zoom factor for a wheel step; None when already at limit."""
        target = round(clamp_zoom(self.scale + ZOOM_STEP * direction), 2)
        if abs(target - self.scale) < 1e-3:
            return None
        return target

    def init_from_open(self, w: int, h: int, content_scaled: bool,
                       saved_scale: float) -> None:
        """Derive base size from the size reported right after open.

        When the plugin accepted a content-scale factor before/at attach,
        the reported (w, h) is already scaled by `saved_scale`.
        """
        saved_scale = clamp_zoom(saved_scale)
        if content_scaled and saved_scale > 0:
            self.scale = saved_scale
            self.base_w = w / saved_scale
            self.base_h = h / saved_scale
        else:
            self.scale = 1.0
            self.base_w = float(w)
            self.base_h = float(h)

    def has_base(self) -> bool:
        return self.base_w > 0 and self.base_h > 0

    def target_size(self, scale: float) -> tuple[int, int]:
        """Physical plugin size for `scale` (plain-resize fallback path)."""
        return (max(1, round(self.base_w * scale)),
                max(1, round(self.base_h * scale)))
