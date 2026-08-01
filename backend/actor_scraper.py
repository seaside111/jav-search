"""Independent actor portrait scraper and background task API."""
import asyncio
import json
import re
import shutil
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Optional
from xml.dom import minidom

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config_manager import CONFIG_PATH, load as load_config
import emby
from scrapers import search, enrich, SEARCH_MODE_ACTOR, SEARCH_MODE_CODE

router = APIRouter(prefix="/api/actors")
_task: Optional[asyncio.Task] = None
_state = {"running": False, "root": "", "total": 0, "processed": 0,
          "actors": 0, "saved": 0, "failed": 0, "emby_updated": 0, "current": "",
          "started": "", "finished": "", "message": "", "recent": []}
_image_exts = {".jpg", ".jpeg", ".png", ".webp"}


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
    for node in root.findall("actor"):
        name = (node.findtext("name") or "").strip()
        if name:
            actors.append({"name": name, "avatar": (node.findtext("thumb") or "").strip()})
    return tree, actors, _code_from_nfo(root, path)


def _write_nfo(path: Path, tree: ET.ElementTree, actors: list, write_thumb: bool):
    root = tree.getroot()
    nodes = {_key(n.findtext("name") or ""): n for n in root.findall("actor")}
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
        if write_thumb and avatar.startswith("http"):
            if thumb is None:
                thumb = ET.SubElement(node, "thumb")
            thumb.text = avatar
        elif not write_thumb and thumb is not None:
            node.remove(thumb)
    raw = ET.tostring(root, encoding="unicode")
    pretty = minidom.parseString(raw.encode("utf-8")).toprettyxml(indent="  ")
    lines = [line for line in pretty.splitlines() if line.strip()]
    lines[0] = '<?xml version="1.0" encoding="utf-8" standalone="yes"?>'
    path.write_text("\n".join(lines), encoding="utf-8")


