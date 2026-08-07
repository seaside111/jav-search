"""Independent actor portrait scraper and background task API."""
import asyncio
import json
import re
import shutil
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Optional
from xml.dom import minidom

import httpx
from bs4 import BeautifulSoup
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config_manager import CONFIG_PATH, load as load_config
import emby
from scrapers import search, enrich, SEARCH_MODE_ACTOR, SEARCH_MODE_CODE
from scrapers import javdb as javdb_scraper

router = APIRouter(prefix="/api/actors")
_task: Optional[asyncio.Task] = None
_directory_task: Optional[asyncio.Task] = None
_emby_retry_task: Optional[asyncio.Task] = None
_state = {"running": False, "root": "", "total": 0, "processed": 0,
          "actors": 0, "saved": 0, "failed": 0, "emby_updated": 0, "current": "",
          "started": "", "finished": "", "message": "", "recent": []}
_image_exts = {".jpg", ".jpeg", ".png", ".webp"}
_video_exts = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".m4v", ".ts", ".webm"}
_pending_lock = asyncio.Lock()
_emby_pending_lock = asyncio.Lock()


class ActorRunRequest(BaseModel):
    root: str = ""
    overwrite: bool = False


class ActorSingleRequest(BaseModel):
    folder: str
    overwrite: bool = False


class EmbyTestRequest(BaseModel):
    url: str
    api_key: str


