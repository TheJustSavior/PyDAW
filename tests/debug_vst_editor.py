"""Open a plugin's native editor inside a bare Qt window for N seconds.

Usage:  py -3 tests/debug_vst_editor.py "Serum 2" [seconds]
"""

import faulthandler
import sys
import time

faulthandler.enable()

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget

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
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0

    plugins = pydaw_engine.AudioEngine.scan_vst_plugins([VST_DIR])
    matches = [p for p in plugins if needle in p.name.lower()]
    if not matches:
        print(f"no plugin matching '{needle}'")
        return 1
    info = matches[0]
    print(f"using: {info.name} | instrument={info.is_instrument}", flush=True)

    eng = pydaw_engine.AudioEngine()
    eng.add_track(1, 1 if info.is_instrument else 0)
    slot = -1 if info.is_instrument else 0
    if info.is_instrument:
        ok = eng.set_vst_instrument(1, info.path, info.uid)
    else:
        ok = eng.add_vst_effect(1, info.path, info.uid)
    print(f"load ok: {ok}", flush=True)
    if not ok:
        return 2
    wait_loaded(eng, 1, slot)
    print("diag:", eng.vst_diagnostic(1, slot), flush=True)

    app = QApplication(sys.argv)
    host = QWidget()
    host.setWindowTitle(f"editor test: {info.name}")
    host.resize(400, 300)
    host.show()
    hwnd = int(host.winId())
    print("opening editor...", flush=True)
    if not eng.vst_open_editor(1, slot, hwnd):
        print("vst_open_editor failed", flush=True)
        print("diag:", eng.vst_diagnostic(1, slot), flush=True)
        return 3
    w, h = eng.vst_editor_size(1, slot)
    print(f"editor open ({w}x{h}), running event loop {seconds}s", flush=True)
    host.resize(max(w, 100), max(h, 100))

    def try_zoom():
        ok_zoom = eng.vst_set_content_scale(1, slot, 1.5)
        zw, zh = eng.vst_editor_size(1, slot)
        print(f"content scale 1.5 accepted={ok_zoom}, size now {zw}x{zh}",
              flush=True)
        if not ok_zoom:
            ok_rs = eng.vst_resize_editor(1, slot,
                                          int(w * 1.5), int(h * 1.5))
            zw, zh = eng.vst_editor_size(1, slot)
            print(f"fallback resize ok={ok_rs}, size now {zw}x{zh}",
                  flush=True)
        host.resize(max(zw, 100), max(zh, 100))

    QTimer.singleShot(2000, try_zoom)
    QTimer.singleShot(int(seconds * 1000), app.quit)
    app.exec()

    print("closing editor", flush=True)
    eng.vst_close_editor(1, slot)
    host.close()
    eng.clear_all()
    print("OK, clean exit", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
