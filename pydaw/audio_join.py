"""Offline rendering for joining audio clips (Ctrl+J).

Selected audio clips of one lane are baked into a single WAV file:
each clip's audible region (source window, warp already rendered by
``audio_stretch``, linear fades applied) is placed at its timeline
position relative to the earliest clip; gaps between clips stay silent.
The result goes to the PyDAW cache directory and is loaded into the
engine like any other file, so the C++ side needs no changes. Renders
are content-keyed and reused between sessions.
"""

from __future__ import annotations

import hashlib
import os

import numpy as np
import soundfile as sf

from .audio_stretch import cache_dir as _stretch_cache_dir
from .audio_stretch import stretched_wav_path


def cache_dir() -> str:
    """Directory for joined renders (sibling of the stretch cache)."""
    path = os.path.join(os.path.dirname(_stretch_cache_dir()), "join")
    os.makedirs(path, exist_ok=True)
    return path


def _effective_source_path(clip, sample_rate: int) -> str:
    """Path to audio for join bake: offline stretch cache when warped."""
    if abs(clip.stretch_ratio - 1.0) < 1e-9:
        return clip.file_path
    return stretched_wav_path(clip.file_path, clip.warp, clip.stretch_ratio,
                              sample_rate=sample_rate)


def _file_sig(path: str) -> str:
    try:
        st = os.stat(path)
        return f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        return os.path.abspath(path)


def _join_key(clips, sample_rate: int) -> str:
    parts = [str(sample_rate)]
    for c in sorted(clips, key=lambda c: c.start_frame):
        parts.append("|".join((
            _file_sig(c.file_path), c.warp, f"{c.stretch_ratio:.6f}",
            str(c.start_frame), str(c.source_offset), str(c.length),
            str(c.fade_in), str(c.fade_out))))
    return hashlib.sha1("||".join(parts).encode("utf-8")).hexdigest()[:24]


def _resample_to(data: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Linear-interpolation resample to the engine rate (mirrors the
    engine's own on-load resampling closely enough for a bake)."""
    if src_sr == dst_sr or data.shape[0] == 0:
        return data
    n = data.shape[0]
    out_n = max(1, int(round(n * dst_sr / src_sr)))
    src = np.linspace(0.0, n - 1, out_n)
    i0 = np.floor(src).astype(np.int64)
    i1 = np.minimum(i0 + 1, n - 1)
    frac = (src - i0)[:, None].astype(np.float32)
    return data[i0] * (1.0 - frac) + data[i1] * frac


def render_join_wav(clips, sample_rate: int) -> str:
    """Render `clips` (audio clips of one lane) into a single WAV at the
    engine sample rate; returns the path. Gaps stay silent, linear
    fades are baked in (overlapping selections are rejected by the UI
    before this is called). Raises on decode error."""
    clips = sorted(clips, key=lambda c: c.start_frame)
    out_path = os.path.join(cache_dir(),
                            _join_key(clips, sample_rate) + ".wav")
    if os.path.exists(out_path):
        return out_path

    base = clips[0].start_frame
    total = max(c.start_frame + c.length for c in clips) - base
    channels = 1
    segments = []
    for c in clips:
        data, sr = sf.read(_effective_source_path(c, sample_rate),
                           dtype="float32", always_2d=True)
        data = _resample_to(data, sr, sample_rate)
        seg = np.zeros((c.length, data.shape[1]), dtype=np.float32)
        s0 = max(0, c.source_offset)
        s1 = min(data.shape[0], c.source_offset + c.length)
        if s1 > s0:
            seg[s0 - c.source_offset:s1 - c.source_offset] = data[s0:s1]
        if c.fade_in > 0:
            n = min(int(c.fade_in), c.length)
            seg[:n] *= np.linspace(0.0, 1.0, n,
                                   dtype=np.float32)[:, None]
        if c.fade_out > 0:
            n = min(int(c.fade_out), c.length)
            seg[c.length - n:] *= np.linspace(1.0, 0.0, n,
                                              dtype=np.float32)[:, None]
        channels = max(channels, seg.shape[1])
        segments.append((c.start_frame - base, seg))

    out = np.zeros((total, channels), dtype=np.float32)
    for offset, seg in segments:
        if seg.shape[1] < channels:  # mono into a stereo bake
            seg = np.repeat(seg, channels, axis=1)
        out[offset:offset + seg.shape[0]] += seg

    tmp = out_path + ".tmp"
    sf.write(tmp, out, sample_rate, subtype="FLOAT", format="WAV")
    os.replace(tmp, out_path)
    return out_path
