"""VST3 plugin scanner and browser dialog."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from PySide6.QtCore import (QAbstractTableModel, QModelIndex, QObject, Qt,
                            QRunnable, QSettings, QThreadPool, Signal)
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QPushButton, QTableView, QVBoxLayout)

from ..engine_bridge import EngineBridge
from . import theme
from .i18n import tr, trf

DEFAULT_VST_DIRS = [
    "C:/Program Files/Common Files/VST3",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs/Common/VST3"),
]

CACHE_KEY = "plugins/vst_cache"
DIRS_KEY = "plugins/vst_dirs"


@dataclass
class PluginInfo:
    path: str
    name: str
    vendor: str
    category: str
    sub_categories: str
    uid: str
    is_instrument: bool

    @classmethod
    def from_engine(cls, info) -> "PluginInfo":
        return cls(
            path=info.path,
            name=info.name,
            vendor=info.vendor,
            category=info.category,
            sub_categories=info.sub_categories,
            uid=info.uid,
            is_instrument=bool(info.is_instrument),
        )

    @classmethod
    def from_dict(cls, d: dict) -> "PluginInfo":
        return cls(**d)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "name": self.name,
            "vendor": self.vendor,
            "category": self.category,
            "sub_categories": self.sub_categories,
            "uid": self.uid,
            "is_instrument": self.is_instrument,
        }


class _PluginTableModel(QAbstractTableModel):
    HEADERS = ("Name", "Vendor", "Category", "Path")

    @classmethod
    def header_labels(cls) -> tuple[str, ...]:
        return tuple(tr(h) for h in cls.HEADERS)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._all: list[PluginInfo] = []
        self._rows: list[PluginInfo] = []

    def set_plugins(self, plugins: list[PluginInfo]) -> None:
        self.beginResetModel()
        self._all = list(plugins)
        self._rows = list(plugins)
        self.endResetModel()

    def apply_filter(self, text: str, kind: str) -> None:
        text = text.strip().lower()
        self.beginResetModel()
        self._rows = []
        for p in self._all:
            if kind == "Instruments" and not p.is_instrument:
                continue
            if kind == "Effects" and p.is_instrument:
                continue
            if text and text not in p.name.lower() and text not in p.vendor.lower():
                continue
            self._rows.append(p)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid() or role not in (Qt.DisplayRole, Qt.ToolTipRole):
            return None
        p = self._rows[index.row()]
        col = index.column()
        if col == 0:
            return p.name
        if col == 1:
            return p.vendor
        if col == 2:
            cat = p.category
            if p.sub_categories:
                cat = f"{cat} / {p.sub_categories}" if cat else p.sub_categories
            return cat
        return p.path

    def headerData(self, section: int, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.header_labels()[section]
        return None

    def plugin_at(self, row: int) -> PluginInfo | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None


class _ScanSignals(QObject):
    finished = Signal(list)
    failed = Signal(str)


class _ScanTask(QRunnable):
    def __init__(self, bridge: EngineBridge, dirs: list[str],
                 signals: _ScanSignals) -> None:
        super().__init__()
        self.bridge = bridge
        self.dirs = dirs
        self.signals = signals

    def run(self) -> None:
        try:
            raw = self.bridge.scan_vst_plugins(self.dirs)
            plugins = [PluginInfo.from_engine(p) for p in raw]
            self.signals.finished.emit(plugins)
        except Exception as exc:  # noqa: BLE001
            self.signals.failed.emit(str(exc))


class PluginBrowserDialog(QDialog):
    """Scan and pick a VST3 plugin."""

    def __init__(self, bridge: EngineBridge, parent=None,
                 instruments_only: bool = False,
                 cached_plugins: list[PluginInfo] | None = None) -> None:
        super().__init__(parent)
        self._bridge = bridge
        self._instruments_only = instruments_only
        self._selected: PluginInfo | None = None
        self._settings = QSettings("PyDAW", "PyDAW")
        self.setWindowTitle(tr("VST Instruments") if instruments_only
                            else tr("VST Plugins"))
        self.resize(720, 480)

        root = QVBoxLayout(self)
        root.setSpacing(8)

        dir_row = QHBoxLayout()
        self._scan_label = QLabel(tr("Scan folders:"))
        dir_row.addWidget(self._scan_label)
        self._dir_label = QLabel()
        self._dir_label.setStyleSheet(
            f"color: {theme.TEXT_DIM}; background: transparent;")
        self._dir_label.setWordWrap(True)
        dir_row.addWidget(self._dir_label, 1)
        self._add_dir_btn = QPushButton(tr("Add folder\u2026"))
        self._add_dir_btn.clicked.connect(self._add_scan_dir)
        dir_row.addWidget(self._add_dir_btn)
        root.addLayout(dir_row)

        filter_row = QHBoxLayout()
        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText(tr("Filter by name or vendor\u2026"))
        self._filter_edit.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self._filter_edit, 1)

        self._kind_combo = QComboBox()
        for label in ("All", "Instruments", "Effects"):
            self._kind_combo.addItem(tr(label), label)
        if instruments_only:
            self._kind_combo.setCurrentIndex(1)
            self._kind_combo.setEnabled(False)
        self._kind_combo.currentIndexChanged.connect(self._apply_filter)
        filter_row.addWidget(self._kind_combo)
        root.addLayout(filter_row)

        self._status = QLabel()
        self._status.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; background: transparent;")
        root.addWidget(self._status)

        self._model = _PluginTableModel(self)
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QTableView.SelectRows)
        self._table.setSelectionMode(QTableView.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeToContents)
        self._table.doubleClicked.connect(self._accept_selection)
        root.addWidget(self._table, 1)

        btn_row = QHBoxLayout()
        self._scan_btn = QPushButton(tr("Rescan"))
        self._scan_btn.clicked.connect(self._start_scan)
        btn_row.addWidget(self._scan_btn)
        btn_row.addStretch(1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept_selection)
        buttons.rejected.connect(self.reject)
        btn_row.addWidget(buttons)
        root.addLayout(btn_row)

        self._scan_signals = _ScanSignals()
        self._scan_signals.finished.connect(self._on_scan_finished)
        self._scan_signals.failed.connect(self._on_scan_failed)

        self._dirs = self._load_dirs()
        self._update_dir_label()

        if cached_plugins is not None:
            self._set_plugins(cached_plugins, from_cache=True)
        else:
            cached = self._load_cache()
            if cached:
                self._set_plugins(cached, from_cache=True)
            else:
                self._start_scan()

    def selected_plugin(self) -> PluginInfo | None:
        return self._selected

    def _load_dirs(self) -> list[str]:
        raw = self._settings.value(DIRS_KEY)
        if raw:
            try:
                dirs = json.loads(str(raw))
                if isinstance(dirs, list) and dirs:
                    return [str(d) for d in dirs]
            except json.JSONDecodeError:
                pass
        existing = [d for d in DEFAULT_VST_DIRS if os.path.isdir(d)]
        return existing or list(DEFAULT_VST_DIRS)

    def _save_dirs(self) -> None:
        self._settings.setValue(DIRS_KEY, json.dumps(self._dirs))

    def _load_cache(self) -> list[PluginInfo]:
        raw = self._settings.value(CACHE_KEY)
        if not raw:
            return []
        try:
            data = json.loads(str(raw))
            return [PluginInfo.from_dict(d) for d in data]
        except (json.JSONDecodeError, TypeError, KeyError):
            return []

    def _save_cache(self, plugins: list[PluginInfo]) -> None:
        self._settings.setValue(
            CACHE_KEY,
            json.dumps([p.to_dict() for p in plugins]))

    def _update_dir_label(self) -> None:
        self._dir_label.setText("; ".join(self._dirs))

    def _add_scan_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, tr("VST3 folder"))
        if path and path not in self._dirs:
            self._dirs.append(path)
            self._save_dirs()
            self._update_dir_label()

    def _set_plugins(self, plugins: list[PluginInfo],
                     from_cache: bool = False) -> None:
        self._model.set_plugins(plugins)
        self._apply_filter()
        src = tr("cache") if from_cache else tr("scan")
        self._status.setText(trf("{count} plugins ({source})",
                                 count=len(plugins), source=src))
        self._scan_btn.setEnabled(True)

    def _apply_filter(self, *_args) -> None:
        kind = "Instruments" if self._instruments_only else str(
            self._kind_combo.currentData() or "All")
        self._model.apply_filter(self._filter_edit.text(), kind)

    def _start_scan(self) -> None:
        self._scan_btn.setEnabled(False)
        self._status.setText(tr("Scanning VST3 plugins\u2026"))
        QThreadPool.globalInstance().start(
            _ScanTask(self._bridge, self._dirs, self._scan_signals))

    def _on_scan_finished(self, plugins: list) -> None:
        typed = [p if isinstance(p, PluginInfo) else PluginInfo.from_engine(p)
                 for p in plugins]
        self._save_cache(typed)
        self._set_plugins(typed)

    def _on_scan_failed(self, message: str) -> None:
        self._scan_btn.setEnabled(True)
        self._status.setText(trf("Scan failed: {message}", message=message))

    def _accept_selection(self) -> None:
        indexes = self._table.selectionModel().selectedRows()
        if not indexes:
            return
        plugin = self._model.plugin_at(indexes[0].row())
        if plugin is None:
            return
        if self._instruments_only and not plugin.is_instrument:
            return
        self._selected = plugin
        self.accept()
