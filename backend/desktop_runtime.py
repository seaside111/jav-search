"""Small bridge between HTTP routes and the optional desktop launcher."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


_update_handler: Callable[[Path], None] | None = None
_folder_handler: Callable[[str], str | None] | None = None


def set_update_handler(handler: Callable[[Path], None] | None) -> None:
    global _update_handler
    _update_handler = handler


def request_update(installer: Path) -> bool:
    if _update_handler is None:
        return False
    _update_handler(installer)
    return True


def set_folder_handler(handler: Callable[[str], str | None] | None) -> None:
    global _folder_handler
    _folder_handler = handler


def select_folder(initial: str = "") -> str | None:
    return _folder_handler(initial) if _folder_handler else None
