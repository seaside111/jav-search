"""Emby API client used by the actor portrait scraper."""
import asyncio
import base64
import re
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


def _name_key(value: str) -> str:
    return re.sub(r"[\s・·._-]+", "", (value or "")).casefold()


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


async def _find_person(client: httpx.AsyncClient, base: str, headers: dict,
                       name: str) -> Optional[dict]:
    response = await client.get(
        f"{base}/Persons", headers=headers,
        params={"SearchTerm": name, "Recursive": "true", "Limit": 30,
                "Fields": "ImageTags"})
    response.raise_for_status()
    payload = response.json()
    items = payload.get("Items", payload if isinstance(payload, list) else [])
    wanted = _name_key(name)
    return next((item for item in items
                 if _name_key(item.get("Name", "")) == wanted), None)


async def _verify_primary(client: httpx.AsyncClient, base: str, headers: dict,
                          item_id: str) -> bool:
    response = await client.get(f"{base}/Items/{item_id}/Images", headers=headers)
    response.raise_for_status()
    payload = response.json()
    return isinstance(payload, list) and any(
        (image.get("ImageType") or "").casefold() == "primary" for image in payload)


async def sync_person_images(url: str, api_key: str, portraits: list[dict],
                             media_paths: Optional[list[str]] = None,
                             poll_delays: tuple[float, ...] = (1, 2, 3, 5, 8, 13, 20)) -> dict:
    """Notify Emby about only the new media paths, then upload Person images.

    A newly written NFO is not immediately represented by an Emby Person.  Starting
    a targeted media update before lookup makes Emby import the NFO/actor association.
    Uploading directly to the Person afterwards avoids relying on Emby downloading
    the remote NFO <thumb> URL.  We intentionally do not FullRefresh the Person after
    upload because a metadata provider could replace the image we just set.
    """
    base, token = normalize_url(url), (api_key or "").strip()
    valid = [item for item in (portraits or [])
             if item.get("name") and isinstance(item.get("image"), (bytes, bytearray))
             and item.get("image")]
    if not base or not token or not valid:
        return {"media_update_triggered": False, "results": [], "message": "Emby 未配置或没有头像"}

    paths = []
    for value in media_paths or []:
        path = (value or "").strip()
        if path and path not in paths:
            paths.append(path)
    if not paths:
        return {"media_update_triggered": False, "results": [{
            "name": item["name"], "updated": False,
            "message": "未提供 Emby 可见的当前影片路径，已拒绝全库刷新"} for item in valid],
            "message": "缺少 Emby 当前影片路径"}

    headers = _headers(token)
    results = {item["name"]: {"name": item["name"], "updated": False,
                               "message": "Emby 中未找到同名演员"}
               for item in valid}
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            media_update = await client.post(
                f"{base}/Library/Media/Updated", headers={**headers, "Content-Type": "application/json"},
                json={"Updates": [{"Path": path, "UpdateType": "Created"} for path in paths]})
            if media_update.status_code in (401, 403):
                return {"media_update_triggered": False, "results": [{
                    "name": item["name"], "updated": False,
                    "message": "API Key 没有权限通知当前影片目录"} for item in valid],
                    "message": "Emby 管理员权限不足"}
            media_update.raise_for_status()

            pending = {item["name"]: item for item in valid}
            people = {}
            # 先立即查询已存在的 Person；新 Person 尚未出现时再按间隔轮询。
            delays = (0,) + tuple(poll_delays)
            for delay in delays:
                if delay:
                    await asyncio.sleep(delay)
                for name in list(pending):
                    person = await _find_person(client, base, headers, name)
                    if person and person.get("Id"):
                        people[name] = person
                        pending.pop(name, None)
                if not pending:
                    break

            for name, item in valid_by_name(valid).items():
                person = people.get(name)
                if not person:
                    continue
                item_id = person["Id"]
                upload_headers = {**headers, "Content-Type": item.get("content_type", "image/jpeg")}
                upload = await client.post(
                    f"{base}/Items/{item_id}/Images/Primary", headers=upload_headers,
                    content=base64.b64encode(item["image"]))
                if upload.status_code in (401, 403):
                    results[name] = {"name": name, "updated": False,
                                     "person_id": item_id,
                                     "message": "API Key 没有管理员权限，无法上传头像"}
                    continue
                upload.raise_for_status()
                verified = await _verify_primary(client, base, headers, item_id)
                results[name] = {"name": name, "updated": verified,
                                 "person_id": item_id,
                                 "message": "头像已上传并验证" if verified else "头像上传后未检测到 Primary 图片"}
            return {"media_update_triggered": True, "results": list(results.values()),
                    "message": "已通知当前影片目录，演员头像同步完成"}
    except Exception as exc:
        return {"media_update_triggered": False, "results": [{
            "name": item["name"], "updated": False,
            "message": f"Emby 更新失败：{exc}"} for item in valid],
            "message": f"Emby 更新失败：{exc}"}


def valid_by_name(items: list[dict]) -> dict[str, dict]:
    """Preserve order while de-duplicating portraits by normalized actor name."""
    result = {}
    keys = set()
    for item in items:
        key = _name_key(item.get("name", ""))
        if key and key not in keys:
            keys.add(key)
            result[item["name"]] = item
    return result


async def update_person_image(url: str, api_key: str, name: str, image: bytes,
                              content_type: str = "image/jpeg",
                              media_paths: Optional[list[str]] = None) -> dict:
    """Backward-compatible single-person wrapper using the refresh-and-verify flow."""
    batch = await sync_person_images(url, api_key, [{
        "name": name, "image": image, "content_type": content_type}], media_paths=media_paths)
    return (batch.get("results") or [{"updated": False, "message": batch.get("message", "同步失败")}])[0]
