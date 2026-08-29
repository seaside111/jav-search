"""Runtime paths shared by source, Docker, and frozen Windows builds."""
from __future__ import annotations

import os
from pathlib import Path
import sys


def app_data_dir() -> Path:
    configured = (os.getenv("CONFIG_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "win32":
        local = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(local) / "JAV Search"
    return Path("/config")


def bundle_root() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", "")
    if frozen_root:
        return Path(frozen_root)
    return Path(__file__).resolve().parent.parent


def resource_path(*parts: str) -> Path:
    return bundle_root().joinpath(*parts)


def bundled_tool(name: str) -> Path | None:
    candidates = [
        resource_path("tools", name),
        Path(sys.executable).resolve().parent / "tools" / name,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None

