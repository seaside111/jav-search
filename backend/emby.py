"""Emby API client used by the actor portrait scraper."""
import asyncio
import base64
import os
import re
import unicodedata
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
    # NFKC makes visually identical full-width/half-width Japanese names
    # comparable (for example チーチー and ﾁｰﾁｰ). Format characters include
    # zero-width spaces/marks that can otherwise make an exact match fail.
    value = unicodedata.normalize("NFKC", value or "")
    value = "".join(char for char in value if unicodedata.category(char) != "Cf")
    return re.sub(r"[\s・·._\-‐‑‒–—―−]+", "", value).casefold()


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


async def _find_people_from_media(client: httpx.AsyncClient, base: str, headers: dict,
                                  names: list[str], media_paths: list[str]) -> dict[str, dict]:
    """Resolve Person IDs only through the current movie's People relationship."""
    found, _state = await _find_people_from_media_state(
        client, base, headers, names, media_paths)
    return found


def _path_key(value: str) -> str:
    """Compare Emby paths across Windows/Linux separators and harmless casing differences."""
    return re.sub(r"/+", "/", (value or "").replace("\\", "/")).rstrip("/").casefold()


def _media_search_terms(media_paths: list[str]) -> list[str]:
    """Extract stable movie identifiers for when Emby's exact Path filter misses."""
    terms = []
    for value in reversed(media_paths or []):
        name = os.path.basename((value or "").replace("\\", "/").rstrip("/"))
        stem = os.path.splitext(name)[0]
        match = re.search(r"(?i)(FC2(?:-PPV)?[-_ ]?\d{5,8}|[A-Z]{2,10}[-_ ]?\d{2,7})", stem)
        term = re.sub(r"[_ ]+", "-", match.group(1)).upper() if match else ""
        if term and term not in terms:
            terms.append(term)
    return terms


def _media_path_matches(item: dict, media_paths: list[str], terms: list[str]) -> bool:
    actual = _path_key(item.get("Path", ""))
    candidates = [_path_key(path) for path in media_paths or [] if path]
    if actual and any(actual == path or actual.startswith(path + "/") or
                      path.startswith(actual + "/") for path in candidates):
        return True
    haystack = _name_key(f"{item.get('Name', '')} {item.get('Path', '')}")
    return bool(haystack and any(_name_key(term) in haystack for term in terms))


async def _find_people_from_media_state(client: httpx.AsyncClient, base: str,
                                        headers: dict, names: list[str],
                                        media_paths: list[str]) -> tuple[dict[str, dict], dict]:
    """Return linked people plus enough state to explain/retry an incomplete import."""
    pending = {_name_key(name): name for name in names if _name_key(name)}
    found, media_by_id = {}, {}
    paths = [path for path in media_paths or [] if (path or "").strip()]
    terms = _media_search_terms(paths)

    async def add_items(response, exact_path: bool = False):
        if response.status_code in (401, 403):
            response.raise_for_status()
        if response.status_code >= 400:
            return
        payload = response.json()
        items = payload.get("Items", payload if isinstance(payload, list) else [])
        for item in items or []:
            item_type = (item.get("Type") or "").casefold() if isinstance(item, dict) else ""
            if (isinstance(item, dict) and item.get("Id") and
                    not item.get("IsFolder") and item_type not in ("folder", "collectionfolder") and
                    (exact_path or _media_path_matches(item, paths, terms))):
                media_by_id[item["Id"]] = item

    # NFO files are metadata inputs rather than Emby items. Query video files
    # first and the movie folder last, avoiding a guaranteed-empty NFO request.
    non_nfo = [path for path in paths
               if os.path.splitext(path.replace("\\", "/"))[1].casefold() != ".nfo"]
    exact_paths = sorted(non_nfo, key=lambda path: 0 if os.path.splitext(path)[1] else 1)
    for path in exact_paths:
        response = await client.get(
            f"{base}/Items", headers=headers,
            params={"Recursive": "true", "Path": path, "Limit": 10,
                    "Fields": "Path,People,MediaType"})
        await add_items(response, exact_path=True)
        if media_by_id:
            break

    # Some Emby versions normalize mount paths before persisting them, making an
    # otherwise valid exact Path query return no rows. The movie code is a safe,
    # narrow fallback; results are still checked against path/code before use.
    if not media_by_id:
        for term in terms:
            response = await client.get(
                f"{base}/Items", headers=headers,
                params={"Recursive": "true", "SearchTerm": term, "Limit": 50,
                        "IncludeItemTypes": "Movie,Video,AdultVideo",
                        "MediaTypes": "Video", "Fields": "Path,People,MediaType"})
            await add_items(response)
            if media_by_id:
                break

    people_loaded = False
    for media in media_by_id.values():
        people = media.get("People") or []
        if not people:
            detail = await client.get(
                f"{base}/Items/{media['Id']}", headers=headers,
                params={"Fields": "Path,People"})
            if detail.status_code in (401, 403):
                detail.raise_for_status()
            if detail.status_code < 400:
                people = (detail.json() or {}).get("People") or []
        people_loaded = people_loaded or bool(people)
        for person in people:
            key = _name_key(person.get("Name", ""))
            original = pending.get(key)
            if original and person.get("Id"):
                found[original] = person
                pending.pop(key, None)
    return found, {"media_found": bool(media_by_id), "people_loaded": people_loaded}


def _primary_tag(item: dict) -> str:
    tags = item.get("ImageTags") or {}
    return (tags.get("Primary") or "").strip() if isinstance(tags, dict) else ""


