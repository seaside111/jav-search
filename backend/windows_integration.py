"""Safe Windows desktop integrations used by the local EXE build."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import urlsplit


_CLIENT_NAMES = {
    "qb": "qBittorrent",
    "transmission": "Transmission",
    "system": "系统默认磁力程序",
}


def _candidate_executables(client: str, configured: str = "") -> list[Path]:
    names = ["qbittorrent.exe"] if client == "qb" else ["transmission-qt.exe"]
    roots = [
        os.getenv("ProgramFiles", ""),
        os.getenv("ProgramFiles(x86)", ""),
        os.getenv("LOCALAPPDATA", ""),
    ]
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    located = shutil.which(names[0])
    if located:
        candidates.append(Path(located))
    for root in roots:
        if not root:
            continue
        base = Path(root)
        if client == "qb":
            candidates.append(base / "qBittorrent" / names[0])
        else:
            candidates.extend([
                base / "Transmission" / names[0],
                base / "transmission" / names[0],
            ])
    seen: set[str] = set()
    return [p for p in candidates if not (str(p).lower() in seen or seen.add(str(p).lower()))]


def find_client(client: str, configured: str = "") -> str:
    if sys.platform != "win32" or client not in {"qb", "transmission"}:
        return ""
    for candidate in _candidate_executables(client, configured):
        try:
            if candidate.is_file():
                return str(candidate.resolve())
        except OSError:
            continue
    return ""


def capabilities(config: dict) -> dict:
    is_windows = sys.platform == "win32"
    qb = find_client("qb", config.get("qb_exe_path", "")) if is_windows else ""
    tr = find_client("transmission", config.get("tr_exe_path", "")) if is_windows else ""
    return {
        "windows_desktop": is_windows,
        "local_downloaders": {
            "qb": {"installed": bool(qb), "path": qb},
            "transmission": {"installed": bool(tr), "path": tr},
            "system": {"available": is_windows},
        },
    }


def open_download(download_url: str, client: str, config: dict) -> dict:
    if sys.platform != "win32":
        return {"success": False, "error": "本机程序启动仅支持 Windows 桌面版"}
    client = (client or "system").strip().lower()
    if client not in _CLIENT_NAMES:
        return {"success": False, "error": "不支持的本机下载器"}
    parsed = urlsplit((download_url or "").strip())
    if parsed.scheme.lower() != "magnet":
        return {"success": False, "error": "本机启动当前仅接受 magnet 磁力链接"}

    try:
        if client == "system":
            os.startfile(download_url)  # type: ignore[attr-defined]
            return {"success": True, "client": client, "message": "已交给系统默认磁力程序"}
        key = "qb_exe_path" if client == "qb" else "tr_exe_path"
        executable = find_client(client, config.get(key, ""))
        if not executable:
            return {"success": False, "error": f"未检测到{_CLIENT_NAMES[client]}，请先安装或设置程序路径"}
        subprocess.Popen([executable, download_url], close_fds=True)
        return {"success": True, "client": client, "path": executable,
                "message": f"已启动{_CLIENT_NAMES[client]}"}
    except OSError as exc:
        return {"success": False, "error": f"启动失败：{exc}"}

