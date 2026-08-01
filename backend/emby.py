"""Small Emby API client used by the actor portrait scraper."""
import base64
from typing import Optional

import httpx


def normalize_url(value: str) -> str:
    url = (value or "").strip().rstrip("/")
    for suffix in ("/web/index.html", "/web"):
        if url.lower().endswith(suffix):
            url = url[:-len(suffix)].rstrip("/")
    return url


def _headers(api_key: str) -> dict[str, str]:
    return {"X-Emby-Token": api_key, "X-MediaBrowser-Token": api_key}


async def test_connection(url: str, api_key: str) -> dict:
    base = normalize_url(url)
    if not base or not (api_key or "").strip():
        return {"configured": False, "online": False, "message": "请填写 Emby 地址和 API Key"}
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            response = await client.get(f"{base}/System/Info", headers=_headers(api_key.strip()))
        if response.status_code in (401, 403):
            return {"configured": True, "online": False, "message": "API Key 无效或无权限"}
        response.raise_for_status()
        data = response.json()
        return {"configured": True, "online": True, "message": "连接正常",
                "server_name": data.get("ServerName", "Emby"), "version": data.get("Version", "")}
    except Exception as exc:
        return {"configured": True, "online": False, "message": f"连接失败：{exc}"}


async def update_person_image(url: str, api_key: str, name: str, image: bytes,
                              content_type: str = "image/jpeg") -> dict:
    """Find an exact Emby Person by name and replace its Primary image."""
    base, token = normalize_url(url), (api_key or "").strip()
    if not base or not token or not name or not image:
        return {"updated": False, "message": "Emby 未配置"}
    headers = _headers(token)
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(
                f"{base}/Persons", headers=headers,
                params={"SearchTerm": name, "Recursive": "true", "Limit": 20})
            response.raise_for_status()
            payload = response.json()
            items = payload.get("Items", payload if isinstance(payload, list) else [])
            person: Optional[dict] = next(
                (item for item in items if (item.get("Name") or "").strip().casefold() == name.strip().casefold()), None)
            if not person:
                return {"updated": False, "message": "Emby 中未找到同名演员"}
            item_id = person.get("Id")
            upload_headers = {**headers, "Content-Type": content_type}
            upload = await client.post(
                f"{base}/Items/{item_id}/Images/Primary", headers=upload_headers,
                content=base64.b64encode(image))
            upload.raise_for_status()
            return {"updated": True, "person_id": item_id, "message": "头像已更新"}
    except Exception as exc:
        return {"updated": False, "message": f"Emby 更新失败：{exc}"}
