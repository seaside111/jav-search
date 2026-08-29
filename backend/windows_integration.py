"""Safe Windows desktop integrations used by the local EXE build."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

from platform_paths import bundled_tool


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


def _first_existing(candidates: list[Path]) -> str:
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate.resolve())
        except OSError:
            continue
    return ""


def find_optional_tool(tool: str) -> str:
    """Locate optional Windows services without starting or installing them."""
    if sys.platform != "win32":
        return ""
    program_files = Path(os.getenv("ProgramFiles", r"C:\Program Files"))
    program_data = Path(os.getenv("ProgramData", r"C:\ProgramData"))
    local = Path(os.getenv("LOCALAPPDATA", "")) if os.getenv("LOCALAPPDATA") else None
    if tool == "flaresolverr":
        candidates = [program_files / "FlareSolverr" / "flaresolverr.exe"]
        if local:
            candidates.extend([
                local / "FlareSolverr" / "flaresolverr.exe",
                local / "Programs" / "FlareSolverr" / "flaresolverr.exe",
                Path.home() / "scoop" / "apps" / "flaresolverr" / "current" / "flaresolverr.exe",
            ])
    elif tool == "jackett":
        candidates = [
            program_files / "Jackett" / "JackettTray.exe",
            program_data / "Jackett" / "JackettConsole.exe",
        ]
    else:
        return ""
    return _first_existing(candidates)


def ffprobe_status() -> dict:
    executable = bundled_tool("ffprobe.exe") if sys.platform == "win32" else bundled_tool("ffprobe")
    if not executable:
        located = shutil.which("ffprobe")
        executable = Path(located) if located else None
    result = {"available": bool(executable), "path": str(executable or ""), "version": ""}
    if executable:
        try:
            completed = subprocess.run([str(executable), "-version"], capture_output=True,
                                       text=True, timeout=5, check=False)
            first_line = (completed.stdout or completed.stderr or "").splitlines()
            result["version"] = first_line[0].strip() if first_line else ""
        except (OSError, subprocess.TimeoutExpired):
            result["available"] = False
    return result


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
        "tools": {
            "ffprobe": ffprobe_status(),
            "flaresolverr": {
                "installed": bool(fs := find_optional_tool("flaresolverr")), "path": fs,
                "required": False,
            },
            "jackett": {
                "installed": bool(jackett := find_optional_tool("jackett")), "path": jackett,
                "required": False,
            },
        },
    }


def normalize_local_file_config(update: dict) -> tuple[dict, list[str]]:
    """Apply Windows desktop-only file management rules before saving config."""
    normalized = dict(update)
    if normalized.get("archive_mode") == "hardlink":
        normalized["archive_mode"] = "copy"
    path_keys = (
        "qb_save_path", "tr_save_path", "scrape_watch_dir", "scrape_output_dir",
        "scrape_actor_images_dir", "actor_scrape_cache_dir", "emby_media_root",
    )
    invalid = [key for key in path_keys
               if (value := str(normalized.get(key) or "").strip())
               and not Path(value).is_absolute()]
    return normalized, invalid


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

