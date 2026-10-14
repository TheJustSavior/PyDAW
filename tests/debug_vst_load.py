"""Headless VST load test: scan + instantiate a plugin by name substring.

Usage:  py -3 tests/debug_vst_load.py "Serum 2" [--instrument]
Prints [vst] trace lines from the engine to stderr.
"""

import sys
import time

from pydaw import pydaw_engine

VST_DIR = r"C:\Program Files\Common Files\VST3"


def wait_loaded(eng, track_id: int, slot: int, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        diag = eng.vst_diagnostic(track_id, slot)
        if not eng.vst_is_loading(track_id, slot) and "loading" not in diag.lower():
            return
        time.sleep(0.1)


def main() -> int:
    needle = sys.argv[1].lower() if len(sys.argv) > 1 else "serum"
    plugins = pydaw_engine.AudioEngine.scan_vst_plugins([VST_DIR])
    matches = [p for p in plugins if needle in p.name.lower()]
    if not matches:
        print(f"no plugin matching '{needle}' found among {len(plugins)}")
        return 1
    for p in matches:
        print(f"match: {p.name} | {p.vendor} | instrument={p.is_instrument}"
              f"\n  path: {p.path}\n  uid:  {p.uid}")

    info = matches[0]
    eng = pydaw_engine.AudioEngine()
    eng.add_track(1, 1 if info.is_instrument else 0)
    print(f"\n--- loading '{info.name}' ---", flush=True)
    if info.is_instrument:
        ok = eng.set_vst_instrument(1, info.path, info.uid)
    else:
        ok = eng.add_vst_effect(1, info.path, info.uid)
    print(f"load ok: {ok}", flush=True)
    if not ok:
        return 2

    slot = -1 if info.is_instrument else 0
    wait_loaded(eng, 1, slot)
    print("diag:", eng.vst_diagnostic(1, slot), flush=True)
    print("param count:", eng.vst_param_count(1, slot))
    state = eng.vst_get_state(1, slot)
    print("state size:", len(state))
    print("has editor:", eng.vst_has_editor(1, slot))
    print("OK, shutting down cleanly", flush=True)
    eng.clear_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
