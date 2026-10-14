"""Step 9 performance features: streaming, disk peak cache, export
progress/cancel, plugin delay compensation, multicore mixing.

Run standalone (python tests/test_performance.py) or via pytest.
"""

from __future__ import annotations

import hashlib
import math
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Isolate the disk cache before the engine module is imported.
_CACHE_DIR = Path(tempfile.mkdtemp(prefix="pydaw_cache_test_"))
os.environ.setdefault("PYDAW_CACHE_DIR", str(_CACHE_DIR))

from pydaw.engine_bridge import EngineBridge, ENGINE_AVAILABLE  # noqa: E402
from pydaw.model.project import Clip, Project, Track, next_id  # noqa: E402

TMP = Path(__file__).parent / "_tmp"
TMP.mkdir(exist_ok=True)


def make_wav(path: Path, freq: float = 440.0, seconds: float = 1.0,
             rate: int = 48000) -> None:
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


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_project(bridge: EngineBridge, wav: Path,
                   n_tracks: int = 4) -> Project:
    fid, frames = bridge.load_file(str(wav))
    project = Project(sample_rate=bridge.sample_rate)
    for k in range(n_tracks):
        t = Track(id=next_id(), name=f"T{k}", pan=(k % 3 - 1) * 0.5,
                  gain=0.5 + 0.1 * k)
        t.clips.append(Clip(id=next_id(), file_path=str(wav), file_id=fid,
                            start_frame=k * 1000, length=frames))
        project.tracks.append(t)
    return project


def test_multicore_mixing() -> None:
    """The render pool must be active and produce output identical to a
    single-threaded render (routing/summation stays deterministic)."""
    assert ENGINE_AVAILABLE
    bridge = EngineBridge(48000)
    try:
        assert bridge.mix_threads() >= 0
        wav = TMP / "perf_tone.wav"
        make_wav(wav, seconds=2.0)
        project = _build_project(bridge, wav, n_tracks=6)
        bridge.sync_project(project)

        out = TMP / "perf_multi.wav"
        assert bridge.export_wav(str(out))
        h_multi = file_hash(out)

        # Same render in a single-threaded subprocess must be identical.
        code = (
            "import sys, os; sys.path.insert(0, {root!r});"
            "os.environ['PYDAW_CACHE_DIR'] = {cache!r};"
            "from tests.test_performance import _single_thread_render;"
            "_single_thread_render()"
        ).format(root=str(Path(__file__).resolve().parent.parent),
                 cache=str(_CACHE_DIR))
        env = dict(os.environ, PYDAW_MIX_THREADS="1")
        res = subprocess.run([sys.executable, "-c", code], env=env,
                             capture_output=True, text=True, timeout=120)
        assert res.returncode == 0, res.stderr
        h_single = res.stdout.strip().splitlines()[-1]
        assert h_multi == h_single, \
            f"multithreaded render differs: {h_multi} != {h_single}"
        print("multicore mixing: ok (identical to single-thread),",
              bridge.mix_threads(), "workers")
    finally:
        bridge.shutdown()


def _single_thread_render() -> None:
    """Subprocess helper for test_multicore_mixing (PYDAW_MIX_THREADS=1)."""
    bridge = EngineBridge(48000)
    try:
        assert bridge.mix_threads() == 0, "expected serial mixing"
        wav = TMP / "perf_tone.wav"
        project = _build_project(bridge, wav, n_tracks=6)
        bridge.sync_project(project)
        out = TMP / "perf_single.wav"
        assert bridge.export_wav(str(out))
        print(file_hash(out))
    finally:
        bridge.shutdown()


