"""Tweak-mode confirmation dialog.

Shown when Tweak is armed and the user touches a knob/slider: offers to
create an automation lane for that parameter. Styled from the theme
palette (read at construction time, so it always matches the active
light/dark theme).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout)

from . import theme
from .i18n import tr


class TweakDialog(QDialog):
    """`exec()` returns QDialog.Accepted when the user wants the lane."""

    def __init__(self, param_name: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Tweak")
        self.setModal(True)
        self.setMinimumWidth(320)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)

        title = QLabel(tr("Add automation lane?"), self)
        title.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 14px; font-weight: bold;"
            " background: transparent;")
        layout.addWidget(title)

        # The touched parameter, highlighted in the Tweak (amber) accent.
        chip = QLabel(param_name, self)
        chip.setAlignment(Qt.AlignCenter)
        chip.setWordWrap(True)
        chip.setStyleSheet(
            f"color: {theme.AMBER}; background: {theme.SURFACE};"
            f" border: 1px solid {theme.BORDER_SOFT};"
            f" border-left: 3px solid {theme.AMBER};"
            f" border-radius: {theme.RADIUS}px;"
            " padding: 8px 12px; font-size: 12px; font-weight: bold;")
        layout.addWidget(chip)

        hint = QLabel(
            tr("A lane for this parameter will appear under the track."),
            self)
        hint.setWordWrap(True)
        hint.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; font-size: 11px;"
            " background: transparent;")
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        cancel_btn = QPushButton(tr("Cancel"), self)
        cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(cancel_btn)
        add_btn = QPushButton(tr("Add lane"), self)
        add_btn.setDefault(True)
        add_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.ACCENT}; color: {theme.BG};"
            f" font-weight: bold; border: none;"
            f" border-radius: {theme.RADIUS}px; padding: 6px 16px; }}"
            f"QPushButton:hover {{ background: {theme.ACCENT_HOVER}; }}")
        add_btn.clicked.connect(self.accept)
        buttons.addWidget(add_btn)
        layout.addLayout(buttons)

        add_btn.setFocus()
