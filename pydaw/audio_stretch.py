"""Offline time-stretching for audio clips (warp).

Realtime playback uses the C++ engine warp processors. This module keeps
cache helpers for join (Ctrl+J) and optional offline renders.
"""

from __future__ import annotations

import hashlib
import os
import tempfile

import numpy as np

WARP_METHODS = ("stretch", "stretch_hd", "resample")
WARP_LABELS = {
    "stretch": "Stretch",
    "stretch_hd": "Stretch HD",
    "resample": "Resample",
}
# Default warp for clips created via import or timeline drag-and-drop.
NEW_CLIP_WARP = "resample"
MIN_RATIO = 0.1
MAX_RATIO = 10.0


def cache_dir() -> str:
    """Directory for stretched renders (mirrors the engine cache rules)."""
    base = os.environ.get("PYDAW_CACHE_DIR")
    if not base:
        local = os.environ.get("LOCALAPPDATA")
        if local:
            base = os.path.join(local, "PyDAW", "cache")
        else:
            base = os.path.join(tempfile.gettempdir(), "pydaw_cache")
    path = os.path.join(base, "stretch")
    os.makedirs(path, exist_ok=True)
    return path


def _render_key(path: str, method: str, ratio: float) -> str:
    try:
        st = os.stat(path)
        sig = f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        sig = os.path.abspath(path)
    sig += f"|{method}|{ratio:.6f}"
    return hashlib.sha1(sig.encode("utf-8")).hexdigest()[:24]


def _stretch_via_engine(path: str, method: str, ratio: float,
                        sample_rate: int) -> str:
    from pydaw import pydaw_engine
    return pydaw_engine.stretch_file_to_cache(
        path, method, float(ratio), int(sample_rate))


# -- legacy numpy fallback (tests / dev without engine) --------------------

def _resample(data: np.ndarray, ratio: float) -> np.ndarray:
    n = data.shape[0]
    out_n = max(1, int(round(n * ratio)))
    out = np.empty((out_n, data.shape[1]), dtype=np.float32)
    for i in range(out_n):
        src_pos = i / ratio
        i0 = int(np.floor(src_pos))
        i1 = min(i0 + 1, n - 1)
        frac = np.float32(src_pos - i0)
        out[i] = data[i0] * (1.0 - frac) + data[i1] * frac
    return out


def stretch_audio(data: np.ndarray, ratio: float, method: str) -> np.ndarray:
    """Stretch `data` (frames, channels) by `ratio` using `method`."""
    ratio = float(min(MAX_RATIO, max(MIN_RATIO, ratio)))
    if abs(ratio - 1.0) < 1e-9:
        return data.astype(np.float32, copy=False)
    try:
        from pydaw import pydaw_engine
        flat = np.ascontiguousarray(data, dtype=np.float32)
        out_flat = pydaw_engine.stretch_buffer(
            flat, int(flat.shape[0]), int(flat.shape[1]), ratio, method)
        return np.array(out_flat, dtype=np.float32).reshape(-1, flat.shape[1])
    except Exception:
        pass
    if method == "resample":
        return _resample(data.astype(np.float32, copy=False), ratio)
    import soundfile as sf  # noqa: F401 - only for stretched_wav_path fallback
    raise RuntimeError("warp engine unavailable")


def stretched_wav_path(path: str, method: str, ratio: float, *,
                       sample_rate: int = 48000) -> str:
    """Render (or reuse) a stretched copy of `path`; returns the WAV path."""
    ratio = float(min(MAX_RATIO, max(MIN_RATIO, ratio)))
    if abs(ratio - 1.0) < 1e-9:
        return path
    try:
        return _stretch_via_engine(path, method, ratio, sample_rate)
    except Exception:
        import soundfile as sf
        out = os.path.join(cache_dir(), _render_key(path, method, ratio) + ".wav")
        if os.path.exists(out):
            return out
        data, sr = sf.read(path, dtype="float32", always_2d=True)
        stretched = stretch_audio(data, ratio, method)
        tmp = out + ".tmp"
        sf.write(tmp, stretched, sr, subtype="FLOAT", format="WAV")
        os.replace(tmp, out)
        return out


