"""Step 10 engine features: PDC delay-line mode, background peak
pyramid, disk cache LRU limit, parallel group-bus rendering.

Run standalone (python tests/test_step10.py) or via pytest.
"""

from __future__ import annotations

import hashlib
import math
import os
import struct
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Isolate the disk cache before the engine module is imported.
_CACHE_DIR = Path(tempfile.mkdtemp(prefix="pydaw_cache_step10_"))
os.environ.setdefault("PYDAW_CACHE_DIR", str(_CACHE_DIR))

from pydaw.engine_bridge import EngineBridge, ENGINE_AVAILABLE  # noqa: E402
from pydaw.model.project import Clip, Project, Track, next_id  # noqa: E402

TMP = Path(__file__).parent / "_tmp"
TMP.mkdir(exist_ok=True)

ROOT = str(Path(__file__).resolve().parent.parent)


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


def _build_grouped_project(bridge: EngineBridge, wav: Path) -> Project:
    """Two groups x two children + one free track."""
    fid, frames = bridge.load_file(str(wav))
    project = Project(sample_rate=bridge.sample_rate)
    for g in range(2):
        grp = Track(id=next_id(), name=f"G{g}", type="group",
                    gain=0.8, pan=(g * 2 - 1) * 0.3)
        project.tracks.append(grp)
        for k in range(2):
            t = Track(id=next_id(), name=f"G{g}T{k}", group_id=grp.id,
                      gain=0.4 + 0.2 * k, pan=(k * 2 - 1) * 0.5)
            t.clips.append(Clip(id=next_id(), file_path=str(wav),
                                file_id=fid, start_frame=k * 4000,
                                length=frames))
            project.tracks.append(t)
    free = Track(id=next_id(), name="Free", gain=0.6)
    free.clips.append(Clip(id=next_id(), file_path=str(wav), file_id=fid,
                           start_frame=2000, length=frames))
    project.tracks.append(free)
    return project


def test_pdc_mode_api() -> None:
    """pdc_mode round-trips; with zero-latency chains the delay-line
    mode must render bit-identically to look-ahead mode (all delays 0)."""
    assert ENGINE_AVAILABLE
    bridge = EngineBridge(48000)
    try:
        assert bridge.pdc_mode() == 0
        wav = TMP / "step10_tone.wav"
        make_wav(wav, seconds=1.5)
        project = _build_grouped_project(bridge, wav)
        bridge.sync_project(project)

        out_a = TMP / "pdc_lookahead.wav"
        assert bridge.export_wav(str(out_a))

        bridge.set_pdc_mode(1)
        assert bridge.pdc_mode() == 1
        out_b = TMP / "pdc_delayline.wav"
        assert bridge.export_wav(str(out_b))
        assert file_hash(out_a) == file_hash(out_b), \
            "zero-latency delay-line render must equal look-ahead render"

        bridge.set_pdc_mode(0)
        assert bridge.pdc_mode() == 0
        print("pdc mode: ok (round-trip + bit-identical at zero latency)")
    finally:
        bridge.shutdown()


def test_parallel_group_render() -> None:
    """Grouped project rendered with the pool must be bit-identical to a
    single-threaded render (PYDAW_MIX_THREADS=1 subprocess)."""
    assert ENGINE_AVAILABLE
    bridge = EngineBridge(48000)
    try:
        wav = TMP / "step10_tone.wav"
        make_wav(wav, seconds=1.5)
        project = _build_grouped_project(bridge, wav)
        bridge.sync_project(project)
        out = TMP / "groups_pool.wav"
        assert bridge.export_wav(str(out))
        h_pool = file_hash(out)

        code = (
            "import sys, os; sys.path.insert(0, {root!r});"
            "os.environ['PYDAW_CACHE_DIR'] = {cache!r};"
            "from tests.test_step10 import _group_render_single;"
            "_group_render_single()"
        ).format(root=ROOT, cache=str(_CACHE_DIR))
        env = dict(os.environ, PYDAW_MIX_THREADS="1")
        res = subprocess.run([sys.executable, "-c", code], env=env,
                             capture_output=True, text=True, timeout=120)
        assert res.returncode == 0, res.stderr
        h_single = res.stdout.strip().splitlines()[-1]
        assert h_pool == h_single, \
            f"parallel group render differs: {h_pool} != {h_single}"
        print("parallel group busses: ok (identical to single-thread)")
    finally:
        bridge.shutdown()


def _group_render_single() -> None:
    """Subprocess helper for test_parallel_group_render."""
    bridge = EngineBridge(48000)
    try:
        assert bridge.mix_threads() == 0, "expected serial mixing"
        wav = TMP / "step10_tone.wav"
        project = _build_grouped_project(bridge, wav)
        bridge.sync_project(project)
        out = TMP / "groups_single.wav"
        assert bridge.export_wav(str(out))
        print(file_hash(out))
    finally:
        bridge.shutdown()


