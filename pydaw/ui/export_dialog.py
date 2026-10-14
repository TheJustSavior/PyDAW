"""Export dialog: formats with bitrate options, range and track selection."""

from __future__ import annotations

import os
import re
import tempfile

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QRadioButton,
                               QVBoxLayout)

from .i18n import tr, trf

# fmt key -> (label, file extension, [(option label, option value), ...])
FORMATS: dict[str, tuple[str, str, list[tuple[str, str]]]] = {
    "wav": ("WAV", "wav", [
        ("32-bit float", "FLOAT"),
        ("24-bit PCM", "PCM_24"),
        ("16-bit PCM", "PCM_16"),
    ]),
    "flac": ("FLAC", "flac", [
        ("24-bit", "PCM_24"),
        ("16-bit", "PCM_16"),
    ]),
    "mp3": ("MP3", "mp3", [
        ("320 kbps", "320"),
        ("256 kbps", "256"),
        ("192 kbps", "192"),
        ("128 kbps", "128"),
    ]),
}

MASTER_ID = -1  # pseudo track id for the full mix


class ExportDialog(QDialog):
    """Pick formats (with per-format quality), range and tracks; the actual
    render is done by the caller (MainWindow) after the dialog is accepted."""

    def __init__(self, project, has_loop: bool, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Export audio"))
        self.setModal(True)
        self.resize(460, 520)

        fmt_box = QGroupBox(tr("Formats"))
        grid = QGridLayout(fmt_box)
        self._fmt_checks: dict[str, QCheckBox] = {}
        self._fmt_combos: dict[str, QComboBox] = {}
        for row, (key, (label, _ext, options)) in enumerate(FORMATS.items()):
            check = QCheckBox(label)
            combo = QComboBox()
            for opt_label, opt_value in options:
                combo.addItem(tr(opt_label), opt_value)
            combo.setEnabled(False)
            check.toggled.connect(combo.setEnabled)
            grid.addWidget(check, row, 0)
            grid.addWidget(combo, row, 1)
            self._fmt_checks[key] = check
            self._fmt_combos[key] = combo
        self._fmt_checks["wav"].setChecked(True)

        range_box = QGroupBox(tr("Range"))
        range_lay = QVBoxLayout(range_box)
        self.range_all = QRadioButton(tr(
            "Full arrangement (to end of last clip)"))
        self.range_all.setChecked(True)
        self.range_loop = QRadioButton(tr("Loop region"))
        self.range_loop.setEnabled(has_loop)
        if not has_loop:
            self.range_loop.setToolTip(tr(
                "No loop region — set one on the timeline ruler"))
        range_lay.addWidget(self.range_all)
        range_lay.addWidget(self.range_loop)

        tracks_box = QGroupBox(tr("Tracks (each — separate file)"))
        tracks_lay = QVBoxLayout(tracks_box)
        self.track_list = QListWidget()
        self.track_list.setSelectionMode(QListWidget.NoSelection)

        master_item = QListWidgetItem(tr("Master (full mix)"))
        master_item.setData(Qt.UserRole, MASTER_ID)
        master_item.setFlags(master_item.flags() | Qt.ItemIsUserCheckable)
        master_item.setCheckState(Qt.Checked)
        self.track_list.addItem(master_item)

        for track in project.tracks:
            prefix = "    " if track.group_id else ""
            kind = {"audio": tr("AUDIO"), "midi": tr("MIDI"),
                    "group": tr("GROUP")}.get(track.type, "?")
            item = QListWidgetItem(f"{prefix}{track.name}  [{kind}]")
            item.setData(Qt.UserRole, track.id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.track_list.addItem(item)
        tracks_lay.addWidget(self.track_list)

        self._note = QLabel(tr(
            "Files: «name.ext» for Master, "
            "«name_track.ext» for other tracks."))
        self._note.setWordWrap(True)
        self._note.setStyleSheet("color: gray;")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok |
                                   QDialogButtonBox.Cancel)
        self._ok_btn = buttons.button(QDialogButtonBox.Ok)
        self._ok_btn.setText(tr("Export..."))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        for check in self._fmt_checks.values():
            check.toggled.connect(self._update_ok)
        self.track_list.itemChanged.connect(lambda _i: self._update_ok())

        layout = QVBoxLayout(self)
        layout.addWidget(fmt_box)
        layout.addWidget(range_box)
        layout.addWidget(tracks_box)
        layout.addWidget(self._note)
        layout.addWidget(buttons)

    def _update_ok(self) -> None:
        self._ok_btn.setEnabled(bool(self.formats) and bool(self.track_ids))

    @property
    def formats(self) -> list[tuple[str, str]]:
        """Checked formats as (fmt_key, option_value)."""
        result = []
        for key, check in self._fmt_checks.items():
            if check.isChecked():
                result.append((key, str(self._fmt_combos[key].currentData())))
        return result

    @property
    def track_ids(self) -> list[int]:
        """Checked tracks; MASTER_ID (-1) means the full mix."""
        result = []
        for i in range(self.track_list.count()):
            item = self.track_list.item(i)
            if item.checkState() == Qt.Checked:
                result.append(int(item.data(Qt.UserRole)))
        return result

    @property
    def use_loop(self) -> bool:
        return self.range_loop.isChecked()


def safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w\- ]+", "_", name, flags=re.UNICODE).strip()
    return re.sub(r"\s+", "_", cleaned) or "track"


def export_mix_to_file(bridge, path: str, fmt: str, option: str,
                       start_frame: int = 0, end_frame: int = -1) -> bool:
    """Render the current engine mix to `path` in `fmt`.

    `option` is a subtype (WAV/FLAC: FLOAT/PCM_24/PCM_16) or an MP3
    bitrate in kbps. The engine renders 32-bit float WAV; everything else
    is transcoded from a temporary WAV via soundfile (libsndfile).
    Raises RuntimeError with a readable message if the encoder is missing.
    """
    if fmt == "wav" and option == "FLOAT":
        return bool(bridge.export_wav(path, start_frame, end_frame))

    tmp_fd, tmp_wav = tempfile.mkstemp(suffix=".wav")
    os.close(tmp_fd)
    try:
        if not bridge.export_wav(tmp_wav, start_frame, end_frame):
            return False
        try:
            import soundfile as sf
        except ImportError as exc:
            raise RuntimeError(tr(
                "FLAC/MP3 export requires the soundfile package:\n"
                "pip install soundfile")) from exc
        data, sr = sf.read(tmp_wav, dtype="float32", always_2d=True)
        if fmt in ("wav", "flac"):
            sf.write(path, data, sr, subtype=option)
        elif fmt == "mp3":
            if "MP3" not in sf.available_formats():
                raise RuntimeError(tr(
                    "Installed libsndfile does not support MP3 — "
                    "update soundfile (pip install -U soundfile)."))
            bitrate = float(option)
            level = max(0.0, min(1.0, (320.0 - bitrate) / (320.0 - 8.0)))
            sf.write(path, data, sr, format="MP3",
                     bitrate_mode="CONSTANT", compression_level=level)
        else:
            raise RuntimeError(trf("Unknown export format: {fmt}", fmt=fmt))
        return True
    finally:
        try:
            os.remove(tmp_wav)
        except OSError:
            pass
