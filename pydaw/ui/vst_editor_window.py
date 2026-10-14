"""Native VST plugin editor window.

Supports user zoom: Ctrl + mouse wheel over the plugin working area
scales the plugin GUI (VST3 content scale, with a plain resize fallback
for resizable plugins). The current zoom is shown in a slim header bar
and persisted per plugin uid in QSettings.

The plugin view is a native child HWND that swallows mouse input, so
Qt never sees wheel events over it. A per-thread Windows mouse hook
(WH_MOUSE) intercepts Ctrl+wheel aimed at the plugin view and routes
it to the owning editor window instead.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt, QSettings, QTimer, Signal, QEventLoop
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel,
                               QScrollArea, QVBoxLayout, QWidget)

from ..engine_bridge import EngineBridge
from . import theme
from .vst_widgets import VstParamPanel
from .vst_zoom import (ZOOM_MAX, ZOOM_MIN, ZOOM_STEP, ZoomState, clamp_zoom,
                       sizes_close, to_logical, to_physical)

__all__ = ["VstEditorWindow", "ZOOM_MIN", "ZOOM_MAX", "ZOOM_STEP"]


# ---------------------------------------------------------------------------
# Ctrl+wheel interception over the native plugin view (Windows)
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32

    _WH_MOUSE = 7
    _WM_MOUSEWHEEL = 0x020A
    _VK_CONTROL = 0x11

    # Explicit prototypes: the defaults truncate 64-bit HHOOK/LPARAM values.
    _user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                          wintypes.HINSTANCE, wintypes.DWORD]
    _user32.SetWindowsHookExW.restype = ctypes.c_void_p
    _user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    _user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    _user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                       wintypes.WPARAM, wintypes.LPARAM]
    _user32.CallNextHookEx.restype = ctypes.c_ssize_t
    _user32.GetKeyState.argtypes = [ctypes.c_int]
    _user32.GetKeyState.restype = ctypes.c_short
    _user32.IsChild.argtypes = [wintypes.HWND, wintypes.HWND]
    _user32.IsChild.restype = wintypes.BOOL

    class _MOUSEHOOKSTRUCTEX(ctypes.Structure):
        _fields_ = [
            ("pt", wintypes.POINT),
            ("hwnd", wintypes.HWND),
            ("wHitTestCode", wintypes.UINT),
            ("dwExtraInfo", ctypes.c_size_t),
            ("mouseData", wintypes.DWORD),
        ]

    _HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                   wintypes.WPARAM, wintypes.LPARAM)

    class _WheelZoomHook:
        """Thread-local mouse hook shared by all open editor windows."""

        _instance: "_WheelZoomHook | None" = None

        def __init__(self) -> None:
            self._windows: list["VstEditorWindow"] = []
            self._hook = None
            self._proc = None  # keep the ctypes callback alive

        @classmethod
        def instance(cls) -> "_WheelZoomHook":
            if cls._instance is None:
                cls._instance = _WheelZoomHook()
            return cls._instance

        def register(self, win: "VstEditorWindow") -> None:
            if win not in self._windows:
                self._windows.append(win)
            self._install()

        def unregister(self, win: "VstEditorWindow") -> None:
            if win in self._windows:
                self._windows.remove(win)
            if not self._windows:
                self._remove()

        def _install(self) -> None:
            if self._hook:
                return
            self._proc = _HOOKPROC(self._on_mouse)
            self._hook = _user32.SetWindowsHookExW(
                _WH_MOUSE, self._proc, None,
                _kernel32.GetCurrentThreadId())

        def _remove(self) -> None:
            if self._hook:
                _user32.UnhookWindowsHookEx(self._hook)
                self._hook = None
                self._proc = None

        def _on_mouse(self, n_code: int, w_param: int, l_param: int) -> int:
            if (n_code >= 0 and w_param == _WM_MOUSEWHEEL
                    and _user32.GetKeyState(_VK_CONTROL) & 0x8000):
                info = _MOUSEHOOKSTRUCTEX.from_address(
                    l_param & 0xFFFFFFFFFFFFFFFF)
                target = info.hwnd
                delta = ctypes.c_short((info.mouseData >> 16) & 0xFFFF).value
                for win in list(self._windows):
                    if win.owns_native_window(target):
                        step = 1 if delta > 0 else -1
                        # Defer: don't re-enter the plugin from inside
                        # its own message processing.
                        QTimer.singleShot(
                            0, lambda w=win, s=step: w.zoom_step(s))
                        return 1  # swallow the wheel message
            return _user32.CallNextHookEx(None, n_code, w_param, l_param)
else:  # non-Windows stub

    class _WheelZoomHook:
        @classmethod
        def instance(cls) -> "_WheelZoomHook":
            return cls()

        def register(self, win) -> None:
            pass

        def unregister(self, win) -> None:
            pass


class VstEditorWindow(QDialog):
    """Non-modal dialog hosting a VST plugin's native editor view."""

    # Generic fallback slider moved: (track_id, slot, param, normalized).
    # The owner (MainWindow) applies it, giving these edits undo support.
    param_edited = Signal(int, int, int, float)
    undo_requested = Signal()
    redo_requested = Signal()

    def __init__(self, bridge: EngineBridge, track_id: int, slot: int,
                 title: str, plugin_uid: str = "", parent=None) -> None:
        super().__init__(parent)
        self._bridge = bridge
        self._track_id = track_id
        self._slot = slot
        self._plugin_uid = plugin_uid
        # When True the owner handles param_edited (undo path); otherwise
        # slider edits are applied directly to the engine.
        self.route_params_externally = False
        self.setWindowTitle(title)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setModal(False)

        # Undo/redo from the editor window (Qt-reachable parts: header,
        # generic sliders; the native plugin view swallows its own keys).
        sc_undo = QShortcut(QKeySequence.Undo, self)
        sc_undo.setContext(Qt.WidgetWithChildrenShortcut)
        sc_undo.activated.connect(self.undo_requested.emit)
        sc_redo = QShortcut(QKeySequence.Redo, self)
        sc_redo.setContext(Qt.WidgetWithChildrenShortcut)
        sc_redo.activated.connect(self.redo_requested.emit)

        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(8, 8, 8, 8)
        self._root.setSpacing(6)

        self._header = QWidget(self)
        self._header.setStyleSheet(
            f"background: {theme.PANEL};"
            f" border-bottom: 1px solid {theme.BORDER_SOFT};")
        header_lay = QHBoxLayout(self._header)
        header_lay.setContentsMargins(10, 3, 10, 3)
        header_lay.setSpacing(8)
        self._zoom_label = QLabel("Zoom: 100%")
        self._zoom_label.setStyleSheet(
            f"color: {theme.TEXT}; font-weight: 600; border: none;")
        hint = QLabel("Ctrl + mouse wheel to zoom")
        hint.setStyleSheet(f"color: {theme.TEXT_FAINT}; border: none;")
        header_lay.addWidget(self._zoom_label)
        header_lay.addStretch(1)
        header_lay.addWidget(hint)
        self._header.hide()
        self._root.addWidget(self._header)

        self._host = QWidget(self)
        self._host.setAttribute(Qt.WA_NativeWindow)
        self._host.setStyleSheet(f"background: {theme.SURFACE};")
        self._root.addWidget(self._host)

        self._fallback = QLabel("Plugin has no editor / failed to open")
        self._fallback.setAlignment(Qt.AlignCenter)
        self._fallback.setStyleSheet(
            f"color: {theme.TEXT_DIM}; padding: 24px;"
            f" background: {theme.SURFACE};")
        self._fallback.hide()
        self._root.addWidget(self._fallback)

        self._loading_label = QLabel("Loading VST plugin\u2026")
        self._loading_label.setAlignment(Qt.AlignCenter)
        self._loading_label.setStyleSheet(
            f"color: {theme.TEXT_DIM}; padding: 24px;"
            f" background: {theme.SURFACE};")
        self._loading_label.hide()
        self._root.addWidget(self._loading_label)

        self._param_scroll = QScrollArea(self)
        self._param_scroll.setWidgetResizable(True)
        self._param_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._param_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._param_scroll.setStyleSheet(theme.thin_scroll_area_qss())
        self._param_scroll.hide()
        param_host = QWidget(self._param_scroll)
        self._param_layout = QVBoxLayout(param_host)
        self._param_panel = VstParamPanel(bridge, track_id, slot, parent=param_host)
        self._param_panel.param_changed.connect(self._on_param)
        self._param_layout.addWidget(self._param_panel)
        self._param_layout.addStretch(1)
        self._param_scroll.setWidget(param_host)
        self._root.addWidget(self._param_scroll, stretch=1)

        self._editor_open = False
        self._top_level_editor = False
        self._can_resize = False
        self._last_host_resize: tuple[int, int] = (0, 0)
        self._syncing_window = False
        self._host_hwnd = 0

        # Zoom state: scale multiplies the plugin's 100% ("base") size.
        self._zoom = ZoomState()

        self._resize_timer = QTimer(self)
        self._resize_timer.setInterval(100)
        self._resize_timer.timeout.connect(self._poll_resize)

        self._host_resize_timer = QTimer(self)
        self._host_resize_timer.setSingleShot(True)
        self._host_resize_timer.setInterval(50)
        self._host_resize_timer.timeout.connect(self._apply_host_resize)

        self._open_retry_timer = QTimer(self)
        self._open_retry_timer.setInterval(200)
        self._open_retry_timer.timeout.connect(self._try_open_editor)
        self._open_attempts = 0
        self._open_fail_attempts = 0

    # -- zoom persistence ---------------------------------------------------

    def _settings_key(self) -> str:
        return f"vst_zoom/{self._plugin_uid}"

    def _load_saved_scale(self) -> float:
        if not self._plugin_uid:
            return 1.0
        return clamp_zoom(QSettings("PyDAW", "PyDAW").value(
            self._settings_key(), 1.0))

    def _save_scale(self) -> None:
        if self._plugin_uid:
            QSettings("PyDAW", "PyDAW").setValue(
                self._settings_key(), self._zoom.scale)

    # -- geometry helpers -----------------------------------------------------

    def _dpr(self) -> float:
        dpr = float(self.devicePixelRatioF())
        # Before the dialog is fully mapped, QWidget can still report 1.0
        # while the screen is 125%/150%/200% — host ends up 2x the plugin.
        if dpr <= 1.01:
            screen = self.screen()
            if screen is not None:
                dpr = max(dpr, float(screen.devicePixelRatio()))
        return max(1.0, dpr)

    def _to_logical(self, w: int, h: int) -> tuple[int, int]:
        """Physical plugin pixels -> Qt logical pixels."""
        return to_logical(w, h, self._dpr())

    def _to_physical(self, w: int, h: int) -> tuple[int, int]:
        """Qt logical pixels -> physical plugin pixels."""
        return to_physical(w, h, self._dpr())

    def _header_h(self) -> int:
        return self._header.sizeHint().height() if self._header.isVisible() else 0

    # -- lifecycle ------------------------------------------------------------

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._editor_open:
            return
        self._open_attempts = 0
        self._open_fail_attempts = 0
        self._show_loading()
        # Defer until the native host HWND exists (showEvent alone is too
        # early on Windows; cross-process VST attach needs a realized widget).
        QTimer.singleShot(50, self._try_open_editor)

    def focus_editor(self) -> None:
        self.raise_()
        self.activateWindow()
        if self._top_level_editor and self._host_hwnd:
            self._bridge.vst_open_editor(
                self._track_id, self._slot, self._host_hwnd)

    def _show_loading(self) -> None:
        self._loading_label.show()
        self._fallback.hide()
        self._param_scroll.hide()
        self._host.hide()
        self._header.hide()

    def _try_open_editor(self) -> None:
        if self._editor_open:
            self._open_retry_timer.stop()
            return
        if self._bridge.vst_is_loading(self._track_id, self._slot):
            self._show_loading()
            self._open_attempts += 1
            # Heavy plugins can take tens of seconds; keep retrying.
            if self._open_attempts > 300:
                self._open_retry_timer.stop()
                self._show_open_failure("VST load timed out")
            elif not self._open_retry_timer.isActive():
                self._open_retry_timer.start()
            return
        self._open_retry_timer.stop()
        self._loading_label.hide()
        self._host.show()
        QApplication.processEvents(
            QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        hwnd = int(self._host.winId())
        if not hwnd:
            self._open_fail_attempts += 1
            if self._open_fail_attempts < 20:
                QTimer.singleShot(50, self._try_open_editor)
                return
            self._show_open_failure("Native host window is not ready")
            return
        self._host_hwnd = hwnd

        saved_scale = self._load_saved_scale()
        if saved_scale != 1.0:
            # Best-effort hint so supporting plugins attach already scaled.
            self._bridge.vst_set_content_scale(
                self._track_id, self._slot, saved_scale)

        if self._bridge.vst_open_editor(self._track_id, self._slot, hwnd):
            self._editor_open = True
            diagnostic = ""
            if hasattr(self._bridge, "vst_diagnostic"):
                diagnostic = self._bridge.vst_diagnostic(
                    self._track_id, self._slot).lower()
            if "separate vst host window" in diagnostic:
                self._show_top_level_editor()
                return
            self._header.show()
            self._host.show()
            self._fallback.hide()
            self._param_scroll.hide()
            self._can_resize = self._bridge.vst_editor_can_resize(
                self._track_id, self._slot)
            w, h = self._bridge.vst_editor_size(self._track_id, self._slot)

            content_scaled = False
            if saved_scale != 1.0:
                # Re-probe with the view alive: True means the plugin
                # accepted the factor, i.e. (w, h) is already scaled.
                content_scaled = self._bridge.vst_set_content_scale(
                    self._track_id, self._slot, saved_scale)
            self._zoom.init_from_open(w, h, content_scaled, saved_scale)
            if content_scaled:
                w, h = self._bridge.vst_editor_size(self._track_id, self._slot)

            if w > 0 and h > 0:
                self._setup_editor_layout(w, h)
            # Let Qt realize native geometry, then re-read plugin/HWND size.
            QApplication.processEvents()
            w, h = self._bridge.vst_editor_size(self._track_id, self._slot)
            if w > 0 and h > 0:
                self._setup_editor_layout(w, h)
            self._update_zoom_label()
            self._resize_timer.start()
            _WheelZoomHook.instance().register(self)

            if saved_scale != 1.0 and not content_scaled:
                # Plugin has no content-scale support: fall back to a
                # plain proportional resize once the layout settled.
                QTimer.singleShot(
                    50, lambda: self._apply_scale(saved_scale, save=False))
            self._open_fail_attempts = 0
        else:
            diagnostic = ""
            if hasattr(self._bridge, "vst_diagnostic"):
                diagnostic = self._bridge.vst_diagnostic(
                    self._track_id, self._slot).lower()
            if "native editor is disabled" in diagnostic:
                self._show_open_failure("Native editor is disabled")
                return
            self._open_fail_attempts += 1
            if self._open_fail_attempts < 15:
                QTimer.singleShot(200, self._try_open_editor)
            else:
                self._show_open_failure()

    def _show_open_failure(self, prefix: str | None = None) -> None:
        diagnostic = ""
        if hasattr(self._bridge, "vst_diagnostic"):
            diagnostic = self._bridge.vst_diagnostic(
                self._track_id, self._slot)
        msg = prefix or "Plugin has no editor / failed to open"
        if diagnostic:
            self._fallback.setText(msg + "\n\n" + diagnostic)
        else:
            self._fallback.setText(msg)
        self._host.hide()
        self._header.hide()
        self._fallback.show()
        self._param_scroll.show()
        self._root.setStretchFactor(self._host, 0)
        self._root.setStretchFactor(self._param_scroll, 1)
        self.adjustSize()

    def _show_top_level_editor(self) -> None:
        self._top_level_editor = True
        self._can_resize = False
        self._host.hide()
        self._header.hide()
        self._fallback.setText("Native editor is open in its own plugin window.")
        self._fallback.show()
        self._param_scroll.hide()
        self._root.setStretchFactor(self._host, 0)
        self._root.setStretchFactor(self._param_scroll, 0)
        self.setMinimumSize(320, 96)
        self.adjustSize()

    def _setup_editor_layout(self, w: int, h: int) -> None:
        """`w`/`h` are physical plugin pixels; window = header + working area."""
        lw, lh = self._to_logical(w, h)
        self._root.setContentsMargins(0, 0, 0, 0)
        self._root.setSpacing(0)
        self._last_host_resize = (w, h)
        hh = self._header_h()
        if self._can_resize:
            self._host.setMinimumSize(64, 64)
            self._host.setMaximumSize(16777215, 16777215)
            self._root.setStretchFactor(self._host, 1)
            self._syncing_window = True
            self.resize(lw, lh + hh)
            self._syncing_window = False
            self._apply_host_resize()
        else:
            self._host.setFixedSize(lw, lh)
            self._root.setStretchFactor(self._host, 0)
            self._syncing_window = True
            self.setFixedSize(lw, lh + hh)
            self._syncing_window = False

    # -- zoom -----------------------------------------------------------------

    def owns_native_window(self, hwnd: int) -> bool:
        """True when `hwnd` is our plugin host area or a child of it."""
        if not self._editor_open or not self._host_hwnd or not hwnd:
            return False
        if hwnd == self._host_hwnd:
            return True
        if sys.platform == "win32":
            return bool(_user32.IsChild(self._host_hwnd, hwnd))
        return False

    def zoom_step(self, direction: int) -> None:
        if not self._editor_open or self._top_level_editor:
            return
        target = self._zoom.step_target(direction)
        if target is None:
            return
        self._apply_scale(target)

    def _apply_scale(self, scale: float, save: bool = True) -> None:
        if not self._editor_open or self._top_level_editor:
            return
        if self._bridge.vst_set_content_scale(self._track_id, self._slot,
                                              scale):
            self._zoom.scale = scale
            w, h = self._bridge.vst_editor_size(self._track_id, self._slot)
        else:
            # No content-scale support: plain resize of the plugin view.
            want = self._zoom.target_size(scale)
            if (not self._can_resize or not self._zoom.has_base()
                    or not self._bridge.vst_resize_editor(
                        self._track_id, self._slot, *want)):
                self._zoom_label.setText(
                    f"Zoom: {round(self._zoom.scale * 100)}%"
                    " (not supported by plugin)")
                return
            self._zoom.scale = scale
            w, h = self._bridge.vst_editor_size(self._track_id, self._slot)
        if w > 0 and h > 0:
            self._last_host_resize = (w, h)
            self._sync_window_to_plugin_size(w, h)
        self._update_zoom_label()
        if save:
            self._save_scale()

    def _update_zoom_label(self) -> None:
        self._zoom_label.setText(f"Zoom: {round(self._zoom.scale * 100)}%")

    def wheelEvent(self, event) -> None:
        # Qt-reachable areas (header bar, margins); the native plugin
        # area is covered by the WH_MOUSE hook instead.
        if event.modifiers() & Qt.ControlModifier:
            delta = event.angleDelta().y()
            if delta:
                self.zoom_step(1 if delta > 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)

    # -- window <-> plugin size sync -------------------------------------------

    def _sizes_close(self, a: tuple[int, int], b: tuple[int, int]) -> bool:
        return sizes_close(a, b, self._dpr())

    def _sync_window_to_plugin_size(self, w: int, h: int) -> None:
        """Follow a plugin-initiated resize (`w`/`h` physical)."""
        if w <= 0 or h <= 0:
            return
        lw, lh = self._to_logical(w, h)
        hh = self._header_h()
        self._syncing_window = True
        try:
            if self._can_resize:
                self.resize(lw, lh + hh)
            else:
                self._host.setFixedSize(lw, lh)
                self.setFixedSize(lw, lh + hh)
        finally:
            self._syncing_window = False

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if (self._editor_open and self._can_resize and not self._top_level_editor
                and not self._syncing_window):
            self._host_resize_timer.start()

    def _apply_host_resize(self) -> None:
        if not self._editor_open or not self._can_resize or self._top_level_editor:
            return
        if self._host.width() <= 0 or self._host.height() <= 0:
            return
        requested = self._to_physical(self._host.width(), self._host.height())
        self._last_host_resize = requested
        if not self._bridge.vst_resize_editor(
                self._track_id, self._slot, *requested):
            return
        actual = self._bridge.vst_editor_size(self._track_id, self._slot)
        self._last_host_resize = actual
        if not self._sizes_close(actual, requested):
            # Plugin constrained the size: snap the window back to it.
            self._sync_window_to_plugin_size(actual[0], actual[1])

    def _poll_resize(self) -> None:
        if self._top_level_editor:
            return
        changed, w, h = self._bridge.vst_check_resize(
            self._track_id, self._slot)
        if not changed or w <= 0 or h <= 0:
            return
        plugin_size = (w, h)
        if self._sizes_close(plugin_size, self._last_host_resize):
            return
        self._last_host_resize = plugin_size
        self._sync_window_to_plugin_size(w, h)

    def _on_param(self, param_index: int, normalized: float) -> None:
        if self.route_params_externally:
            self.param_edited.emit(self._track_id, self._slot,
                                   param_index, normalized)
        else:  # standalone use: apply directly, no undo
            self._param_panel.apply_param(param_index, normalized)

    def refresh_params(self) -> None:
        """Mirror automated values into the generic slider fallback."""
        if self._param_scroll.isVisible():
            self._param_panel.refresh_from_engine()

    def rebuild_params(self) -> None:
        """Recreate the generic sliders after the plugin changed its
        parameter list (preset switch / dynamic GUI)."""
        self._param_panel.rebuild_params()

    def detach_editor(self) -> None:
        """Detach the native VST view from the host widget (must run on the
        UI thread before the C++ engine tears plugins down)."""
        if not self._editor_open:
            return
        self._bridge.vst_close_editor(self._track_id, self._slot)
        self._editor_open = False
        self._top_level_editor = False

    def closeEvent(self, event) -> None:
        _WheelZoomHook.instance().unregister(self)
        self._resize_timer.stop()
        self._host_resize_timer.stop()
        self._open_retry_timer.stop()
        self.detach_editor()
        super().closeEvent(event)
