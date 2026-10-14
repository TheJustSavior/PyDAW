"""Solo/Mute on a group track (engine-level, offline export).

Soloing a group must keep its child tracks audible (and cut the rest);
muting a group silences only its children. Regression test for the bug
where group solo silenced the group's own children.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import soundfile as sf


def _rms(path: str) -> float:
    data, _ = sf.read(path, dtype="float32", always_2d=True)
    return float(np.sqrt((data ** 2).mean()))


def test_group_solo_mute() -> None:
    from pydaw import pydaw_engine as eng

    e = eng.AudioEngine()
    sr = 48000

    tmp = tempfile.mkdtemp(prefix="pydaw_gsolo_")
    src = os.path.join(tmp, "tone.wav")
    t = np.arange(sr) / sr
    sf.write(src, np.stack([np.sin(2 * np.pi * 220 * t)] * 2, 1) * 0.5, sr)

    GROUP, CHILD, OTHER = 1, 2, 3
    e.add_track(GROUP, 2)          # group bus
    e.add_track(CHILD, 0)          # audio track inside the group
    e.add_track(OTHER, 0)          # ungrouped audio track
    e.set_track_group(CHILD, GROUP)
    fid = e.load_file(src)
    e.add_clip(CHILD, 10, fid, 0, 0, sr)
    e.add_clip(OTHER, 11, fid, 0, 0, sr)

    def render(name: str) -> float:
        out = os.path.join(tmp, name)
        assert e.export_wav(out, 0, sr)
        return _rms(out)

    base = render("base.wav")
    assert base > 0.1  # both tones sum

    # Solo on the group: children stay audible, the other track is cut.
    e.set_solo(GROUP, True)
    solo_grp = render("solo_group.wav")
    e.set_solo(GROUP, False)
    assert solo_grp > 0.1, "group solo silenced its own children"
    assert solo_grp < base * 0.9, "group solo did not cut the other track"

    # Mute on the group: children silenced, the other track still plays.
    e.set_mute(GROUP, True)
    mute_grp = render("mute_group.wav")
    e.set_mute(GROUP, False)
    assert 0.05 < mute_grp < base * 0.9, "group mute misbehaved"

    # Solo on a child inside the group still works as before.
    e.set_solo(CHILD, True)
    solo_child = render("solo_child.wav")
    e.set_solo(CHILD, False)
    assert solo_child > 0.1
    assert solo_child < base * 0.9

    print("GROUP SOLO/MUTE TEST PASSED")


if __name__ == "__main__":
    test_group_solo_mute()
