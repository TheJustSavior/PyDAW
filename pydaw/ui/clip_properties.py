"""Clip Properties dialog: rename a clip and (for audio clips) pick its
warp (time-stretch) method. Opened from the clip's context menu."""

from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QFormLayout, QLabel,
                               QLineEdit, QVBoxLayout)

from ..audio_stretch import WARP_LABELS, WARP_METHODS


class ClipPropertiesDialog(QDialog):
    """Modal editor for clip properties.

    Audio clips expose name + warp method; MIDI clips only the name
    (`midi=True`). Returns the new values through `values()` after
    `exec()` accepts.
    """

    def __init__(self, name: str, warp: str = "stretch",
                 stretch_ratio: float = 1.0, auto_warp: bool = True,
                 *, midi: bool = False, parent=None) -> None:
        super().__init__(parent)
        self._midi = midi
        self.setWindowTitle("Clip properties")
        self.setMinimumWidth(320)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self._name_edit = QLineEdit(name, self)
        form.addRow("Name:", self._name_edit)

        self._warp_combo = QComboBox(self)
        if not midi:
            for method in WARP_METHODS:
                self._warp_combo.addItem(WARP_LABELS[method], method)
            idx = self._warp_combo.findData(warp)
            self._warp_combo.setCurrentIndex(max(0, idx))
            form.addRow("Warp:", self._warp_combo)

            ratio_label = QLabel(f"{stretch_ratio:.3f}x", self)
            form.addRow("Stretch:", ratio_label)

            self._auto_warp_check = QCheckBox(
                "Auto-warp on tempo change", self)
            self._auto_warp_check.setChecked(auto_warp)
            self._auto_warp_check.setToolTip(
                "Re-warp this clip automatically when the project BPM "
                "changes (active only while Snap is enabled).")
            form.addRow("", self._auto_warp_check)

            hint = QLabel(
                "Ctrl+drag a clip edge to time-stretch it with this method.",
                self)
            hint.setWordWrap(True)
            hint.setStyleSheet("color: #888;")
            layout.addWidget(hint)
        else:
            self._warp_combo.hide()
            self._auto_warp_check = None

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._name_edit.setFocus()
        self._name_edit.selectAll()

    def values(self) -> tuple[str, str, bool]:
        """(name, warp_method, auto_warp) chosen by the user
        (warp is "" and auto_warp True for MIDI)."""
        return (self._name_edit.text().strip(),
                self._warp_combo.currentData() if not self._midi else "",
                (self._auto_warp_check.isChecked()
                 if self._auto_warp_check is not None else True))