def _log(message: str):
    print(f"[演员头像 {datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def _key(name: str) -> str:
    return re.sub(r"[\s・·._-]+", "", (name or "")).casefold()


def _safe(name: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", (name or "").strip())
    return value.rstrip(" .")[:100] or "unknown"


def _code_key(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def _usable_avatar(url: str) -> bool:
    value = (url or "").strip().lower()
    return value.startswith("http") and not any(token in value for token in (
        "nowprinting", "noimage", "no_image", "no-photo", "nophoto",
        "placeholder", "/default-avatar", "/default_actor",
    ))


def _code_from_nfo(root: ET.Element, path: Path) -> str:
    for node in root.findall("uniqueid"):
        if (node.text or "").strip():
            return (node.text or "").strip().upper()
    candidates = [path.stem, path.parent.name]
    for value in candidates:
        match = re.search(r"(?i)(FC2(?:-PPV)?[-_ ]?\d{5,8}|[A-Z]{2,10}[-_ ]?\d{2,7})", value)
        if match:
            return re.sub(r"[_ ]+", "-", match.group(1)).upper()
    return ""


def _read_nfo(path: Path) -> tuple[ET.ElementTree, list, str]:
    tree = ET.parse(path)
    root = tree.getroot()
    actors = []
    known = {}
    for node in root.findall("actor"):
        name = (node.findtext("name") or "").strip()
        if name:
            avatar = (node.findtext("thumb") or "").strip()
            key = _key(name)
            if key not in known:
                actor = {"name": name, "avatar": avatar if _usable_avatar(avatar) else ""}
                actors.append(actor)
                known[key] = actor
            elif not _usable_avatar(known[key].get("avatar", "")) and _usable_avatar(avatar):
                known[key]["avatar"] = avatar
    return tree, actors, _code_from_nfo(root, path)


def _write_nfo(path: Path, tree: ET.ElementTree, actors: list, write_thumb: bool) -> int:
    root = tree.getroot()
    nodes = {_key(n.findtext("name") or ""): n for n in root.findall("actor")}
    written = 0
    for order, actor in enumerate(actors):
        name = (actor.get("name") or "").strip()
        if not name:
            continue
        node = nodes.get(_key(name))
        if node is None:
            node = ET.SubElement(root, "actor")
            ET.SubElement(node, "name").text = name
            ET.SubElement(node, "role").text = ""
            ET.SubElement(node, "order").text = str(order)
        thumb = node.find("thumb")
        avatar = (actor.get("avatar") or "").strip()
        if write_thumb and _usable_avatar(avatar):
            if thumb is None:
                thumb = ET.SubElement(node, "thumb")
            thumb.text = avatar
            written += 1
        elif actor.get("avatar_invalid") and thumb is not None:
            # 404/410 已确认永久失效，不把死链继续留作 NFO 回退地址。
            node.remove(thumb)
        elif not write_thumb and thumb is not None:
            node.remove(thumb)
    raw = ET.tostring(root, encoding="unicode")
    pretty = minidom.parseString(raw.encode("utf-8")).toprettyxml(indent="  ")
    lines = [line for line in pretty.splitlines() if line.strip()]
    lines[0] = '<?xml version="1.0" encoding="utf-8" standalone="yes"?>'
    path.write_text("\n".join(lines), encoding="utf-8")
    return written


def _merge(target: list, incoming: list) -> int:
    known = {_key(a.get("name", "")): a for a in target if a.get("name")}
    changed = 0
    for source in incoming or []:
        name = (source.get("name") or "").strip()
        if not name:
            continue
        key = _key(name)
        if key not in known:
            avatar = (source.get("avatar") or "").strip()
            actor = {"name": name, "avatar": avatar if _usable_avatar(avatar) else ""}
            target.append(actor)
            known[key] = actor
            changed += 1
        elif not _usable_avatar(known[key].get("avatar", "")):
            avatar = (source.get("avatar") or "").strip()
            if _usable_avatar(avatar):
                known[key]["avatar"] = avatar
                changed += 1
    return changed


def _sources(config: dict) -> list[str]:
    raw = config.get("actor_scrape_sources") or ["javbus", "avsox"]
    # AVMOO and AVSOX share the same javu backend. JavDB currently exposes
    # names but no portrait URLs, so neither should cause duplicate requests.
    aliases = {"avmoo": "avsox"}
    allowed = {"javbus", "avsox"}
    result = []
    for value in raw:
        source = aliases.get(str(value).strip().lower(), str(value).strip().lower())
        if source in allowed and source not in result:
            result.append(source)
    return result or ["javbus"]


def _request_interval(config: dict) -> float:
    """Return a bounded cooldown used by manual actor scraping."""
    try:
        return max(0.0, min(float(config.get("actor_scrape_interval_seconds", 2.0)), 60.0))
    except (TypeError, ValueError):
        return 2.0


def _pending_path(config: dict) -> Path:
    cache = Path(config.get("actor_scrape_cache_dir") or CONFIG_PATH.parent / "actor-cache")
    return cache / "javdb-pending.json"


def _load_pending(config: dict) -> dict:
    try:
        payload = json.loads(_pending_path(config).read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save_pending(config: dict, pending: dict):
    path = _pending_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")


def _emby_pending_path(config: dict) -> Path:
    cache = Path(config.get("actor_scrape_cache_dir") or CONFIG_PATH.parent / "actor-cache")
    return cache / "emby-pending.json"


def _load_emby_pending(config: dict) -> dict:
    try:
        payload = json.loads(_emby_pending_path(config).read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save_emby_pending(config: dict, pending: dict):
    path = _emby_pending_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


async def _queue_emby_pending(folder: Path, code: str, config: dict,
                              names: list[str]):
    """Persist an incomplete Emby import so a late Person creation is not missed."""
    key = str(folder.resolve())
    async with _emby_pending_lock:
        pending = _load_emby_pending(config)
        current = pending.get(key) if isinstance(pending.get(key), dict) else {}
        pending[key] = {
            "folder": key,
            "code": code or folder.name,
            "names": list(dict.fromkeys(name for name in names if name)),
            "attempts": int(current.get("attempts", 0)),
            "queued": current.get("queued") or datetime.now().isoformat(timespec="seconds"),
            "next_attempt": min(float(current.get("next_attempt", time.time() + 120)),
                                time.time() + 120),
        }
        _save_emby_pending(config, pending)


async def _remove_emby_pending(folder: Path, config: dict):
    key = str(folder.resolve())
    async with _emby_pending_lock:
        pending = _load_emby_pending(config)
        if pending.pop(key, None) is not None:
            _save_emby_pending(config, pending)


async def _queue_javdb_pending(actors: list[dict], folder: Path, config: dict):
    """Register only actors still missing after the normal low-cost sources."""
    def has_local(actor: dict) -> bool:
        path = folder / "actors" / f"{_safe(actor['name'])}.jpg"
        return path.exists() and path.stat().st_size > 1024

    missing = [a for a in actors if a.get("name") and not has_local(a)]
    if not missing or not config.get("actor_javdb_directory_enabled", True):
        return
    async with _pending_lock:
        pending = _load_pending(config)
        now = datetime.now().isoformat(timespec="seconds")
        for actor in missing:
            key = _key(actor["name"])
            item = pending.setdefault(key, {"name": actor["name"], "folders": [],
                                            "queued": now, "checks": 0})
            value = str(folder.resolve())
            if value not in item["folders"]:
                item["folders"].append(value)
        _save_pending(config, pending)
    _log(f"已登记 JavDB 低频待补：{', '.join(a['name'] for a in missing)}")


def _parse_javdb_actor_directory(html: str) -> list[dict]:
    """Parse actor cards defensively; JavDB has used several card layouts."""
    soup = BeautifulSoup(html or "", "html.parser")
    found = {}
    for link in soup.select('a[href*="/actors/"]'):
        name = (link.get("title") or link.get("aria-label") or link.get_text(" ", strip=True)).strip()
        img = link.select_one("img")
        avatar = ""
        if img:
            avatar = (img.get("data-src") or img.get("data-original") or
                      img.get("data-lazy-src") or img.get("src") or "").strip()
            name = name or (img.get("alt") or "").strip()
        if not avatar:
            styled = link.select_one("[style*='background-image']") or link
            match = re.search(r"background-image\s*:\s*url\(['\"]?([^)'\"]+)",
                              styled.get("style") or "", re.I)
            avatar = match.group(1).strip() if match else ""
        if avatar.startswith("//"):
            avatar = "https:" + avatar
        elif avatar.startswith("/"):
            avatar = javdb_scraper.JAVDB_BASE + avatar
        if name and _usable_avatar(avatar):
            found.setdefault(_key(name), {"name": name, "avatar": avatar,
                                          "source": "javdb"})
    return list(found.values())


async def _apply_directory_portrait(item: dict, folders: list[str], config: dict) -> dict:
    name, avatar = item["name"], item["avatar"]
    image = await _download(avatar, config.get("proxy") or None)
    if not image:
        return {"updated": False, "folders": 0, "emby_updated": 0}
    cache_dir = Path(config.get("actor_scrape_cache_dir") or CONFIG_PATH.parent / "actor-cache") / _safe(name)
    cache_dir.mkdir(parents=True, exist_ok=True)
    portrait = cache_dir / "portrait.jpg"
    portrait.write_bytes(image)
    (cache_dir / "metadata.json").write_text(json.dumps({
        "name": name, "url": avatar, "source": "javdb-directory",
        "updated": datetime.now().isoformat(timespec="seconds")},
        ensure_ascii=False, indent=2), encoding="utf-8")
    touched, emby_updated = 0, 0
    for value in folders:
        folder = Path(value)
        if not folder.is_dir():
            continue
        local = folder / "actors" / f"{_safe(name)}.jpg"
        local.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(portrait, local)
        nfo = next(folder.glob("*.nfo"), None)
        actors, code = [], ""
        if nfo:
            try:
                tree, actors, code = _read_nfo(nfo)
                for actor in actors:
                    if _key(actor.get("name", "")) == _key(name):
                        actor["avatar"] = avatar
                        actor["avatar_source"] = "javdb-directory"
                if config.get("actor_scrape_write_nfo", True):
                    _write_nfo(nfo, tree, actors,
                               config.get("scrape_actor_thumb_in_nfo", True))
            except Exception as exc:
                _log(f"后台头像更新 NFO 失败（{name}，{folder}）：{exc}")
        touched += 1
        if config.get("emby_actor_sync_enabled", False):
            result = await sync_emby_folder(folder, config, code, actors or [{"name": name}])
            emby_updated += result.get("emby_updated", 0)
    return {"updated": touched > 0, "folders": touched, "emby_updated": emby_updated}


async def run_javdb_directory_once(config: Optional[dict] = None) -> dict:
    """Fetch one directory page and consume only matching pending actors."""
    config = config or load_config()
    async with _pending_lock:
        pending = _load_pending(config)
    if not pending:
        return {"success": True, "pending": 0, "matched": 0, "updated": 0}
    html, status, error = await javdb_scraper._fetch_html(
        f"{javdb_scraper.JAVDB_BASE}/actors", config.get("proxy") or None,
        retries=0)
    if error or status != 200:
        return {"success": False, "pending": len(pending), "matched": 0,
                "updated": 0, "error": error or f"HTTP {status}"}
    directory = {_key(a["name"]): a for a in _parse_javdb_actor_directory(html)}
    matched = updated = emby_updated = 0
    for key, queued in list(pending.items()):
        actor = directory.get(key)
        queued["checks"] = int(queued.get("checks", 0)) + 1
        if not actor:
            continue
        matched += 1
        result = await _apply_directory_portrait(actor, queued.get("folders") or [], config)
        if result["updated"]:
            updated += 1
            emby_updated += result["emby_updated"]
            pending.pop(key, None)
        await asyncio.sleep(max(1.0, _request_interval(config)))
    async with _pending_lock:
        _save_pending(config, pending)
    _log(f"JavDB 演员目录低频扫描完成：待补 {len(pending)}，匹配 {matched}，更新 {updated}")
    return {"success": True, "pending": len(pending), "matched": matched,
            "updated": updated, "emby_updated": emby_updated}


async def _directory_loop():
    while True:
        config = load_config()
        hours = max(1.0, min(float(config.get("actor_javdb_directory_interval_hours", 12)), 168.0))
        # 启动后先等待完整周期，避免容器重启时立刻与首页/手动任务争抢 FlareSolverr。
        await asyncio.sleep(hours * 3600)
        if config.get("actor_javdb_directory_enabled", True):
            try:
                await run_javdb_directory_once(config)
            except Exception as exc:
                _log(f"JavDB 演员目录低频扫描失败：{exc}")


def start_directory_monitor():
    global _directory_task
    if _directory_task is None or _directory_task.done():
        _directory_task = asyncio.create_task(_directory_loop())
    return _directory_task


async def _details(query: str, mode: str, source: str, proxy: Optional[str]) -> list[dict]:
    try:
        items = await search(query=query, mode=mode, proxy=proxy,
                             sources=[source], max_results=6)
        selected = items[:3]
        if not selected:
            return []
        # JavBus 番号搜索会直接返回已加载的详情；不要再 enrich 一次，更不能让
        # 7 天磁盘缓存里的旧空头像覆盖本次实时解析到的头像。
        results = [None] * len(selected)
        pending_indexes = [i for i, item in enumerate(selected)
                           if not item.get("detail_loaded")]
        for i, item in enumerate(selected):
            if item.get("detail_loaded"):
                results[i] = item
        if pending_indexes:
            pending = await enrich([selected[i] for i in pending_indexes], proxy=proxy)
            for i, detail in zip(pending_indexes, pending):
                results[i] = detail
        return [item for item in results if item]
    except Exception as exc:
        _log(f"来源 {source} 查询失败：{query}: {exc}")
        return []


async def _actors_by_code(code: str, sources: list[str], proxy: Optional[str],
                          wanted: Optional[list[str]] = None) -> list:
    """Resolve source-by-source and stop as soon as the request is satisfied."""
    actors = []
    wanted_keys = {_key(name) for name in (wanted or []) if name}
    for source in sources:
        details = await _details(code, SEARCH_MODE_CODE, source, proxy)
        exact = [d for d in details if _code_key(d.get("code", "")) == _code_key(code)]
        for detail in exact or details[:1]:
            _merge(actors, detail.get("actors") or [])
        if not actors:
            continue
        found = {_key(a.get("name", "")): _usable_avatar(a.get("avatar", ""))
                 for a in actors}
        if not wanted_keys or all(found.get(key, False) for key in wanted_keys):
            _log(f"演员番号补查命中：{code}（{source}，{len(actors)} 位）")
            break
    return actors


async def _avatar_by_name(name: str, sources: list[str], proxy: Optional[str]) -> tuple[str, str]:
    # Current AVSOX/AVMOO javu endpoints only search movies/codes. JavDB actor
    # details expose names but no portrait URL. Sending actor names to them
    # produces HTTP/code 400 or expensive timeouts with no usable result.
    sources = [source for source in sources if source == "javbus"]
    wanted = _key(name)
    for source in sources:
        details = await _details(name, SEARCH_MODE_ACTOR, source, proxy)
        for detail in details:
            for actor in detail.get("actors") or []:
                avatar = (actor.get("avatar") or "").strip()
                if _key(actor.get("name", "")) == wanted and _usable_avatar(avatar):
                    return avatar, source
    return "", ""


async def _download(url: str, proxy: Optional[str], with_status: bool = False):
    def result(data: Optional[bytes], status: str):
        return (data, status) if with_status else data

    if not _usable_avatar(url):
        _log("头像下载跳过：来源未返回有效图片地址")
        return result(None, "invalid")
    referer = "https://javdb.com/" if ("javdb" in url or "jdbstatic" in url) \
        else "https://www.javbus.com/"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": referer,
               "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
    try:
        async with httpx.AsyncClient(proxy=proxy or None, timeout=30,
                                     follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            content_type = response.headers.get("content-type", "")
            if response.status_code == 200 and content_type.startswith("image/") and len(response.content) > 1024:
                return result(response.content, "ok")
            _log(f"头像下载无效：HTTP {response.status_code}，类型 {content_type or '未知'}，"
                 f"大小 {len(response.content)} 字节（{url}）")
            return result(None, "gone" if response.status_code in {404, 410} else "transient")
    except Exception as exc:
        _log(f"图片下载失败：{url}: {exc}")
    return result(None, "transient")


def _cached_image(name: str, config: dict) -> Optional[Path]:
    cache = Path(config.get("actor_scrape_cache_dir") or CONFIG_PATH.parent / "actor-cache")
    person = cache / _safe(name)
    for suffix in _image_exts:
        candidate = person / f"portrait{suffix}"
        if candidate.exists() and candidate.stat().st_size > 1024:
            return candidate
    return None


def _cached_avatar(name: str, config: dict) -> tuple[str, str]:
    """Restore the source URL saved alongside a cached portrait."""
    cache = Path(config.get("actor_scrape_cache_dir") or CONFIG_PATH.parent / "actor-cache")
    metadata = cache / _safe(name) / "metadata.json"
    try:
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        url = (payload.get("url") or "").strip()
        if _usable_avatar(url):
            return url, (payload.get("source") or "").strip()
    except (OSError, ValueError, TypeError):
        pass
    return "", ""


async def _save_actor(actor: dict, folder: Path, config: dict,
                      proxy: Optional[str], overwrite: bool) -> bool:
    name = (actor.get("name") or "").strip()
    if not name:
        return False
    local = folder / "actors" / f"{_safe(name)}.jpg"
    cache_dir = Path(config.get("actor_scrape_cache_dir") or CONFIG_PATH.parent / "actor-cache") / _safe(name)
    cached = _cached_image(name, config)
    if local.exists() and local.stat().st_size > 1024 and not overwrite:
        cached = local
    if cached is None or overwrite:
        downloaded = await _download((actor.get("avatar") or "").strip(), proxy,
                                     with_status=True)
        if isinstance(downloaded, tuple):
            image, download_status = downloaded
        else:  # 兼容测试替身及旧的自定义下载包装
            image, download_status = downloaded, ("ok" if downloaded else "transient")
        if not image:
            if download_status == "gone":
                actor["avatar"] = ""
                actor["avatar_invalid"] = True
            return False
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached = cache_dir / "portrait.jpg"
        cached.write_bytes(image)
    if cached is None:
        return False
    # Old releases could leave a usable movie-local image without a global
    # portrait or metadata. Preserve it globally and persist a recovered URL.
    cache_dir.mkdir(parents=True, exist_ok=True)
    global_cached = _cached_image(name, config)
    if global_cached is None:
        global_cached = cache_dir / "portrait.jpg"
        if cached != global_cached:
            shutil.copy2(cached, global_cached)
    avatar = (actor.get("avatar") or "").strip()
    if _usable_avatar(avatar):
        (cache_dir / "metadata.json").write_text(json.dumps({
            "name": name, "url": avatar, "source": actor.get("avatar_source", ""),
            "updated": datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False, indent=2), encoding="utf-8")
    local.parent.mkdir(parents=True, exist_ok=True)
    if overwrite or not local.exists():
        shutil.copy2(cached, local)
    people = (config.get("scrape_actor_images_dir") or "").strip()
    if people:
        person = Path(people) / _safe(name)
        person.mkdir(parents=True, exist_ok=True)
        for filename in ("folder.jpg", "portrait.jpg"):
            destination = person / filename
            if overwrite or not destination.exists():
                shutil.copy2(cached, destination)
    return True


def _emby_media_paths(folder: Path, config: dict) -> tuple[list[str], str]:
    """Map the archived folder to paths visible inside the Emby server/container."""
    local_folder = folder.resolve()
    local_root_value = (config.get("scrape_output_dir") or "").strip()
    emby_root = (config.get("emby_media_root") or "").strip()
    if emby_root:
        if not local_root_value:
            return [], "配置了 Emby 归档根路径，但本项目归档目录为空"
        try:
            relative = local_folder.relative_to(Path(local_root_value).resolve())
        except ValueError:
            return [], f"当前目录不在项目归档根路径内，无法映射到 Emby：{local_folder}"
        remote_folder = str(PurePosixPath(emby_root, *relative.parts))
    else:
        remote_folder = str(local_folder)

    paths = [remote_folder]
    for child in sorted(folder.iterdir() if folder.is_dir() else []):
        if child.is_file() and (child.suffix.lower() in _video_exts or child.suffix.lower() == ".nfo"):
            if emby_root:
                paths.append(str(PurePosixPath(remote_folder, child.name)))
            else:
                paths.append(str(child.resolve()))
    return paths, ""


async def notify_emby_folder(folder: Path, config: dict) -> dict:
    """Notify Emby to refresh only this archived movie folder after an artwork backfill."""
    media_paths, path_error = _emby_media_paths(folder, config)
    if path_error:
        return {"triggered": False, "message": path_error}
    # The folder path is sufficient for Emby to discover newly added local artwork.
    return await emby.notify_media_paths(
        config.get("emby_url", ""), config.get("emby_api_key", ""),
        media_paths[:1], update_type="Updated")


async def sync_emby_folder(folder: Path, config: dict, code: str = "",
                           actors: Optional[list] = None,
                           queue_retry: bool = True,
                           notify_media: bool = True,
                           poll_delays: Optional[tuple[float, ...]] = None) -> dict:
    """After archive completion, notify only this Emby folder and sync its portraits."""
    if not config.get("emby_actor_sync_enabled", False):
        return {"emby_updated": 0, "results": [], "message": "Emby 同步未启用"}
    if actors is None:
        nfo = next(folder.glob("*.nfo"), None) if folder.is_dir() else None
        if not nfo:
            return {"emby_updated": 0, "results": [], "message": "归档目录中没有 NFO"}
        try:
            _tree, actors, nfo_code = _read_nfo(nfo)
            code = code or nfo_code
        except Exception as exc:
            return {"emby_updated": 0, "results": [], "message": f"读取归档 NFO 失败：{exc}"}

    portraits = []
    for actor in actors or []:
        name = (actor.get("name") or "").strip()
        local = folder / "actors" / f"{_safe(name)}.jpg"
        if name and local.exists() and local.stat().st_size > 1024:
            portraits.append({"name": name, "image": local.read_bytes(),
                              "content_type": "image/jpeg"})
    if not portraits:
        return {"emby_updated": 0, "results": [], "message": "归档目录中没有可用演员头像"}

    media_paths, path_error = _emby_media_paths(folder, config)
    if path_error:
        _log(f"Emby 当前影片目录映射失败：{path_error}")
        return {"emby_updated": 0, "results": [{
            "name": item["name"], "updated": False, "message": path_error} for item in portraits],
            "message": path_error}

    _log(f"Emby 定向通知当前影片目录并同步演员头像：{code or folder.name}"
         f"（演员 {len(portraits)} 位，目录 {media_paths[0]}）")
    sync_options = {"media_paths": media_paths}
    if not notify_media:
        sync_options["notify_media"] = False
    if poll_delays is not None:
        sync_options["poll_delays"] = poll_delays
    batch = await emby.sync_person_images(
        config.get("emby_url", ""), config.get("emby_api_key", ""), portraits,
        **sync_options)
    by_name = {_key(item.get("name", "")): item for item in batch.get("results") or []}
    for actor in actors or []:
        result = by_name.get(_key(actor.get("name", "")), {})
        actor["emby_updated"] = bool(result.get("updated"))
        actor["emby_message"] = result.get("message", "")
        if result and not result.get("updated"):
            _log(f"Emby 演员头像同步失败（{actor['name']}）：{result.get('message', '')}")
    updated = sum(1 for actor in actors or [] if actor.get("emby_updated"))
    if queue_retry and batch.get("retryable"):
        await _queue_emby_pending(
            folder, code or folder.name, config,
            batch.get("pending_names") or [item["name"] for item in portraits])
        _log(f"Emby 演员头像已进入延迟重试：{code or folder.name}"
             f"（{len(batch.get('pending_names') or portraits)} 位）")
    elif queue_retry and not batch.get("retryable"):
        await _remove_emby_pending(folder, config)
    _log(f"Emby 当前影片目录同步完成：{code or folder.name}"
         f"（头像 {len(portraits)}，更新 {updated}）")
    return {**batch, "actors": actors or [], "emby_updated": updated}


async def run_emby_pending_once(config: Optional[dict] = None) -> dict:
    """Retry one due movie; retain failures with a bounded backoff until they succeed."""
    config = config or load_config()
    if not config.get("emby_actor_sync_enabled", False):
        return {"processed": 0, "pending": len(_load_emby_pending(config))}
    async with _emby_pending_lock:
        pending = _load_emby_pending(config)
        due = next(((key, item) for key, item in pending.items()
                    if isinstance(item, dict) and
                    float(item.get("next_attempt", 0)) <= time.time()), None)
    if not due:
        return {"processed": 0, "pending": len(pending)}

    key, task = due
    folder = Path(task.get("folder") or key)
    if not folder.is_dir():
        async with _emby_pending_lock:
            current = _load_emby_pending(config)
            current.pop(key, None)
            _save_emby_pending(config, current)
        _log(f"Emby 延迟重试已移除：归档目录不存在（{folder}）")
        return {"processed": 1, "pending": max(0, len(pending) - 1), "removed": True}

    retry_actors = [{"name": name} for name in (task.get("names") or []) if name]
    result = await sync_emby_folder(
        folder, config, task.get("code") or folder.name,
        actors=retry_actors or None, queue_retry=False,
        notify_media=False, poll_delays=())
    if result.get("emby_updated", 0) > 0 and not result.get("retryable"):
        await _remove_emby_pending(folder, config)
        _log(f"Emby 延迟重试成功：{task.get('code') or folder.name}"
             f"（更新 {result.get('emby_updated', 0)} 位）")
    else:
        async with _emby_pending_lock:
            current = _load_emby_pending(config)
            saved = current.get(key)
            if isinstance(saved, dict):
                attempts = int(saved.get("attempts", 0)) + 1
                # 2/5/15/30/60 分钟，之后每 6 小时继续尝试；单轮只查当前影片一次。
                delays = (300, 900, 1800, 3600)
                saved["attempts"] = attempts
                saved["next_attempt"] = time.time() + (
                    delays[attempts - 1] if attempts <= len(delays) else 21600)
                saved["last_message"] = result.get("message", "Emby 同步尚未完成")
                _save_emby_pending(config, current)
        _log(f"Emby 延迟重试仍待处理：{task.get('code') or folder.name}"
             f"（第 {int(task.get('attempts', 0)) + 1} 次）")
    return {"processed": 1, "pending": len(_load_emby_pending(config)),
            "emby_updated": result.get("emby_updated", 0)}


async def _emby_retry_loop():
    await asyncio.sleep(60)
    while True:
        try:
            await run_emby_pending_once(load_config())
        except Exception as exc:
            _log(f"Emby 演员头像延迟重试异常：{exc}")
        await asyncio.sleep(60)


def start_emby_retry_monitor():
    global _emby_retry_task
    if _emby_retry_task is None or _emby_retry_task.done():
        _emby_retry_task = asyncio.create_task(_emby_retry_loop())
    return _emby_retry_task


async def process_movie(folder: Path, actors: list, code: str, config: dict,
                        nfo_path: Optional[Path] = None, tree: Optional[ET.ElementTree] = None,
                        overwrite: bool = False, sync_emby: bool = True) -> dict:
    """Resolve and save portraits for one movie independently of movie scraping."""
    proxy = config.get("proxy") or None
    sources = _sources(config)
    actors = [dict(a) for a in (actors or []) if a.get("name")]
    for actor in actors:
        if not _usable_avatar(actor.get("avatar", "")):
            avatar, source = _cached_avatar(actor["name"], config)
            if avatar:
                actor["avatar"], actor["avatar_source"] = avatar, source
    if not actors and code and config.get("actor_scrape_lookup_by_code", True):
        actors = await _actors_by_code(code, sources, proxy)
    elif actors and code and config.get("actor_scrape_lookup_by_code", True):
        # One code lookup per source is cheaper and more accurate than searching
        # every actor by name; AVSOX/AVMOO can provide portraits on movie details.
        missing = [a for a in actors if not _usable_avatar(a.get("avatar", ""))]
        if missing:
            _log(f"演员头像待补全：{code}（{len(missing)} 位，先按番号逐源查询）")
            code_actors = await _actors_by_code(
                code, sources, proxy, [a["name"] for a in missing])
            _merge(actors, code_actors)
            resolved = sum(1 for actor in missing
                           if _usable_avatar(actor.get("avatar", "")))
            if not resolved:
                _log(f"演员番号补查未取得头像：{code}，转为按演员名回退查询")
    # A code lookup may discover new actors whose portrait URL is absent from
    # the source response but already known by the persistent actor cache.
    for actor in actors:
        if not _usable_avatar(actor.get("avatar", "")):
            avatar, source = _cached_avatar(actor["name"], config)
            if avatar:
                actor["avatar"], actor["avatar_source"] = avatar, source
    saved = 0
    interval = _request_interval(config)
    for actor_index, actor in enumerate(actors):
        # A movie with several missing actors can otherwise issue continuous
        # name fallbacks and forced image downloads. Keep those operations
        # serial and paced; cache-only copies do not need a cooldown.
        needs_network = (not _usable_avatar(actor.get("avatar", "")) or overwrite or
                         _cached_image(actor.get("name", ""), config) is None)
        if actor_index and needs_network and interval:
            _state["message"] = f"请求冷却 {interval:g} 秒"
            await asyncio.sleep(interval)
        if not _usable_avatar(actor.get("avatar", "")):
            avatar, source = await _avatar_by_name(actor["name"], sources, proxy)
            actor["avatar"], actor["avatar_source"] = avatar, source
        if await _save_actor(actor, folder, config, proxy, overwrite):
            saved += 1
    # 首轮现有来源到此即结束。仍缺头像者只登记给 JavDB 目录后台任务，
    # 不在影片任务中同步重试需要过盾的 JavDB。
    await _queue_javdb_pending(actors, folder, config)
    # 必须先把番号补查新增的演员与头像 URL 写回 NFO，再让 Emby 扫描；
    # 否则新 Person/影片演员关系可能直到下一次手工刷新才出现。
    if nfo_path and tree is not None and actors and config.get("actor_scrape_write_nfo", True):
        write_thumb = config.get("scrape_actor_thumb_in_nfo", True)
        written = _write_nfo(nfo_path, tree, actors, write_thumb)
        if write_thumb:
            _log(f"NFO 演员头像 URL 已写入：{code or folder.name}（{written}/{len(actors)} 位）")
    if sync_emby and config.get("emby_actor_sync_enabled", False) and saved:
        await sync_emby_folder(folder, config, code, actors)
    emby_updated = sum(1 for actor in actors if actor.get("emby_updated"))
    failed = max(0, len(actors) - saved)
    emby_status = (f"Emby 更新 {emby_updated}" if sync_emby
                   else "Emby 待归档后定向同步")
    _log(f"演员头像处理完成：{code or folder.name}（演员 {len(actors)}，本地可用 {saved}，"
         f"失败 {failed}，{emby_status}）")
    return {"success": True, "code": code, "actors": actors, "saved": saved,
            "failed": failed, "emby_updated": emby_updated,
            "emby_pending": bool(not sync_emby and config.get("emby_actor_sync_enabled", False) and saved)}


async def process_nfo(path: Path, config: dict, overwrite: bool = False,
                      sync_emby: bool = True) -> dict:
    try:
        tree, actors, code = _read_nfo(path)
        return await process_movie(path.parent, actors, code, config, path, tree,
                                   overwrite, sync_emby)
    except Exception as exc:
        return {"success": False, "file": str(path), "saved": 0, "actors": [], "error": str(exc)}


async def _run(root: Path, config: dict, overwrite: bool):
    global _state
    nfos = sorted(root.rglob("*.nfo"))
    _state.update({"running": True, "root": str(root), "total": len(nfos), "processed": 0,
                   "actors": 0, "saved": 0, "failed": 0, "emby_updated": 0, "current": "",
                   "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "finished": "",
                   "message": "正在扫描", "recent": []})
    try:
        interval = _request_interval(config)
        for index, path in enumerate(nfos):
            if index and interval:
                _state["message"] = f"批量任务冷却 {interval:g} 秒"
                await asyncio.sleep(interval)
            _state["current"] = str(path)
            _state["message"] = "正在扫描"
            result = await process_nfo(path, config, overwrite)
            _state["processed"] += 1
            _state["actors"] += len(result.get("actors") or [])
            _state["saved"] += result.get("saved", 0)
            _state["failed"] += result.get("failed", 0) if result.get("success") else 1
            _state["emby_updated"] += result.get("emby_updated", 0)
            _state["recent"].insert(0, {"file": str(path), **result})
            del _state["recent"][20:]
        _state["message"] = "扫描完成"
    finally:
        _state["running"] = False
        _state["current"] = ""
        _state["finished"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@router.get("/status")
async def status():
    return dict(_state)


@router.post("/emby/test")
async def test_emby(req: EmbyTestRequest):
    api_key = req.api_key.strip()
    if api_key.startswith("***"):
        api_key = (load_config().get("emby_api_key") or "").strip()
    return await emby.test_connection(req.url, api_key)


@router.post("/run")
async def run(req: ActorRunRequest):
    global _task
    if _task and not _task.done():
        raise HTTPException(status_code=409, detail="演员头像任务正在运行")
    config = load_config()
    value = req.root.strip() or config.get("scrape_output_dir", "").strip()
    root = Path(value) if value else None
    if not root or not root.is_dir():
        raise HTTPException(status_code=400, detail="请配置有效的归档目录或指定扫描目录")
    _task = asyncio.create_task(_run(root, config, req.overwrite))
    return {"success": True, "root": str(root)}


@router.post("/single")
async def single(req: ActorSingleRequest):
    folder = Path(req.folder)
    if not folder.is_dir():
        raise HTTPException(status_code=404, detail="影片目录不存在")
    nfo = next(folder.glob("*.nfo"), None)
    if not nfo:
        raise HTTPException(status_code=404, detail="影片目录中没有 NFO")
    return await process_nfo(nfo, load_config(), req.overwrite)


@router.post("/javdb-directory/run")
async def run_javdb_directory():
    return await run_javdb_directory_once(load_config())


@router.get("/javdb-directory/status")
async def javdb_directory_status():
    config = load_config()
    pending = _load_pending(config)
    return {"running": bool(_directory_task and not _directory_task.done()),
            "enabled": config.get("actor_javdb_directory_enabled", True),
            "pending": len(pending),
            "actors": [item.get("name", "") for item in pending.values()]}
