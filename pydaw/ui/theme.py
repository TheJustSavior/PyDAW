"""Central color palette and application-wide stylesheet.

Every widget picks its colors from here so the whole app stays
consistent and readable. Apply with `apply_theme(app)` at startup.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QWidget

# -- palette (defaults = dark; overridden by set_theme) ----------------------

BG = "#131519"
PANEL = "#1b1e23"
SURFACE = "#22262c"
RAISED = "#2a2f37"
HOVER = "#343a44"
BORDER = "#3a414c"
BORDER_SOFT = "#2e343d"

TEXT = "#e8ebef"
TEXT_DIM = "#a5adba"
TEXT_FAINT = "#6f7887"

ACCENT = "#58a8f5"
ACCENT_HOVER = "#72b8ff"
ACCENT_MIDI = "#52d088"
ACCENT_MIDI_HOVER = "#6ae09a"
AMBER = "#e0aa42"
RED = "#e06060"
# Playhead follows the theme accent family so it matches the UI style
# (dark: bright accent-blue on dark lanes).
PLAYHEAD = "#72b8ff"
# Loop region gets its own themed tone (warm amber) so it reads apart
# from the accent-colored playhead and selection.
LOOP_REGION = "#e0aa42"

PIANO_ROW_WHITE = "#1e2128"
PIANO_ROW_BLACK = "#17191e"
PIANO_BAR_ALT = "#232830"
PIANO_GRID_BEAT = "#303540"
PIANO_GRID_BAR = "#464e5a"
PIANO_GHOST = "#08090c"

LANE = "#1e2127"
LANE_ALT = "#22252c"
LANE_MIDI = "#1d2622"
LANE_MIDI_ALT = "#202a25"
LANE_GROUP = "#26221b"
LANE_AUTO = "#181a20"
LANE_SEP = "#131519"
GRID_BEAT = "#2d3139"
GRID_BAR = "#3e444f"
NOTE_MIDI = "#45b872"
NOTE_MIDI_BORDER = "#8ee4ad"
NOTE_SELECTED = "#e8a838"
NOTE_SELECTED_BORDER = "#ffe8a8"

RADIUS = 5

# Per-track accent colors (distinct, readable on dark UI).
TRACK_COLORS: list[str] = [
    "#58a8f5",  # blue
    "#52d088",  # green
    "#e0aa42",  # amber
    "#c678dd",  # purple
    "#e06060",  # red
    "#56b6c2",  # cyan
    "#d19a66",  # orange
    "#98c379",  # lime
    "#61afef",  # sky
    "#e06c75",  # rose
    "#abb2bf",  # silver
    "#be5046",  # brick
    "#7ec699",  # mint
    "#6791e0",  # indigo
    "#d4a574",  # sand
    "#82aaff",  # periwinkle
]


def random_track_color() -> str:
    """Pick a random color from :data:`TRACK_COLORS`."""
    import random
    return random.choice(TRACK_COLORS)

COL_BG = QColor(BG)
COL_PANEL = QColor(PANEL)
COL_SURFACE = QColor(SURFACE)
COL_RAISED = QColor(RAISED)
COL_BORDER = QColor(BORDER)
COL_TEXT = QColor(TEXT)
COL_TEXT_DIM = QColor(TEXT_DIM)
COL_TEXT_FAINT = QColor(TEXT_FAINT)
COL_ACCENT = QColor(ACCENT)
COL_ACCENT_MIDI = QColor(ACCENT_MIDI)
COL_PLAYHEAD = QColor(PLAYHEAD)
COL_RED = QColor(RED)
COL_METER_OK = QColor(86, 197, 124)
COL_METER_HOT = QColor(217, 87, 87)
COL_NOTE_MIDI = QColor(NOTE_MIDI)
COL_NOTE_MIDI_BORDER = QColor(NOTE_MIDI_BORDER)
COL_NOTE_SELECTED = QColor(NOTE_SELECTED)
COL_NOTE_SELECTED_BORDER = QColor(NOTE_SELECTED_BORDER)
COL_PIANO_ROW_WHITE = QColor(PIANO_ROW_WHITE)
COL_PIANO_ROW_BLACK = QColor(PIANO_ROW_BLACK)
COL_PIANO_BAR_ALT = QColor(PIANO_BAR_ALT)

_STR_KEYS = (
    "BG", "PANEL", "SURFACE", "RAISED", "HOVER", "BORDER", "BORDER_SOFT",
    "TEXT", "TEXT_DIM", "TEXT_FAINT",
    "ACCENT", "ACCENT_HOVER", "ACCENT_MIDI", "ACCENT_MIDI_HOVER",
    "AMBER", "RED", "PLAYHEAD", "LOOP_REGION",
    "PIANO_ROW_WHITE", "PIANO_ROW_BLACK", "PIANO_BAR_ALT",
    "PIANO_GRID_BEAT", "PIANO_GRID_BAR", "PIANO_GHOST",
    "NOTE_MIDI", "NOTE_MIDI_BORDER", "NOTE_SELECTED", "NOTE_SELECTED_BORDER",
    "LANE", "LANE_ALT", "LANE_MIDI", "LANE_MIDI_ALT", "LANE_GROUP", "LANE_AUTO",
    "LANE_SEP", "GRID_BEAT", "GRID_BAR",
)

_DARK: dict[str, str] = {k: globals()[k] for k in _STR_KEYS}

_LIGHT: dict[str, str] = {
    "BG": "#e4e7ec",
    "PANEL": "#eef0f4",
    "SURFACE": "#ffffff",
    "RAISED": "#e2e6ec",
    "HOVER": "#d4dae3",
    "BORDER": "#b8c0cc",
    "BORDER_SOFT": "#cdd3dc",
    "TEXT": "#1a1f28",
    "TEXT_DIM": "#4a5568",
    "TEXT_FAINT": "#7a8494",
    "ACCENT": "#3580d4",
    "ACCENT_HOVER": "#4a90e8",
    "ACCENT_MIDI": "#2ea86a",
    "ACCENT_MIDI_HOVER": "#45b872",
    "AMBER": "#c99220",
    "RED": "#d04545",
    # Deep accent-blue: readable on the light lanes, clearly different
    # from the dark theme's bright playhead.
    "PLAYHEAD": "#1f5fae",
    # Darker amber than the dark theme's: keeps contrast on light lanes.
    "LOOP_REGION": "#b57a10",
    "PIANO_ROW_WHITE": "#f5f7fa",
    "PIANO_ROW_BLACK": "#e8ebf0",
    "PIANO_BAR_ALT": "#eef1f5",
    "PIANO_GRID_BEAT": "#d0d6e0",
    "PIANO_GRID_BAR": "#b0b8c6",
    "PIANO_GHOST": "#fafbfc",
    "NOTE_MIDI": "#38a868",
    "NOTE_MIDI_BORDER": "#6ac890",
    "NOTE_SELECTED": "#d89020",
    "NOTE_SELECTED_BORDER": "#f0c060",
    "LANE": "#f2f4f8",
    "LANE_ALT": "#eaedf2",
    "LANE_MIDI": "#ecf5ef",
    "LANE_MIDI_ALT": "#e3efe8",
    "LANE_GROUP": "#f3efe4",
    "LANE_AUTO": "#e8eaef",
    "LANE_SEP": "#c8cfd9",
    "GRID_BEAT": "#d8dde5",
    "GRID_BAR": "#b6bfcc",
}

_COL_MAP = (
    ("COL_BG", "BG"),
    ("COL_PANEL", "PANEL"),
    ("COL_SURFACE", "SURFACE"),
    ("COL_RAISED", "RAISED"),
    ("COL_BORDER", "BORDER"),
    ("COL_TEXT", "TEXT"),
    ("COL_TEXT_DIM", "TEXT_DIM"),
    ("COL_TEXT_FAINT", "TEXT_FAINT"),
    ("COL_ACCENT", "ACCENT"),
    ("COL_ACCENT_MIDI", "ACCENT_MIDI"),
    ("COL_PLAYHEAD", "PLAYHEAD"),
    ("COL_RED", "RED"),
    ("COL_NOTE_MIDI", "NOTE_MIDI"),
    ("COL_NOTE_MIDI_BORDER", "NOTE_MIDI_BORDER"),
    ("COL_NOTE_SELECTED", "NOTE_SELECTED"),
    ("COL_NOTE_SELECTED_BORDER", "NOTE_SELECTED_BORDER"),
    ("COL_PIANO_ROW_WHITE", "PIANO_ROW_WHITE"),
    ("COL_PIANO_ROW_BLACK", "PIANO_ROW_BLACK"),
    ("COL_PIANO_BAR_ALT", "PIANO_BAR_ALT"),
)

_current_theme = "dark"


def set_theme(name: str) -> None:
    """Switch module-level palette constants (call before building QSS)."""
    global _current_theme
    palette = _LIGHT if name == "light" else _DARK
    _current_theme = "light" if name == "light" else "dark"
    g = globals()
    for key in _STR_KEYS:
        g[key] = palette[key]
    for col_name, src in _COL_MAP:
        g[col_name] = QColor(g[src])


def drag_highlight_color() -> QColor:
    """Translucent overlay that brightens a widget's drag preview.

    White works for both palettes (dark cards get lighter, light cards get
    whiter); the light theme needs more alpha because its cards are already
    close to white.
    """
    return QColor(255, 255, 255, 70 if _current_theme == "light" else 34)


def thin_scrollbar_qss(*, width: int = 8, vertical: bool = True) -> str:
    """QSS for a thin themed scrollbar (transparent track, themed handle).

    Uses current module-level palette constants so light/dark themes apply
    when the stylesheet is built or refreshed.
    """
    radius = max(2, width // 2)
    if vertical:
        return (
            f"QScrollBar:vertical {{ background: transparent; width: {width}px;"
            f" margin: 0; border: none; }}"
            f"QScrollBar::handle:vertical {{ background: {RAISED};"
            f" border-radius: {radius}px; min-height: 24px; }}"
            f"QScrollBar::handle:vertical:hover {{ background: {HOVER}; }}"
            f"QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical"
            f" {{ height: 0; }}"
            f"QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical"
            f" {{ background: transparent; }}"
        )
    return (
        f"QScrollBar:horizontal {{ background: transparent; height: {width}px;"
        f" margin: 0; border: none; }}"
        f"QScrollBar::handle:horizontal {{ background: {RAISED};"
        f" border-radius: {radius}px; min-width: 24px; }}"
        f"QScrollBar::handle:horizontal:hover {{ background: {HOVER}; }}"
        f"QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal"
        f" {{ width: 0; }}"
        f"QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal"
        f" {{ background: transparent; }}"
    )


def thin_scroll_area_qss(*, width: int = 8) -> str:
    """Borderless scroll area with a thin vertical scrollbar."""
    return (
        f"QScrollArea {{ border: none; background: transparent; }}"
        f"{thin_scrollbar_qss(width=width, vertical=True)}"
    )


def _qss() -> str:
    r = RADIUS
    return f"""
