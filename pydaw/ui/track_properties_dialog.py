"""Modal dialog for editing track name and color."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QColorDialog, QDialog, QDialogButtonBox,
                               QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QVBoxLayout)

from ..model.project import Track
from . import theme


class TrackPropertiesDialog(QDialog):
    """Edit a track's display name and accent color."""

    SWATCH_SIZE = 22

    def __init__(self, track: Track, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Track properties")
        self.setModal(True)
        self.resize(360, 220)
        self._track = track
        self._selected_color = track.color

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name"))
        self._name_edit = QLineEdit(track.name)
        self._name_edit.selectAll()
        name_row.addWidget(self._name_edit, 1)
        layout.addLayout(name_row)

        layout.addWidget(QLabel("Color"))
        swatch_grid = QGridLayout()
        swatch_grid.setSpacing(4)
        self._swatch_buttons: list[QPushButton] = []
        cols = 8
        for i, hex_color in enumerate(theme.TRACK_COLORS):
            btn = QPushButton()
            btn.setFixedSize(self.SWATCH_SIZE, self.SWATCH_SIZE)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setProperty("hex_color", hex_color)
            btn.clicked.connect(lambda _=False, c=hex_color: self._pick_palette(c))
            swatch_grid.addWidget(btn, i // cols, i % cols)
            self._swatch_buttons.append(btn)
        layout.addLayout(swatch_grid)

        custom_row = QHBoxLayout()
        custom_row.addStretch(1)
        custom_btn = QPushButton("Custom\u2026")
        custom_btn.clicked.connect(self._pick_custom)
        custom_row.addWidget(custom_btn)
        layout.addLayout(custom_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._refresh_swatches()

    def result_values(self) -> tuple[str, str]:
        """Return ``(name, color)`` after the dialog is accepted."""
        return self._name_edit.text().strip() or self._track.name, \
            self._selected_color

    def _pick_palette(self, color: str) -> None:
        self._selected_color = color
        self._refresh_swatches()

    def _pick_custom(self) -> None:
        initial = QColor(self._selected_color)
        color = QColorDialog.getColor(initial, self, "Custom track color")
        if not color.isValid():
            return
        self._selected_color = color.name(QColor.HexRgb)
        self._refresh_swatches()

    def _refresh_swatches(self) -> None:
        for btn in self._swatch_buttons:
            hex_color = btn.property("hex_color")
            border = theme.ACCENT if hex_color == self._selected_color \
                else theme.BORDER_SOFT
            btn.setStyleSheet(
                f"QPushButton {{ background: {hex_color};"
                f" border: 2px solid {border}; border-radius: 3px; }}"
                f"QPushButton:hover {{ border-color: {theme.ACCENT_HOVER}; }}")