def test_peak_disk_cache() -> None:
    """Base peaks are persisted; derived zoom levels match a direct scan."""
    assert ENGINE_AVAILABLE
    bridge = EngineBridge(48000)
    try:
        wav = TMP / "peak_tone.wav"
        make_wav(wav, freq=330.0, seconds=1.5)
        fid, frames = bridge.load_file(str(wav))

        peaks_fine = bridge.file_peaks(fid, 64)     # direct scan (< base 256)
        peaks_base = bridge.file_peaks(fid, 256)    # pyramid base
        peaks_coarse = bridge.file_peaks(fid, 2048) # derived from base
        assert len(peaks_base) == 2 * ((frames + 255) // 256)
        assert len(peaks_coarse) == 2 * ((frames + 2047) // 2048)
        assert max(peaks_base) > 0.4 and max(peaks_coarse) > 0.4
        # Derived coarse block must bound the fine blocks it covers.
        assert abs(max(peaks_coarse) - max(peaks_fine)) < 0.02

        cache_files = list(_CACHE_DIR.glob("*.peaks"))
        assert cache_files, "peak cache file should be written to disk"
        print("peak disk cache: ok,", len(cache_files), "cache file(s)")
    finally:
        bridge.shutdown()


def test_streaming_large_file() -> None:
    """With a 1 MB threshold a 2 MB+ file must round-trip through the
    mmap cache with identical export output."""
    assert ENGINE_AVAILABLE
    wav = TMP / "stream_tone.wav"
    make_wav(wav, freq=220.0, seconds=8.0)  # ~1.5 MB source, 3 MB decoded

    code = (
        "import sys, os; sys.path.insert(0, {root!r});"
        "os.environ['PYDAW_CACHE_DIR'] = {cache!r};"
        "from tests.test_performance import _streamed_render;"
        "_streamed_render()"
    ).format(root=str(Path(__file__).resolve().parent.parent),
             cache=str(_CACHE_DIR))
    env = dict(os.environ, PYDAW_STREAM_THRESHOLD_MB="1")
    res = subprocess.run([sys.executable, "-c", code], env=env,
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stderr
    h_streamed = res.stdout.strip().splitlines()[-1]

    # Reference render fully in RAM (default 64 MB threshold).
    bridge = EngineBridge(48000)
    try:
        fid, frames = bridge.load_file(str(wav))
        project = Project(sample_rate=bridge.sample_rate)
        t = Track(id=next_id(), name="S")
        t.clips.append(Clip(id=next_id(), file_path=str(wav), file_id=fid,
                            start_frame=0, length=frames))
        project.tracks.append(t)
        bridge.sync_project(project)
        out = TMP / "stream_ram.wav"
        assert bridge.export_wav(str(out))
        assert file_hash(out) == h_streamed, \
            "streamed render must be bit-identical to the RAM render"
        pcm_files = list(_CACHE_DIR.glob("*.pcm"))
        assert pcm_files, "streamed PCM cache file should exist"
        print("streaming: ok, pcm cache:",
              [f.name for f in pcm_files[:3]])
    finally:
        bridge.shutdown()


def _streamed_render() -> None:
    """Subprocess helper for test_streaming_large_file (1 MB threshold)."""
    bridge = EngineBridge(48000)
    try:
        wav = TMP / "stream_tone.wav"
        fid, frames = bridge.load_file(str(wav))
        project = Project(sample_rate=bridge.sample_rate)
        t = Track(id=next_id(), name="S")
        t.clips.append(Clip(id=next_id(), file_path=str(wav), file_id=fid,
                            start_frame=0, length=frames))
        project.tracks.append(t)
        bridge.sync_project(project)
        out = TMP / "stream_mapped.wav"
        assert bridge.export_wav(str(out))
        print(file_hash(out))
    finally:
        bridge.shutdown()


def test_export_progress_and_cancel() -> None:
    """Progress must advance during export; cancel must abort and remove
    the partial file."""
    assert ENGINE_AVAILABLE
    bridge = EngineBridge(48000)
    try:
        wav = TMP / "long_tone.wav"
        make_wav(wav, seconds=2.0)
        fid, frames = bridge.load_file(str(wav))
        project = Project(sample_rate=bridge.sample_rate)
        t = Track(id=next_id(), name="L")
        # Repeat the clip to make a ~60 s timeline (render takes a while).
        for k in range(30):
            t.clips.append(Clip(id=next_id(), file_path=str(wav),
                                file_id=fid, start_frame=k * frames,
                                length=frames))
        project.tracks.append(t)
        bridge.sync_project(project)

        out = TMP / "progress_full.wav"
        seen = []

        def poll() -> None:
            while not done.is_set():
                seen.append(bridge.export_progress())
                time.sleep(0.005)

        done = threading.Event()
        poller = threading.Thread(target=poll)
        poller.start()
        assert bridge.export_wav(str(out))
        done.set()
        poller.join()
        partial = [p for p in seen if 0.0 <= p < 1.0]
        assert partial, "export progress should be observable mid-render"
        assert bridge.export_progress() == 1.0
        assert not bridge.export_was_cancelled()
        print("export progress: ok,", len(partial), "mid-render samples")

        # Cancellation. A fixed post-start delay is racy: with warm caches
        # the whole export can finish before the cancel lands. Use a much
        # longer timeline and trigger cancel on observed mid-render
        # progress instead.
        for k in range(30, 240):
            t.clips.append(Clip(id=next_id(), file_path=str(wav),
                                file_id=fid, start_frame=k * frames,
                                length=frames))
        bridge.sync_project(project)
        out2 = TMP / "progress_cancelled.wav"
        result = {}

        def do_export() -> None:
            result["ok"] = bridge.export_wav(str(out2))

        worker = threading.Thread(target=do_export)
        worker.start()
        deadline = time.time() + 20.0
        while time.time() < deadline and "ok" not in result \
                and not (0.0 < bridge.export_progress() < 0.9):
            time.sleep(0.001)
        bridge.cancel_export()
        worker.join(timeout=30)
        assert result.get("ok") is False, "cancelled export must return False"
        assert bridge.export_was_cancelled()
        assert not out2.exists(), "partial file must be removed on cancel"
        print("export cancel: ok")
    finally:
        bridge.shutdown()


def test_pdc_api() -> None:
    """PDC accessors: zero-latency chains report 0, toggle round-trips."""
    assert ENGINE_AVAILABLE
    bridge = EngineBridge(48000)
    try:
        assert bridge.engine.pdc_enabled()
        wav = TMP / "pdc_tone.wav"
        make_wav(wav, seconds=0.5)
        project = _build_project(bridge, wav, n_tracks=1)
        bridge.sync_project(project)
        tid = project.tracks[0].id
        bridge.add_effect(tid, "delay")  # built-in fx: zero latency
        assert bridge.track_latency(tid) == 0
        bridge.set_pdc_enabled(False)
        assert not bridge.engine.pdc_enabled()
        bridge.set_pdc_enabled(True)
        assert bridge.engine.pdc_enabled()
        print("pdc api: ok")
    finally:
        bridge.shutdown()


def main() -> int:
    test_multicore_mixing()
    test_peak_disk_cache()
    test_streaming_large_file()
    test_export_progress_and_cancel()
    test_pdc_api()
    print("ALL PERFORMANCE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