QWidget {{
    background: {PANEL};
    color: {TEXT};
    font-size: 12px;
}}
QMainWindow, QDialog {{ background: {PANEL}; }}

QMenuBar {{
    background: {PANEL};
    color: {TEXT_DIM};
    border-bottom: 1px solid {BORDER_SOFT};
    padding: 1px 4px;
}}
QMenuBar::item {{ padding: 5px 10px; background: transparent; border-radius: {r}px; }}
QMenuBar::item:selected {{ background: {HOVER}; color: {TEXT}; }}

QMenu {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 4px;
    border-radius: {r}px;
}}
QMenu::item {{ padding: 5px 26px 5px 14px; border-radius: {r}px; }}
QMenu::item:selected {{ background: {ACCENT}; color: white; }}
QMenu::item:disabled {{ color: {TEXT_FAINT}; }}
QMenu::separator {{ height: 1px; background: {BORDER_SOFT}; margin: 4px 8px; }}

QPushButton {{
    background: {RAISED};
    color: {TEXT_DIM};
    border: 1px solid {BORDER_SOFT};
    border-radius: {r}px;
    padding: 4px 12px;
}}
QPushButton:hover {{ background: {HOVER}; color: {TEXT}; border-color: {BORDER}; }}
QPushButton:pressed {{ background: {ACCENT_HOVER}; color: white; border-color: {ACCENT}; }}
QPushButton:checked {{
    background: {ACCENT};
    color: white;
    border-color: {ACCENT_HOVER};
}}
QPushButton:checked:hover {{ background: {ACCENT_HOVER}; }}
QPushButton:disabled {{ color: {TEXT_FAINT}; background: {SURFACE}; }}

