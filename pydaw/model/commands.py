"""Undo/redo command stack.

Every user action is a Command with do()/undo(); both mutate the model
and mirror the change into the engine via the bridge.
"""

from __future__ import annotations

from typing import Callable


class Command:
    """A reversible action."""

    text = "Action"

    def __init__(self, do: Callable[[], None], undo: Callable[[], None],
                 text: str = "Action") -> None:
        self._do = do
        self._undo = undo
        self.text = text

    def do(self) -> None:
        self._do()

    def undo(self) -> None:
        self._undo()


class CompositeCommand(Command):
    """Several commands undone/redone as one step."""

    def __init__(self, commands: list[Command], text: str = "Action") -> None:
        self._commands = commands
        self.text = text

    def do(self) -> None:
        for cmd in self._commands:
            cmd.do()

    def undo(self) -> None:
        for cmd in reversed(self._commands):
            cmd.undo()


class UndoStack:
    def __init__(self, on_change: Callable[[], None] | None = None) -> None:
        self._undo: list[Command] = []
        self._redo: list[Command] = []
        self._on_change = on_change
        # Called before push/undo/redo so the owner can finalize a pending
        # coalesced gesture (knob/slider drag) into the history first.
        # Must be reentrancy-safe: flushing may itself call push().
        self._flush: Callable[[], None] | None = None
        self._flushing = False

    def set_flush(self, flush: Callable[[], None] | None) -> None:
        self._flush = flush

    def _run_flush(self) -> None:
        if self._flush is None or self._flushing:
            return
        self._flushing = True
        try:
            self._flush()
        finally:
            self._flushing = False

    def push(self, cmd: Command) -> None:
        """Execute the command and add it to the stack."""
        self._run_flush()
        cmd.do()
        self._undo.append(cmd)
        self._redo.clear()
        self._notify()

    def undo(self) -> None:
        self._run_flush()
        if not self._undo:
            return
        cmd = self._undo.pop()
        cmd.undo()
        self._redo.append(cmd)
        self._notify()

    def redo(self) -> None:
        self._run_flush()
        if not self._redo:
            return
        cmd = self._redo.pop()
        cmd.do()
        self._undo.append(cmd)
        self._notify()

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()
        self._notify()

    def _notify(self) -> None:
        if self._on_change:
            self._on_change()