def test_background_peak_pyramid() -> None:
    """After load_file the base peak pyramid must appear on disk without
    anyone asking for peaks (background import-time build)."""
    assert ENGINE_AVAILABLE
    cache = Path(tempfile.mkdtemp(prefix="pydaw_bgpeaks_"))
    wav = TMP / "bg_peaks_tone.wav"
    make_wav(wav, freq=330.0, seconds=2.0)
    code = (
        "import sys, os, time; sys.path.insert(0, {root!r});"
        "os.environ['PYDAW_CACHE_DIR'] = {cache!r};"
        "from pydaw.engine_bridge import EngineBridge;"
        "from pathlib import Path;"
        "b = EngineBridge(48000);"
        "b.load_file({wav!r});"
        "deadline = time.time() + 15.0;"
        "found = False\n"
        "while time.time() < deadline:\n"
        "    if list(Path({cache!r}).glob('*.peaks')):\n"
        "        found = True; break\n"
        "    time.sleep(0.05)\n"
        "b.shutdown(); print('PEAKS' if found else 'MISSING')"
    ).format(root=ROOT, cache=str(cache), wav=str(wav))
    res = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip().splitlines()[-1] == "PEAKS", \
        "peak pyramid should be built in the background after import"
    print("background peak pyramid: ok")


def test_cache_lru_limit() -> None:
    """With PYDAW_CACHE_LIMIT_MB=1 the oldest cache files must be evicted
    until the directory fits the limit; the newest file survives."""
    assert ENGINE_AVAILABLE
    cache = Path(tempfile.mkdtemp(prefix="pydaw_lru_"))
    # Three ~0.7 MB files with staggered mtimes: total ~2.1 MB > 1 MB.
    names = ["old.pcm", "mid.pcm", "new.pcm"]
    now = time.time()
    for i, name in enumerate(names):
        p = cache / name
        p.write_bytes(b"\0" * (700 * 1024))
        age = (len(names) - 1 - i) * 3600  # old.pcm is the oldest
        os.utime(p, (now - age, now - age))

    code = (
        "import sys, os, time; sys.path.insert(0, {root!r});"
        "os.environ['PYDAW_CACHE_DIR'] = {cache!r};"
        "os.environ['PYDAW_CACHE_LIMIT_MB'] = '1';"
        "from pydaw.engine_bridge import EngineBridge;"
        "from pathlib import Path;"
        "b = EngineBridge(48000);"
        "deadline = time.time() + 15.0\n"
        "while time.time() < deadline:\n"
        "    files = sorted(f.name for f in Path({cache!r}).iterdir())\n"
        "    total = sum(f.stat().st_size"
        " for f in Path({cache!r}).iterdir())\n"
        "    if total <= 1024 * 1024:\n"
        "        break\n"
        "    time.sleep(0.05)\n"
        "b.shutdown(); print(','.join(files))"
    ).format(root=ROOT, cache=str(cache))
    res = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, res.stderr
    survivors = res.stdout.strip().splitlines()[-1].split(",")
    assert "new.pcm" in survivors, "newest cache entry must survive"
    assert "old.pcm" not in survivors, "oldest cache entry must be evicted"
    print("cache LRU limit: ok, survivors:", survivors)


def test_playback_advances() -> None:
    """Realtime playhead must keep moving with parallel track rendering."""
    assert ENGINE_AVAILABLE
    bridge = EngineBridge(44100, 512)
    try:
        project = Project(sample_rate=bridge.sample_rate)
        project.tracks.append(Track(id=next_id(), name="A", type="audio"))
        project.tracks.append(Track(id=next_id(), name="M", type="midi"))
        bridge.sync_project(project)
        bridge.seek(0)
        bridge.play()
        time.sleep(0.25)
        f1 = bridge.playhead_frame()
        time.sleep(0.25)
        f2 = bridge.playhead_frame()
        assert bridge.is_playing()
        assert f1 > bridge.sample_rate // 10, f"playhead too slow after 0.25s: {f1}"
        assert f2 > f1, f"playhead stuck: {f1} -> {f2}"
    finally:
        bridge.pause()
        bridge.shutdown()


def test_playback_after_export() -> None:
    """exporting_ must always clear; playback works after offline render."""
    assert ENGINE_AVAILABLE
    wav = TMP / "playback_after_export.wav"
    make_wav(wav, seconds=0.5, rate=44100)
    bridge = EngineBridge(44100, 512)
    try:
        fid, frames = bridge.load_file(str(wav))
        project = Project(sample_rate=bridge.sample_rate)
        track = Track(id=next_id(), name="A")
        track.clips.append(Clip(id=next_id(), file_path=str(wav),
                                 file_id=fid, start_frame=0, length=frames))
        project.tracks.append(track)
        bridge.sync_project(project)
        out = TMP / "export_check.wav"
        assert bridge.export_wav(str(out))
        assert bridge.export_progress() >= 1.0
        bridge.seek(0)
        bridge.play()
        time.sleep(0.3)
        pos = bridge.playhead_frame()
        assert pos > bridge.sample_rate // 5, \
            f"playhead frozen after export: {pos}"
    finally:
        bridge.pause()
        bridge.shutdown()