QLineEdit {{
    background: {RAISED};
    color: {TEXT};
    border: 1px solid {BORDER_SOFT};
    border-radius: {r}px;
    padding: 3px 6px;
    selection-background-color: {ACCENT};
}}
QLineEdit:hover {{ border-color: {BORDER}; }}
QLineEdit:focus {{ border-color: {ACCENT}; }}

QComboBox {{
    background: {RAISED};
    color: {TEXT};
    border: 1px solid {BORDER_SOFT};
    border-radius: {r}px;
    padding: 3px 8px;
}}
QComboBox:hover {{ background: {HOVER}; border-color: {BORDER}; }}
QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {TEXT_DIM};
    margin-right: 6px;
}}
QComboBox QAbstractItemView {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
    selection-color: white;
    outline: none;
    border-radius: {r}px;
}}

QDoubleSpinBox, QSpinBox {{
    background: {RAISED};
    color: {TEXT};
    border: 1px solid {BORDER_SOFT};
    border-radius: {r}px;
    padding: 2px 4px;
    selection-background-color: {ACCENT};
}}
QDoubleSpinBox:hover, QSpinBox:hover {{ border-color: {BORDER}; }}
QDoubleSpinBox:focus, QSpinBox:focus {{ border-color: {ACCENT}; }}
QDoubleSpinBox::up-button, QSpinBox::up-button,
QDoubleSpinBox::down-button, QSpinBox::down-button {{
    background: {HOVER};
    border: none;
    width: 15px;
    border-radius: 2px;
}}
QDoubleSpinBox::up-arrow, QSpinBox::up-arrow {{
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
    border-bottom: 4px solid {TEXT_DIM};
}}
QDoubleSpinBox::down-arrow, QSpinBox::down-arrow {{
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
    border-top: 4px solid {TEXT_DIM};
}}