def _merge(target: list, incoming: list) -> int:
    known = {_key(a.get("name", "")): a for a in target if a.get("name")}
    changed = 0
    for source in incoming or []:
        name = (source.get("name") or "").strip()
        if not name:
            continue
        key = _key(name)
        if key not in known:
            actor = {"name": name, "avatar": (source.get("avatar") or "").strip()}
            target.append(actor)
            known[key] = actor
            changed += 1
        elif not (known[key].get("avatar") or "").startswith("http"):
            avatar = (source.get("avatar") or "").strip()
            if avatar.startswith("http"):
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
        found = {_key(a.get("name", "")): (a.get("avatar") or "").startswith("http")
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
                if _key(actor.get("name", "")) == wanted and avatar.startswith("http"):
                    return avatar, source
    return "", ""


async def _download(url: str, proxy: Optional[str]) -> Optional[bytes]:
    if not url.startswith("http"):
        _log("头像下载跳过：来源未返回有效图片地址")
        return None
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.javbus.com/",
               "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
    try:
        async with httpx.AsyncClient(proxy=proxy or None, timeout=30,
                                     follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            content_type = response.headers.get("content-type", "")
            if response.status_code == 200 and content_type.startswith("image/") and len(response.content) > 1024:
                return response.content
            _log(f"头像下载无效：HTTP {response.status_code}，类型 {content_type or '未知'}，"
                 f"大小 {len(response.content)} 字节（{url}）")
    except Exception as exc:
        _log(f"图片下载失败：{url}: {exc}")
    return None


def _cached_image(name: str, config: dict) -> Optional[Path]:
    cache = Path(config.get("actor_scrape_cache_dir") or CONFIG_PATH.parent / "actor-cache")
    person = cache / _safe(name)
    for suffix in _image_exts:
        candidate = person / f"portrait{suffix}"
        if candidate.exists() and candidate.stat().st_size > 1024:
            return candidate
    return None


async def _save_actor(actor: dict, folder: Path, config: dict,
                      proxy: Optional[str], overwrite: bool) -> bool:
    name = (actor.get("name") or "").strip()
    if not name:
        return False
    local = folder / "actors" / f"{_safe(name)}.jpg"
    cached = _cached_image(name, config)
    if local.exists() and not overwrite:
        cached = local
    if cached is None or overwrite:
        image = await _download((actor.get("avatar") or "").strip(), proxy)
        if not image:
            return False
        cache_dir = Path(config.get("actor_scrape_cache_dir") or CONFIG_PATH.parent / "actor-cache") / _safe(name)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached = cache_dir / "portrait.jpg"
        cached.write_bytes(image)
        (cache_dir / "metadata.json").write_text(json.dumps({
            "name": name, "url": actor.get("avatar", ""), "source": actor.get("avatar_source", ""),
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
    if config.get("emby_actor_sync_enabled", False):
        image = cached.read_bytes()
        result = await emby.update_person_image(
            config.get("emby_url", ""), config.get("emby_api_key", ""), name, image)
        actor["emby_updated"] = bool(result.get("updated"))
        actor["emby_message"] = result.get("message", "")
        if not result.get("updated"):
            _log(f"Emby actor sync skipped/failed ({name}): {result.get('message', '')}")
    return True


async def process_movie(folder: Path, actors: list, code: str, config: dict,
                        nfo_path: Optional[Path] = None, tree: Optional[ET.ElementTree] = None,
                        overwrite: bool = False) -> dict:
    """Resolve and save portraits for one movie independently of movie scraping."""
    proxy = config.get("proxy") or None
    sources = _sources(config)
    actors = [dict(a) for a in (actors or []) if a.get("name")]
    if not actors and code and config.get("actor_scrape_lookup_by_code", True):
        actors = await _actors_by_code(code, sources, proxy)
    elif actors and code:
        # One code lookup per source is cheaper and more accurate than searching
        # every actor by name; AVSOX/AVMOO can provide portraits on movie details.
        missing = [a for a in actors if not (a.get("avatar") or "").startswith("http")
                   and _cached_image(a["name"], config) is None]
        if missing:
            _log(f"演员头像待补全：{code}（{len(missing)} 位，先按番号逐源查询）")
            code_actors = await _actors_by_code(
                code, sources, proxy, [a["name"] for a in missing])
            _merge(actors, code_actors)
            resolved = sum(1 for actor in missing
                           if (actor.get("avatar") or "").startswith("http"))
            if not resolved:
                _log(f"演员番号补查未取得头像：{code}，转为按演员名回退查询")
    saved = 0
    for actor in actors:
        local_cached = folder / "actors" / f"{_safe(actor['name'])}.jpg"
        has_cache = local_cached.exists() or _cached_image(actor["name"], config) is not None
        if (overwrite or not has_cache) and not (actor.get("avatar") or "").startswith("http"):
            avatar, source = await _avatar_by_name(actor["name"], sources, proxy)
            actor["avatar"], actor["avatar_source"] = avatar, source
        if await _save_actor(actor, folder, config, proxy, overwrite):
            saved += 1
    if nfo_path and tree is not None and actors and config.get("actor_scrape_write_nfo", True):
        _write_nfo(nfo_path, tree, actors, config.get("scrape_actor_thumb_in_nfo", True))
    emby_updated = sum(1 for actor in actors if actor.get("emby_updated"))
    failed = max(0, len(actors) - saved)
    _log(f"演员头像处理完成：{code or folder.name}（演员 {len(actors)}，本地可用 {saved}，"
         f"失败 {failed}，Emby 更新 {emby_updated}）")
    return {"success": True, "code": code, "actors": actors, "saved": saved,
            "failed": failed, "emby_updated": emby_updated}


async def process_nfo(path: Path, config: dict, overwrite: bool = False) -> dict:
    try:
        tree, actors, code = _read_nfo(path)
        return await process_movie(path.parent, actors, code, config, path, tree, overwrite)
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
        for path in nfos:
            _state["current"] = str(path)
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