def test_playback_during_export() -> None:
    """Concurrent export must not permanently freeze transport."""
    assert ENGINE_AVAILABLE
    import threading

    wav = TMP / "long_export.wav"
    make_wav(wav, seconds=2.0, rate=44100)
    bridge = EngineBridge(44100, 512)
    try:
        fid, frames = bridge.load_file(str(wav))
        project = Project(sample_rate=bridge.sample_rate)
        track = Track(id=next_id(), name="A")
        track.clips.append(Clip(id=next_id(), file_path=str(wav),
                                 file_id=fid, start_frame=0, length=frames))
        project.tracks.append(track)
        bridge.sync_project(project)
        bridge.seek(0)
        bridge.play()
        time.sleep(0.05)
        out = TMP / "during_export.wav"
        err: list[str] = []

        def worker() -> None:
            try:
                bridge.export_wav(str(out))
            except Exception as exc:
                err.append(str(exc))

        th = threading.Thread(target=worker)
        th.start()
        time.sleep(1.0)
        f1 = bridge.playhead_frame()
        th.join(timeout=30.0)
        assert not err, err
        assert bridge.export_progress() >= 1.0
        bridge.play()
        time.sleep(0.3)
        f2 = bridge.playhead_frame()
        assert f2 > f1, f"playhead did not recover: {f1} -> {f2}"
    finally:
        bridge.pause()
        bridge.shutdown()


def test_playhead_smooth_cap() -> None:
    """UI smoothing: capped at play start, smooth and monotonic after."""
    from pydaw.ui.playhead_smoother import PlayheadSmoother

    sr, buf = 44100, 512
    s = PlayheadSmoother()

    # 1. Engine stuck at 0 (first callbacks in flight): estimate must not
    #    sprint past one buffer (~the old 00:00.145 bug).
    t = 0.0
    for _ in range(12):  # ~192 ms of 60 Hz ticks
        t += 0.016
        est = s.update(0, True, sr, buf, now=t)
    assert est <= buf, f"uncapped extrapolation overshoots: {est}"

    # 2. Engine advancing normally: estimate is smooth (monotonic, no
    #    jump back when the UI tick lands just before an engine update).
    s.reset()
    t = 10.0
    raw = 0
    prev = -1
    for i in range(60):
        t += 0.016
        raw = int((t - 10.0) * sr) // buf * buf  # engine steps per buffer
        est = s.update(raw, True, sr, buf, now=t)
        assert est >= prev, f"playhead jumped back: {prev} -> {est}"
        assert est <= raw + buf, f"ran ahead of engine: {est} > {raw + buf}"
        prev = est
    assert est > sr * 0.7, f"playhead barely moved: {est}"

    # 3. Seek backwards is accepted immediately.
    est = s.update(0, True, sr, buf, now=t + 0.016)
    assert est <= buf, f"seek back not accepted: {est}"

    # 4. Stopped: raw is returned verbatim and state resets.
    assert s.update(12345, False, sr, buf, now=t + 0.032) == 12345
    print("playhead smoothing: ok")


def test_unicode_path_load_and_export() -> None:
    """Files in non-ASCII folders (e.g. 'Документы') must load and export."""
    assert ENGINE_AVAILABLE
    udir = TMP / "Документы юникод"
    udir.mkdir(exist_ok=True)
    wav = udir / "тестовый файл.wav"
    make_wav(wav, seconds=0.3, rate=44100)
    bridge = EngineBridge(44100, 512)
    try:
        fid, frames = bridge.load_file(str(wav))
        assert frames > 0, "unicode wav did not load"
        project = Project(sample_rate=bridge.sample_rate)
        track = Track(id=next_id(), name="U")
        track.clips.append(Clip(id=next_id(), file_path=str(wav),
                                 file_id=fid, start_frame=0, length=frames))
        project.tracks.append(track)
        bridge.sync_project(project)
        out = udir / "экспорт.wav"
        assert bridge.export_wav(str(out)), "export to unicode path failed"
        assert out.exists() and out.stat().st_size > 44
    finally:
        bridge.pause()
        bridge.shutdown()
    print("unicode paths: ok")


def test_default_projects_dir() -> None:
    from pydaw.paths import (default_export_dir, default_projects_dir,
                             ensure_user_dirs, user_root_dir)

    ensure_user_dirs()
    root = user_root_dir()
    projects = default_projects_dir()
    export = default_export_dir()
    assert os.path.isdir(root)
    assert os.path.isdir(projects)
    assert os.path.isdir(export)
    assert projects == os.path.join(root, "projects")
    assert export == os.path.join(root, "export")
    print("user dirs: ok", root)


def main() -> int:
    test_pdc_mode_api()
    test_parallel_group_render()
    test_background_peak_pyramid()
    test_cache_lru_limit()
    test_playback_advances()
    test_playback_after_export()
    test_playback_during_export()
    test_playhead_smooth_cap()
    test_unicode_path_load_and_export()
    test_default_projects_dir()
    print("ALL STEP 10 TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
