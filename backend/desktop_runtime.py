"""Small bridge between HTTP routes and the optional desktop launcher."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


_update_handler: Callable[[Path], None] | None = None


def set_update_handler(handler: Callable[[Path], None] | None) -> None:
    global _update_handler
    _update_handler = handler


def request_update(installer: Path) -> bool:
    if _update_handler is None:
        return False
    _update_handler(installer)
    return True
