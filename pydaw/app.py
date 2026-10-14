"""PyDAW application entry point: `python -m pydaw.app` or `pydaw`.

Theme and UI scale changes do not need a manual restart: the main
window requests a soft restart, the QApplication is torn down and a
new one is created with the new ``QT_SCALE_FACTOR`` / palette, and the
session (project state) is carried over through a temp file.

Language switches apply immediately via ``MainWindow._retranslate_ui()``
without restarting the process.
"""

from __future__ import annotations

import faulthandler
import gc
import os
import sys

from PySide6.QtCore import QSettings

# Dump Python stacks on native crashes (e.g. inside a VST plugin) so the
# console shows where the engine call came from.
faulthandler.enable()

_VALID_SCALES = frozenset({"1.0", "1.25", "1.5", "1.75", "2.0"})


def _hard_exit(code: int = 0) -> None:
    """Terminate the process without C++/Python static destructors.

    After VST3 plugins were loaded, normal exit (and even ``os._exit`` with
    Qt active) can still AV in loaded DLLs; ``ExitProcess`` is immediate.
    """
    try:
        import ctypes
        ctypes.windll.kernel32.ExitProcess(int(code))
    except OSError:
        pass
    os._exit(code)


def _read_scale(settings: QSettings) -> str:
    scale = str(settings.value("ui/scale", "1.0"))
    return scale if scale in _VALID_SCALES else "1.0"


def _read_theme(settings: QSettings) -> str:
    name = str(settings.value("ui/theme", "dark")).lower()
    return name if name in ("dark", "light") else "dark"


def _purge_ui_modules() -> None:
    """Drop cached pydaw.ui modules so theme constants are re-captured."""
    for name in [m for m in sys.modules if m.startswith("pydaw.ui")]:
        del sys.modules[name]


def _run_once(is_restart: bool) -> bool:
    """One QApplication lifetime. Returns True when a restart is requested."""
    settings = QSettings("PyDAW", "PyDAW")
    os.environ["QT_SCALE_FACTOR"] = _read_scale(settings)
    theme_name = _read_theme(settings)

    # The palette and language must be switched BEFORE the UI modules
    # are imported: many widgets capture theme constants and translated
    # strings at import/construction time. On a soft restart the modules
    # are purged so the new palette/language is re-captured.
    _purge_ui_modules()
    from .ui import i18n, theme
    i18n.set_language(
        i18n.normalize(settings.value("ui/language", "")))
    theme.set_theme(theme_name)

    from PySide6.QtWidgets import QApplication
    from .paths import ensure_user_dirs
    from .ui.main_window import MainWindow

    ensure_user_dirs()
    app = QApplication(sys.argv)
    app.setApplicationName("PyDAW")
    theme.apply_theme(app, name=theme_name)

    window = MainWindow()
    if is_restart:
        window.restore_session_if_pending()
    else:
        # A cold start is always a new project: drop any stale restore
        # flag left behind by an interrupted soft restart.
        window.discard_pending_session()
    window.showMaximized()
    app.exec()

    restart = window.restart_requested
    if not restart:
        # Normal quit: closeEvent already shut the engine down. Skip Qt /
        # Python teardown — loaded VST3 DLLs can AV during static dtors.
        _hard_exit(0)

    # Soft restart (theme/scale): tear down QApplication and boot again.
    window.deleteLater()
    app.processEvents()
    del window
    gc.collect()
    app.processEvents()
    import shiboken6
    shiboken6.delete(app)
    del app
    gc.collect()
    return restart


def main() -> int:
    is_restart = False
    while _run_once(is_restart):
        is_restart = True
    _hard_exit(0)
    return 0  # unreachable; keeps the declared return type honest


if __name__ == "__main__":
    sys.exit(main())
