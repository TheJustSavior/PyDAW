"""Keyboard-layout-independent key handling.

Qt's ``QKeyEvent.key()`` for letter keys reflects the active keyboard
layout: with a Cyrillic layout the physical J key reports the Cyrillic
letter code, so comparisons like ``key == Qt.Key_J`` silently fail and
shortcuts (Ctrl+J, Ctrl+D, ...) stop working until the user switches
back to a Latin layout.

``nativeVirtualKey()`` is layout-independent for letters on the
platforms we care about: Windows VK codes (``0x41..0x5A``) and X11
keysyms for Latin letters (``0x41..0x5A`` / ``0x61..0x7A``) both match
ASCII. ``letter_key()`` maps that back to the ``Qt.Key_A..Qt.Key_Z``
range so shortcut checks work in any layout.
"""

from __future__ import annotations

from PySide6.QtCore import Qt


def letter_key(event) -> int:
    """``event.key()``, but stable for letter keys across keyboard layouts."""
    key = event.key()
    if Qt.Key_A <= key <= Qt.Key_Z:
        return key
    vk = event.nativeVirtualKey()
    if 0x41 <= vk <= 0x5A:          # 'A'..'Z'
        return Qt.Key_A + (vk - 0x41)
    if 0x61 <= vk <= 0x7A:          # 'a'..'z' (X11 keysyms)
        return Qt.Key_A + (vk - 0x61)
    return key
