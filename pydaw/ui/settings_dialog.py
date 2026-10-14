"""Unified program settings: appearance, audio and keyboard shortcuts."""

from __future__ import annotations

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QFormLayout, QHBoxLayout,
                               QHeaderView, QLabel, QMessageBox, QPushButton,
                               QTabWidget, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from . import i18n, theme
from .i18n import tr

BUFFER_SIZES = [128, 256, 512, 1024, 2048]
COMMON_RATES = [44100, 48000, 88200, 96000]

THEME_CHOICES = [("dark", "Dark"), ("light", "Light")]
SCALE_CHOICES = [
    ("1.0", "100%"),
    ("1.25", "125%"),
    ("1.5", "150%"),
    ("1.75", "175%"),
    ("2.0", "200%"),
]


class SettingsDialog(QDialog):
    """Tabbed settings: appearance, audio, keyboard shortcuts."""

    def __init__(self, bridge, actions: dict[str, QAction],
                 default_shortcuts: dict[str, QKeySequence],
                 settings: QSettings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Settings"))
        self.resize(560, 480)
        self.bridge = bridge
        self._actions = actions
        self._default_shortcuts = default_shortcuts
        self._settings = settings

        self._saved_theme = str(settings.value("ui/theme", "dark")).lower()
        if self._saved_theme not in ("dark", "light"):
            self._saved_theme = "dark"
        self._saved_scale = str(settings.value("ui/scale", "1.0"))
        self._saved_language = i18n.normalize(
            settings.value("ui/language", ""))

        self._orig_device_id = int(settings.value("audio/device_id", 0))
        self._orig_sample_rate = int(settings.value("audio/sample_rate", 48000))
        self._orig_buffer = int(settings.value("audio/buffer", 512))
        self._orig_api_name = str(settings.value(
            "audio/api_name", getattr(bridge, "api_name", "")) or "")

        tabs = QTabWidget()
        tabs.addTab(self._build_appearance_tab(), tr("Appearance"))
        tabs.addTab(self._build_audio_tab(), tr("Audio"))
        tabs.addTab(self._build_shortcuts_tab(), tr("Keyboard shortcuts"))
        tabs.addTab(self._build_help_tab(), tr("Help"))

        buttons = QDialogButtonBox(QDialogButtonBox.Ok |
                                   QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

    # -- appearance ----------------------------------------------------------

    def _build_appearance_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        self.theme_combo = QComboBox()
        for value, label in THEME_CHOICES:
            self.theme_combo.addItem(tr(label), value)
            if value == self._saved_theme:
                self.theme_combo.setCurrentIndex(self.theme_combo.count() - 1)
        form.addRow(tr("Theme"), self.theme_combo)

        self.scale_combo = QComboBox()
        for value, label in SCALE_CHOICES:
            self.scale_combo.addItem(label, value)
            if value == self._saved_scale:
                self.scale_combo.setCurrentIndex(self.scale_combo.count() - 1)
        form.addRow(tr("UI scale"), self.scale_combo)

        self.language_combo = QComboBox()
        for value, label in i18n.LANGUAGES:
            self.language_combo.addItem(label, value)
            if value == self._saved_language:
                self.language_combo.setCurrentIndex(
                    self.language_combo.count() - 1)
        form.addRow(tr("Language"), self.language_combo)

        self._restart_note = QLabel(tr("Restart required"))
        self._restart_note.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-size: 11px;")
        self._restart_note.setVisible(False)
        form.addRow(self._restart_note)

        self.theme_combo.currentIndexChanged.connect(self._update_restart_note)
        self.scale_combo.currentIndexChanged.connect(self._update_restart_note)
        self._update_restart_note()

        return page

    def _update_restart_note(self) -> None:
        theme_changed = self.theme_combo.currentData() != self._saved_theme
        scale_changed = self.scale_combo.currentData() != self._saved_scale
        self._restart_note.setVisible(theme_changed or scale_changed)

    # -- help ----------------------------------------------------------------

    def _build_help_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        saved = str(self._settings.value("help/tooltips", "true")).lower()
        self.tooltips_check = QCheckBox(tr("Show tooltips"))
        self.tooltips_check.setChecked(saved not in ("false", "0"))
        form.addRow(self.tooltips_check)

        note = QLabel(tr("Disables every tooltip in all program windows.\n"
                         "Applies immediately."))
        note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        form.addRow(note)

        return page

    # -- audio ---------------------------------------------------------------

    def _build_audio_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)

        self.api_combo = QComboBox()
        self.api_combo.addItem("Auto", "")
        self._apis = self.bridge.list_audio_apis()
        for name, label in self._apis:
            text = label or name
            if name == "asio":
                text += " (ASIO)"
            self.api_combo.addItem(text, name)
        for i in range(self.api_combo.count()):
            if self.api_combo.itemData(i) == self._orig_api_name:
                self.api_combo.setCurrentIndex(i)
                break
        form.addRow(tr("Audio API"), self.api_combo)

        self.device_combo = QComboBox()
        form.addRow(tr("Output device"), self.device_combo)

        self.rate_combo = QComboBox()
        form.addRow(tr("Sample rate"), self.rate_combo)

        self.buffer_combo = QComboBox()
        # ASIO drivers may force their own buffer size (e.g. FlexASIO uses
        # 480); include the actual engine value so the combo doesn't silently
        # fall back to the first item and re-apply a wrong size.
        sizes = sorted(set(BUFFER_SIZES) | {int(self.bridge.buffer_frames)})
        for size in sizes:
            label = f"{size} samples"
            if size not in BUFFER_SIZES:
                label += "  (driver)"
            self.buffer_combo.addItem(label, size)
            if size == self.bridge.buffer_frames:
                self.buffer_combo.setCurrentIndex(
                    self.buffer_combo.count() - 1)
        form.addRow(tr("Buffer size"), self.buffer_combo)

        note = QLabel(tr("Applying restarts the audio engine;\n"
                         "loaded files are resampled automatically."))
        note.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-size: 11px;")
        form.addRow(note)

        self.api_combo.currentIndexChanged.connect(self._fill_devices)
        self.device_combo.currentIndexChanged.connect(self._fill_rates)
        self._fill_devices()

        return page

    def _fill_devices(self) -> None:
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        self._devices = self.bridge.list_devices(self.api_name)
        current_id = self.bridge.engine.current_device_id()
        for dev in self._devices:
            label = dev.name + ("  (default)" if dev.is_default else "")
            if dev.api_display_name:
                label = f"{label}  [{dev.api_display_name}]"
            self.device_combo.addItem(label, dev.id)
            if dev.id == current_id:
                self.device_combo.setCurrentIndex(
                    self.device_combo.count() - 1)
        if not self._devices:
            self.device_combo.addItem("No devices found", 0)
            self.device_combo.setEnabled(False)
        else:
            self.device_combo.setEnabled(True)
        self.device_combo.blockSignals(False)
        self._fill_rates()

    def _fill_rates(self) -> None:
        self.rate_combo.clear()
        device_id = self.device_combo.currentData()
        rates: list[int] = []
        for dev in self._devices:
            if dev.id == device_id:
                rates = [r for r in dev.sample_rates if r in COMMON_RATES]
                break
        if not rates:
            rates = [48000]
        for rate in rates:
            self.rate_combo.addItem(f"{rate} Hz", rate)
            if rate == self.bridge.sample_rate:
                self.rate_combo.setCurrentIndex(self.rate_combo.count() - 1)

    # -- shortcuts -----------------------------------------------------------

    def _build_shortcuts_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        self._shortcut_values: dict[str, str] = {}
        for action_id, action in sorted(self._actions.items(),
                                        key=lambda kv: kv[1].text()):
            stored = self._settings.value(f"keys/{action_id}")
            if stored:
                seq = QKeySequence(str(stored))
            else:
                seq = action.shortcut()
            self._shortcut_values[action_id] = seq.toString(
                QKeySequence.NativeText)

        self.shortcut_table = QTableWidget()
        self.shortcut_table.setColumnCount(2)
        self.shortcut_table.setHorizontalHeaderLabels(
            [tr("Action"), tr("Shortcut")])
        self.shortcut_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch)
        self.shortcut_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents)
        self.shortcut_table.verticalHeader().setVisible(False)
        self.shortcut_table.setSelectionBehavior(
            QTableWidget.SelectRows)
        self.shortcut_table.setSelectionMode(
            QTableWidget.SingleSelection)
        self.shortcut_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.shortcut_table.setShowGrid(False)
        self.shortcut_table.setAlternatingRowColors(True)
        self._action_ids_by_row: list[str] = []
        self._populate_shortcut_table()
        layout.addWidget(self.shortcut_table)

        edit_row = QHBoxLayout()
        edit_row.addWidget(QLabel(tr("New shortcut:")))
        from PySide6.QtWidgets import QKeySequenceEdit
        self._key_edit = QKeySequenceEdit()
        self._key_edit.setClearButtonEnabled(True)
        edit_row.addWidget(self._key_edit, 1)
        assign_btn = QPushButton(tr("Assign"))
        assign_btn.clicked.connect(self._assign_shortcut)
        edit_row.addWidget(assign_btn)
        layout.addLayout(edit_row)

        reset_btn = QPushButton(tr("Reset all"))
        reset_btn.clicked.connect(self._reset_all_shortcuts)
        layout.addWidget(reset_btn)

        self.shortcut_table.itemSelectionChanged.connect(
            self._on_shortcut_row_selected)
        if self.shortcut_table.rowCount():
            self.shortcut_table.selectRow(0)

        return page

    def _populate_shortcut_table(self) -> None:
        rows = sorted(self._actions.items(), key=lambda kv: kv[1].text())
        self._action_ids_by_row = [aid for aid, _ in rows]
        self.shortcut_table.setRowCount(len(rows))
        for row, (action_id, action) in enumerate(rows):
            action_item = QTableWidgetItem(
                action.text().replace("&", ""))
            action_item.setData(Qt.UserRole, action_id)
            action_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            shortcut_item = QTableWidgetItem(
                self._shortcut_values[action_id])
            shortcut_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.shortcut_table.setItem(row, 0, action_item)
            self.shortcut_table.setItem(row, 1, shortcut_item)
            self._clear_conflict_style(row)

    def _on_shortcut_row_selected(self) -> None:
        row = self.shortcut_table.currentRow()
        if row < 0:
            return
        action_id = self._action_ids_by_row[row]
        self._key_edit.setKeySequence(
            QKeySequence(self._shortcut_values[action_id]))

    def _assign_shortcut(self) -> None:
        row = self.shortcut_table.currentRow()
        if row < 0:
            return
        action_id = self._action_ids_by_row[row]
        seq = self._key_edit.keySequence()
        seq_str = seq.toString(QKeySequence.NativeText)
        conflict = self._find_conflict(action_id, seq_str)
        if conflict:
            other = self._actions[conflict].text().replace("&", "")
            QMessageBox.warning(
                self, tr("Conflict"),
                tr("The shortcut \u00ab{seq}\u00bb is already assigned "
                   "to \u00ab{other}\u00bb.").format(seq=seq_str,
                                                     other=other))
            self._mark_conflict(row)
            return
        self._shortcut_values[action_id] = seq_str
        self.shortcut_table.item(row, 1).setText(seq_str)
        self._clear_conflict_style(row)

    def _find_conflict(self, action_id: str, seq_str: str) -> str | None:
        if not seq_str:
            return None
        for aid, value in self._shortcut_values.items():
            if aid != action_id and value == seq_str:
                return aid
        return None

    def _mark_conflict(self, row: int) -> None:
        for col in (0, 1):
            item = self.shortcut_table.item(row, col)
            if item:
                item.setForeground(theme.COL_RED)

    def _clear_conflict_style(self, row: int) -> None:
        for col, color in ((0, theme.COL_TEXT), (1, theme.COL_ACCENT)):
            item = self.shortcut_table.item(row, col)
            if item:
                item.setForeground(color)

    def _reset_all_shortcuts(self) -> None:
        for action_id in self._shortcut_values:
            default = self._default_shortcuts.get(action_id, QKeySequence())
            self._shortcut_values[action_id] = default.toString(
                QKeySequence.NativeText)
        self._populate_shortcut_table()
        self.shortcut_table.selectRow(0)

    def _on_accept(self) -> None:
        conflicts: list[str] = []
        seen: dict[str, str] = {}
        for action_id, seq_str in self._shortcut_values.items():
            if not seq_str:
                continue
            if seq_str in seen:
                conflicts.append(seq_str)
            else:
                seen[seq_str] = action_id
        if conflicts:
            QMessageBox.warning(
                self, tr("Conflict"),
                tr("There are duplicate shortcuts.\n"
                   "Fix them before saving."))
            return
        self.accept()

    # -- public properties ---------------------------------------------------

    @property
    def theme_name(self) -> str:
        return str(self.theme_combo.currentData() or "dark")

    @property
    def ui_scale(self) -> str:
        return str(self.scale_combo.currentData() or "1.0")

    @property
    def language(self) -> str:
        return str(self.language_combo.currentData() or "en")

    @property
    def tooltips_enabled(self) -> bool:
        return self.tooltips_check.isChecked()

    @property
    def device_id(self) -> int:
        return int(self.device_combo.currentData() or 0)

    @property
    def api_name(self) -> str:
        return str(self.api_combo.currentData() or "")

    @property
    def sample_rate(self) -> int:
        return int(self.rate_combo.currentData() or 48000)

    @property
    def buffer_size(self) -> int:
        return int(self.buffer_combo.currentData() or 512)

    @property
    def audio_changed(self) -> bool:
        return (self.api_name != self._orig_api_name
                or self.device_id != self._orig_device_id
                or self.sample_rate != self._orig_sample_rate
                or self.buffer_size != self._orig_buffer)

    @property
    def shortcut_overrides(self) -> dict[str, str]:
        """action_id -> shortcut string (only entries differing from default)."""
        result: dict[str, str] = {}
        for action_id, seq_str in self._shortcut_values.items():
            default = self._default_shortcuts.get(action_id, QKeySequence())
            default_str = default.toString(QKeySequence.NativeText)
            if seq_str != default_str:
                result[action_id] = seq_str
        return result

    @property
    def cleared_shortcut_ids(self) -> list[str]:
        """Actions reset to default (had a stored override, now matches default)."""
        cleared: list[str] = []
        for action_id in self._actions:
            default = self._default_shortcuts.get(action_id, QKeySequence())
            default_str = default.toString(QKeySequence.NativeText)
            current = self._shortcut_values.get(action_id, default_str)
            had_override = self._settings.value(f"keys/{action_id}") is not None
            if had_override and current == default_str:
                cleared.append(action_id)
        return cleared
