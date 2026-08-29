"""Verified GitHub Releases updater for the frozen Windows desktop build."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
from urllib.parse import urlsplit

import httpx

from platform_paths import app_data_dir


INSTALLER_RE = re.compile(r"^JAV-Search-v(.+)-Windows-x64-Setup\.exe$", re.I)
ALLOWED_DOWNLOAD_HOSTS = {"github.com", "objects.githubusercontent.com",
                          "release-assets.githubusercontent.com"}
MAX_INSTALLER_BYTES = 750 * 1024 * 1024


def select_windows_assets(release: dict) -> dict:
    assets = release.get("assets") or []
    installer = next((a for a in assets if INSTALLER_RE.match(a.get("name") or "")), None)
    checksum = None
    if installer:
        expected_name = installer["name"] + ".sha256"
        checksum = next((a for a in assets if (a.get("name") or "").lower() == expected_name.lower()), None)
    return {
        "installer": installer,
        "checksum": checksum,
        "available": bool(installer and (checksum or installer.get("digest"))),
    }


def _safe_download_url(url: str) -> str:
    parsed = urlsplit(url or "")
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_DOWNLOAD_HOSTS:
        raise ValueError("更新文件下载地址不是受信任的 GitHub HTTPS 地址")
    return url


def _verify_response_url(response: httpx.Response) -> None:
    _safe_download_url(str(response.url))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_sha256(text: str, filename: str) -> str:
    for line in (text or "").splitlines():
        parts = line.strip().split()
        if parts and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            if len(parts) == 1 or parts[-1].lstrip("*") == filename:
                return parts[0].lower()
    raise ValueError("更新校验文件格式无效")


async def _download(client: httpx.AsyncClient, url: str, target: Path,
                    max_bytes: int) -> None:
    url = _safe_download_url(url)
    partial = target.with_suffix(target.suffix + ".part")
    total = 0
    try:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            _verify_response_url(response)
            length = int(response.headers.get("content-length") or 0)
            if length and length > max_bytes:
                raise ValueError("更新文件超过允许大小")
            with partial.open("wb") as output:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("更新文件超过允许大小")
                    output.write(chunk)
        partial.replace(target)
    except Exception:
        partial.unlink(missing_ok=True)
        raise


async def prepare_update(release: dict, proxy: str | None = None) -> dict:
    selected = select_windows_assets(release)
    if not selected["available"]:
        raise ValueError("此版本没有可验证的 Windows x64 安装包")
    installer = selected["installer"]
    checksum = selected["checksum"]
    name = Path(installer["name"]).name
    update_dir = app_data_dir() / "updates"
    update_dir.mkdir(parents=True, exist_ok=True)
    target = update_dir / name

    async with httpx.AsyncClient(proxy=proxy, timeout=httpx.Timeout(30, read=300),
                                 follow_redirects=True) as client:
        digest = (installer.get("digest") or "").lower()
        expected = digest.removeprefix("sha256:") if digest.startswith("sha256:") else ""
        if checksum:
            checksum_url = _safe_download_url(checksum.get("browser_download_url") or "")
            response = await client.get(checksum_url)
            response.raise_for_status()
            _verify_response_url(response)
            expected = parse_sha256(response.text, name)
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("GitHub Release 未提供有效的 SHA-256")
        await _download(client, installer.get("browser_download_url") or "", target,
                        MAX_INSTALLER_BYTES)

    actual = _sha256_file(target)
    if actual != expected:
        target.unlink(missing_ok=True)
        raise ValueError("安装包 SHA-256 校验失败")
    return {"path": str(target), "sha256": actual, "name": name}