def load_clip_audio(bridge, clip) -> tuple[int, int]:
    """Load the original audio for `clip` into the engine; warp is realtime."""
    return bridge.load_file(clip.file_path)


# -- NumPy reference implementations (tests only) ----------------------------

def _reference_stretch_ola(data: np.ndarray, ratio: float) -> np.ndarray:
    n = data.shape[0]
    win_len = 4096
    if n < win_len * 2:
        win_len = max(256, 1 << max(2, int(np.log2(max(4, n // 4)))))
    syn_hop = win_len // 4
    ana_hop = syn_hop / ratio
    win = np.hanning(win_len).astype(np.float32)
    out_n = max(1, int(round(n * ratio)))
    out = np.zeros((out_n + win_len, data.shape[1]), dtype=np.float32)
    norm = np.zeros(out_n + win_len, dtype=np.float32)
    pos = 0.0
    out_pos = 0
    while out_pos < out_n:
        start = int(round(pos))
        chunk = np.zeros((win_len, data.shape[1]), dtype=np.float32)
        avail = min(win_len, n - start)
        if avail > 0:
            chunk[:avail] = data[start:start + avail]
        out[out_pos:out_pos + win_len] += chunk * win[:, None]
        norm[out_pos:out_pos + win_len] += win
        pos += ana_hop
        out_pos += syn_hop
    norm = np.maximum(norm, 1e-6)
    return (out[:out_n] / norm[:out_n, None]).astype(np.float32)


def _reference_stretch_vocoder(data: np.ndarray, ratio: float) -> np.ndarray:
    n = data.shape[0]
    win_len = 2048
    if n < win_len * 2:
        return _reference_stretch_ola(data, ratio)
    syn_hop = win_len // 4
    ana_hop = syn_hop / ratio
    win = np.hanning(win_len).astype(np.float32)
    out_n = max(1, int(round(n * ratio)))
    n_bins = win_len // 2 + 1
    omega = 2.0 * np.pi * np.arange(n_bins) * syn_hop / win_len
    channels = []
    for ch in range(data.shape[1]):
        x = data[:, ch]
        out = np.zeros(out_n + win_len, dtype=np.float32)
        norm = np.zeros(out_n + win_len, dtype=np.float32)
        phase_acc = None
        prev_phase = None
        pos = 0.0
        out_pos = 0
        while out_pos < out_n:
            start = int(round(pos))
            frame = np.zeros(win_len, dtype=np.float32)
            avail = min(win_len, n - start)
            if avail > 0:
                frame[:avail] = x[start:start + avail]
            spec = np.fft.rfft(frame * win)
            mag = np.abs(spec)
            phase = np.angle(spec)
            if phase_acc is None:
                phase_acc = phase.copy()
            else:
                dphi = phase - prev_phase - omega * (ana_hop / syn_hop)
                dphi -= 2.0 * np.pi * np.round(dphi / (2.0 * np.pi))
                true_freq = omega + (dphi * syn_hop / ana_hop
                                     if ana_hop > 0 else 0.0)
                phase_acc = phase_acc + true_freq
            prev_phase = phase
            frame_out = np.fft.irfft(
                mag * np.exp(1j * phase_acc)).astype(np.float32)
            out[out_pos:out_pos + win_len] += frame_out * win
            norm[out_pos:out_pos + win_len] += win * win
            pos += ana_hop
            out_pos += syn_hop
        norm = np.maximum(norm, 1e-6)
        channels.append(out[:out_n] / norm[:out_n])
    return np.stack(channels, axis=1).astype(np.float32)


def reference_stretch_audio(data: np.ndarray, ratio: float,
                            method: str) -> np.ndarray:
    """Pure NumPy stretch (golden reference for tests)."""
    ratio = float(min(MAX_RATIO, max(MIN_RATIO, ratio)))
    if abs(ratio - 1.0) < 1e-9:
        return data.astype(np.float32, copy=False)
    data = data.astype(np.float32, copy=False)
    if method == "resample":
        return _resample(data, ratio)
    if method == "stretch_hd":
        return _reference_stretch_vocoder(data, ratio)
    return _reference_stretch_ola(data, ratio)