async def _verify_primary(client: httpx.AsyncClient, base: str, headers: dict,
                          item_id: str, previous_tag: str = "") -> tuple[bool, str]:
    response = await client.get(f"{base}/Items/{item_id}/Images", headers=headers)
    response.raise_for_status()
    payload = response.json()
    primary = next((image for image in payload if
                    (image.get("ImageType") or "").casefold() == "primary"), None) \
        if isinstance(payload, list) else None
    if not primary:
        return False, ""
    current_tag = (primary.get("ImageTag") or primary.get("Tag") or "").strip()
    # Older Emby versions do not expose a tag from /Images. Presence remains
    # the only available verification in that compatibility case.
    return (not previous_tag or not current_tag or current_tag != previous_tag), current_tag


async def notify_media_paths(url: str, api_key: str, paths: list[str],
                             update_type: str = "Updated") -> dict:
    """Notify Emby about only the supplied media paths; never start a full-library scan."""
    base, token = normalize_url(url), (api_key or "").strip()
    unique = []
    for value in paths or []:
        path = (value or "").strip()
        if path and path not in unique:
            unique.append(path)
    if not base or not token or not unique:
        return {"triggered": False, "message": "Emby 未配置或缺少当前影片路径"}
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.post(
                f"{base}/Library/Media/Updated",
                headers={**_headers(token), "Content-Type": "application/json"},
                json={"Updates": [{"Path": path, "UpdateType": update_type}
                                  for path in unique]})
            if response.status_code in (401, 403):
                return {"triggered": False, "message": "API Key 没有权限通知当前影片目录"}
            response.raise_for_status()
        return {"triggered": True, "message": "已通知当前影片目录"}
    except Exception as e:
        return {"triggered": False, "message": f"Emby 定向通知失败：{e}"}


async def sync_person_images(url: str, api_key: str, portraits: list[dict],
                             media_paths: Optional[list[str]] = None,
                             poll_delays: tuple[float, ...] = (2, 5, 8),
                             notify_media: bool = True) -> dict:
    """Resolve Person IDs from the current movie and upload local portraits.

    The movie People relationship is authoritative and already contains the Person
    IDs needed for upload. Initial calls notify only the current media paths and wait
    briefly; persisted retries skip the notification and perform one lightweight
    movie lookup. We never enumerate the global Persons library or FullRefresh a Person.
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
            if notify_media:
                notify_paths = [path for path in paths
                                if os.path.splitext(path.replace("\\", "/"))[1].casefold() != ".nfo"]
                media_update = await client.post(
                    f"{base}/Library/Media/Updated",
                    headers={**headers, "Content-Type": "application/json"},
                    json={"Updates": [{"Path": path, "UpdateType": "Created"}
                                      for path in (notify_paths or paths[:1])]})
                if media_update.status_code in (401, 403):
                    return {"media_update_triggered": False, "results": [{
                        "name": item["name"], "updated": False,
                        "message": "API Key 没有权限通知当前影片目录"} for item in valid],
                        "message": "Emby 管理员权限不足"}
                media_update.raise_for_status()

            pending = {item["name"]: item for item in valid}
            people = {}
            media_state = {"media_found": False, "people_loaded": False}
            # 只轮询当前影片 People；所有演员共享同一次影片查询。
            delays = (0,) + tuple(poll_delays)
            for delay in delays:
                if delay:
                    await asyncio.sleep(delay)
                linked, current_state = await _find_people_from_media_state(
                    client, base, headers, list(pending), paths)
                media_state["media_found"] = (media_state["media_found"] or
                                              current_state["media_found"])
                media_state["people_loaded"] = (media_state["people_loaded"] or
                                                 current_state["people_loaded"])
                for name, person in linked.items():
                    people[name] = person
                    pending.pop(name, None)
                if not pending:
                    break

            if pending:
                if not media_state["media_found"]:
                    pending_message = "Emby 尚未导入当前影片，已进入延迟重试"
                elif not media_state["people_loaded"]:
                    pending_message = "Emby 已导入影片但演员关系尚未建立，已进入延迟重试"
                else:
                    pending_message = "Emby 当前影片中未找到同名演员，已进入延迟重试"
                for name in pending:
                    results[name]["message"] = pending_message

            for name, item in valid_by_name(valid).items():
                person = people.get(name)
                if not person:
                    continue
                item_id = person["Id"]
                previous_tag = _primary_tag(person)
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
                verified, current_tag = await _verify_primary(
                    client, base, headers, item_id, previous_tag)
                results[name] = {"name": name, "updated": verified,
                                 "person_id": item_id,
                                 "previous_image_tag": previous_tag,
                                 "image_tag": current_tag,
                                 "message": "头像已上传，Primary ImageTag 已变化"
                                            if verified and previous_tag and current_tag else
                                            "头像已上传并验证" if verified else
                                            "头像上传后 Primary ImageTag 未变化"}
            failed_names = [name for name, result in results.items()
                            if not result.get("updated")]
            return {"media_update_triggered": notify_media, "results": list(results.values()),
                    "retryable": bool(failed_names), "pending_names": failed_names,
                    "media_found": media_state["media_found"],
                    "people_loaded": media_state["people_loaded"],
                    "message": ("已通知当前影片目录，部分演员等待 Emby 完成入库"
                                if pending else
                                "部分演员头像上传或验证未完成，已进入延迟重试"
                                if failed_names else
                                "已通知当前影片目录，演员头像同步完成")}
    except Exception as exc:
        return {"media_update_triggered": False, "results": [{
            "name": item["name"], "updated": False,
            "message": f"Emby 更新失败：{exc}"} for item in valid],
            "retryable": True, "pending_names": [item["name"] for item in valid],
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
