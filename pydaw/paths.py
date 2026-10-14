"""Standard user-facing directories (Documents/PyDAW/...)."""

from __future__ import annotations

import os


def user_root_dir() -> str:
    """``Documents/PyDAW`` — created on first use, verified on every launch."""
    from PySide6.QtCore import QStandardPaths

    docs = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
    root = os.path.join(docs, "PyDAW")
    os.makedirs(root, exist_ok=True)
    return root


def default_projects_dir() -> str:
    """``Documents/PyDAW/projects`` — default save/open location."""
    projects = os.path.join(user_root_dir(), "projects")
    os.makedirs(projects, exist_ok=True)
    return projects


def default_export_dir() -> str:
    """``Documents/PyDAW/export`` — default export location."""
    export = os.path.join(user_root_dir(), "export")
    os.makedirs(export, exist_ok=True)
    return export


def ensure_user_dirs() -> None:
    """Ensure ``PyDAW``, ``projects`` and ``export`` exist (every launch)."""
    user_root_dir()
    default_projects_dir()
    default_export_dir()
