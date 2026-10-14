"""Headless bridge sound test: load a real VST instrument through the
default subprocess bridge, schedule a note and verify audible output.

Usage: py tests/debug_vst_sound.py "TAL-NoiseMaker"
"""

import math
import sys
import time

from pydaw import pydaw_engine

VST_DIR = r"C:\Program Files\Common Files\VST3"


def main() -> int:
    needle = sys.argv[1].lower() if len(sys.argv) > 1 else "noisemaker"
    plugins = pydaw_engine.AudioEngine.scan_vst_plugins([VST_DIR])
    matches = [p for p in plugins if needle in p.name.lower()
               and p.is_instrument]
    if not matches:
        print(f"no instrument matching '{needle}'")
        return 1
    info = matches[0]
    print(f"using: {info.name} ({info.path})")

    eng = pydaw_engine.AudioEngine()
    eng.add_track(1, 1)  # midi track
    t0 = time.perf_counter()
    ok = eng.set_vst_instrument(1, info.path, info.uid)
    print(f"set_vst_instrument returned in "
          f"{(time.perf_counter() - t0) * 1000:.0f} ms, ok={ok}")
    if not ok:
        return 2
    # Load is asynchronous now: wait until the bridge reports ready.
    for _ in range(600):
        diag = eng.vst_diagnostic(1, -1)
        if "loading" not in diag:
            break
        time.sleep(0.1)
    print("diag:", eng.vst_diagnostic(1, -1))

    eng.add_midi_clip(1, 1, 0, 48000 * 2)
    eng.add_note(1, 1, 1, 60, 1.0, 0, 48000)
    eng.play()

    total = 48000
    buf = eng.debug_process(total)
    peak = max(abs(x) for x in buf)
    rms = math.sqrt(sum(x * x for x in buf) / len(buf))
    print(f"peak={peak:.4f} rms={rms:.5f}")
    print("diag after:", eng.vst_diagnostic(1, -1))
    eng.stop()
    eng.clear_all()
    print("SOUND OK" if peak > 0.001 else "SILENCE!")
    return 0 if peak > 0.001 else 3


if __name__ == "__main__":
    sys.exit(main())