QSlider::groove:horizontal {{
    height: 4px;
    background: {RAISED};
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    width: 12px; height: 12px;
    margin: -5px 0;
    border-radius: 6px;
    background: {TEXT};
}}
QSlider::handle:horizontal:hover {{ background: white; }}
QSlider::groove:vertical {{
    width: 4px;
    background: {RAISED};
    border-radius: 2px;
}}
QSlider::add-page:vertical {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:vertical {{
    height: 12px; width: 12px;
    margin: 0 -5px;
    border-radius: 6px;
    background: {TEXT};
}}
QSlider::handle:vertical:hover {{ background: white; }}

QScrollBar:horizontal {{
    background: {PANEL}; height: 11px; margin: 0;
}}
QScrollBar:vertical {{
    background: {PANEL}; width: 11px; margin: 0;
}}
QScrollBar::handle {{
    background: {RAISED}; border-radius: {r}px; min-width: 24px; min-height: 24px;
    margin: 2px;
}}
QScrollBar::handle:hover {{ background: {BORDER}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QToolTip {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 4px 8px;
    border-radius: {r}px;
    /* Tooltips inherit the spawning widget's font (e.g. the 20px monospace
       transport clock) unless pinned here explicitly. */
    font-family: "Segoe UI", sans-serif;
    font-size: 11px;
    font-weight: normal;
}}

QStatusBar {{
    background: {PANEL};
    color: {TEXT_DIM};
    border-top: 1px solid {BORDER_SOFT};
}}

QScrollArea {{ border: none; }}
QGraphicsView {{ border: none; }}

QAbstractItemView {{
    background: {SURFACE};
    alternate-background-color: {RAISED};
    color: {TEXT};
    border: 1px solid {BORDER_SOFT};
    border-radius: {r}px;
    gridline-color: {BORDER_SOFT};
    selection-background-color: {ACCENT};
    selection-color: white;
    outline: none;
}}
QAbstractItemView::item {{ padding: 3px 6px; }}
QAbstractItemView::item:hover {{ background: {HOVER}; }}
QAbstractItemView::item:selected {{ background: {ACCENT}; color: white; }}

QHeaderView {{ background: {PANEL}; border: none; }}
QHeaderView::section {{
    background: {PANEL};
    color: {TEXT_DIM};
    border: none;
    border-bottom: 1px solid {BORDER_SOFT};
    border-right: 1px solid {BORDER_SOFT};
    padding: 5px 8px;
    font-weight: bold;
}}
QHeaderView::section:last {{ border-right: none; }}
QTableCornerButton::section {{
    background: {PANEL};
    border: none;
    border-bottom: 1px solid {BORDER_SOFT};
}}

QGroupBox {{
    border: 1px solid {BORDER_SOFT};
    border-radius: {r}px;
    margin-top: 10px;
    padding-top: 4px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 8px;
    padding: 0 4px;
    color: {TEXT_DIM};
}}

QDialogButtonBox QPushButton {{ min-width: 72px; }}

QMessageBox {{ background: {PANEL}; }}
QMessageBox QLabel {{ background: transparent; color: {TEXT}; }}
QMessageBox QLabel#qt_msgbox_label {{
    padding: 8px 12px 8px 4px;
    min-width: 240px;
    font-size: 12px;
}}
QMessageBox QLabel#qt_msgbox_informativelabel {{
    padding: 0 12px 8px 4px;
    color: {TEXT_DIM};
}}
QMessageBox QPushButton {{ min-width: 84px; padding: 5px 14px; }}

