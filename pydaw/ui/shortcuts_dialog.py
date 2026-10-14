"""Modal reference dialog listing keyboard shortcuts."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHeaderView,
                               QTableWidget, QTableWidgetItem, QVBoxLayout)

from . import theme
from .i18n import tr


def shortcut_sections() -> list[tuple[str, list[tuple[str, str]]]]:
    """(section title, [(action, shortcut), ...]) in the active language."""
    return [
        (tr("Transport"), [
            (tr("Play/Pause"), "Space"),
            (tr("Stop"), "Ctrl+."),
            (tr("Metronome"), "Ctrl+M"),
            (tr("Loop (with clips selected — region over them)"), "Ctrl+L"),
            (tr("Mixer"), "Ctrl+Shift+M"),
        ]),
        (tr("Selection"), [
            (tr("Select all clips"), "Ctrl+A"),
            (tr("Deselect all"), "Ctrl+Shift+A"),
            (tr("Invert selection"), "Ctrl+Shift+I"),
            (tr("Select all on track"), "Ctrl+Alt+A"),
            (tr("Select row of clips on a track"), tr("Shift + click")),
            (tr("Add clip to selection"),
             tr("Ctrl + click / Shift + click")),
        ]),
        (tr("Timeline"), [
            (tr("Delete clip"), "Delete"),
            (tr("Duplicate selection (as one block)"), "Ctrl+D"),
            (tr("Copy clips"), "Ctrl+C"),
            (tr("Paste clips at playhead"), "Ctrl+V"),
            (tr("Split at playhead"), "Ctrl+E"),
            (tr("Join clips on one lane"), "Ctrl+J"),
            (tr("Mouse tool"), "1"),
            (tr("Selection tool"), "2"),
            (tr("Rubber-band select"), tr("Selection tool drag")),
            (tr("Slice tool"), "C"),
            (tr("Exit slice tool"), "Esc / C"),
            (tr("Jump to start"), "Home"),
            (tr("Jump to end"), "End"),
            (tr("Zoom to fit (selection or whole project)"), "F"),
            (tr("Zoom in"), "+ / = / Ctrl++"),
            (tr("Zoom out"), "- / Ctrl+-"),
            (tr("Horizontal scroll"), tr("Shift + wheel")),
            (tr("Vertical scroll"), tr("Wheel / Alt + wheel")),
            (tr("Zoom"), tr("Ctrl + wheel")),
            (tr("Pan"), tr("MMB drag")),
        ]),
        (tr("Piano roll"), [
            (tr("Select all"), "Ctrl+A"),
            (tr("Copy"), "Ctrl+C"),
            (tr("Paste"), "Ctrl+V"),
            (tr("Duplicate"), "Ctrl+D"),
            (tr("Delete notes"), "Delete"),
            (tr("Mouse tool"), "1"),
            (tr("Selection tool"), "2"),
            (tr("Rubber-band select"), tr("Select tool / Ctrl + drag")),
            (tr("Slice tool"), "C"),
            (tr("Resize without snap"), tr("Ctrl + drag edge")),
            (tr("Snap on/off"),
             tr("Snap button in the piano roll window")),
            (tr("Full list"), tr("Help ▾ in the piano roll window")),
        ]),
        (tr("Tracks"), [
            (tr("Add audio track (under selected clip)"), "Ctrl+T"),
            (tr("Add MIDI track (under selected clip)"), "Ctrl+Shift+T"),
            (tr("Add group track"), "Ctrl+Shift+G"),
            (tr("Group selected tracks"), "Ctrl+G"),
            (tr("Rename track"), "Ctrl+Shift+R"),
            (tr("Rename selected clip"), "Ctrl+R"),
            (tr("Delete selected track"),
             tr("Delete (when left header is selected)")),
        ]),
        (tr("General"), [
            (tr("Undo"), "Ctrl+Z"),
            (tr("Redo"), "Ctrl+Y"),
            (tr("New project"), "Ctrl+N"),
            (tr("Open project"), "Ctrl+O"),
            (tr("Save project"), "Ctrl+S"),
            (tr("Settings"), "Ctrl+,"),
            (tr("Hotkeys reference"), "F1"),
        ]),
    ]


class ShortcutsDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Keyboard shortcuts"))
        self.resize(520, 520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)

        table = QTableWidget()
        table.setColumnCount(2)
        table.setHorizontalHeaderLabels([tr("Action"), tr("Shortcut")])
        table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionMode(QTableWidget.NoSelection)
        table.setFocusPolicy(Qt.NoFocus)
        table.setShowGrid(False)
        table.setAlternatingRowColors(True)  # colors come from the app theme

        rows: list[tuple[str, str, bool]] = []
        for section, items in shortcut_sections():
            rows.append((section, "", True))
            rows.extend((action, shortcut, False) for action, shortcut in items)

        table.setRowCount(len(rows))
        for row, (label, shortcut, is_header) in enumerate(rows):
            action_item = QTableWidgetItem(label)
            shortcut_item = QTableWidgetItem(shortcut)
            if is_header:
                action_item.setBackground(theme.COL_PANEL)
                shortcut_item.setBackground(theme.COL_PANEL)
                font = action_item.font()
                font.setBold(True)
                action_item.setFont(font)
                action_item.setForeground(theme.COL_TEXT)
                shortcut_item.setForeground(theme.COL_TEXT_DIM)
            else:
                action_item.setForeground(theme.COL_TEXT)
                shortcut_item.setForeground(theme.COL_ACCENT)
            table.setItem(row, 0, action_item)
            table.setItem(row, 1, shortcut_item)

        layout.addWidget(table)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