QInputDialog {{ background: {PANEL}; }}
QInputDialog QLabel {{ background: transparent; }}
"""


# -- native (Windows) title bar theming ---------------------------------------

def _set_win_titlebar_dark(widget: QWidget, dark: bool) -> None:
    """Ask DWM to draw a dark title bar for one top-level window."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = int(widget.winId())
        value = ctypes.c_int(1 if dark else 0)
        # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE (Win10 1903+/Win11)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:  # noqa: BLE001 - cosmetic only, never break the UI
        pass


class _TitleBarThemer(QObject):
    """App-wide event filter: every top-level window (dialogs, message
    boxes, popup windows) gets a title bar matching the current theme."""

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() == QEvent.Show and isinstance(obj, QWidget) \
                and obj.isWindow():
            _set_win_titlebar_dark(obj, _current_theme == "dark")
        return False


_titlebar_themer: _TitleBarThemer | None = None


def _palette() -> QPalette:
    """App-wide palette so native bits (item views, check indicators,
    disabled states) follow the theme instead of Fusion defaults."""
    p = QPalette()
    p.setColor(QPalette.Window, QColor(PANEL))
    p.setColor(QPalette.WindowText, QColor(TEXT))
    p.setColor(QPalette.Base, QColor(SURFACE))
    p.setColor(QPalette.AlternateBase, QColor(RAISED))
    p.setColor(QPalette.Text, QColor(TEXT))
    p.setColor(QPalette.PlaceholderText, QColor(TEXT_FAINT))
    p.setColor(QPalette.Button, QColor(RAISED))
    p.setColor(QPalette.ButtonText, QColor(TEXT))
    p.setColor(QPalette.BrightText, QColor(RED))
    p.setColor(QPalette.Highlight, QColor(ACCENT))
    p.setColor(QPalette.HighlightedText, QColor("white"))
    p.setColor(QPalette.Link, QColor(ACCENT))
    p.setColor(QPalette.ToolTipBase, QColor(SURFACE))
    p.setColor(QPalette.ToolTipText, QColor(TEXT))
    p.setColor(QPalette.Light, QColor(HOVER))
    p.setColor(QPalette.Midlight, QColor(BORDER_SOFT))
    p.setColor(QPalette.Mid, QColor(BORDER))
    p.setColor(QPalette.Dark, QColor(BG))
    p.setColor(QPalette.Shadow, QColor(BG))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor(TEXT_FAINT))
    p.setColor(QPalette.Disabled, QPalette.Highlight, QColor(BORDER))
    p.setColor(QPalette.Disabled, QPalette.HighlightedText, QColor(TEXT_DIM))
    return p


def apply_theme(app: QApplication, name: str = "dark") -> None:
    global _titlebar_themer
    set_theme(name)
    app.setStyle("Fusion")
    app.setPalette(_palette())
    font = QFont("Segoe UI", 10)
    font.setHintingPreference(QFont.PreferFullHinting)
    app.setFont(font)
    app.setStyleSheet(_qss())
    if _titlebar_themer is None:
        _titlebar_themer = _TitleBarThemer(app)
        app.installEventFilter(_titlebar_themer)
