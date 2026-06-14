"""
JAV Search — FastAPI 后端主程序
"""
import os
import re
import sys
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response as FastResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# 确保 backend 目录在 Python 路径中
sys.path.insert(0, str(Path(__file__).parent))

from scrapers import (
    search, enrich, get_latest, detect_search_mode,
    SEARCH_MODE_CODE, SEARCH_MODE_ACTOR, SEARCH_MODE_KEYWORD,
)
from scrapers import javdb as javdb_scraper
from scrapers import fc2 as fc2_scraper
from translator import translate
from config_manager import load as load_config, save as save_config, MAX_RESULTS_HARD_CAP
import jackett
from jackett import search_jackett
from scrapers._sukebei import search_sukebei
import qbittorrent
import downloader
import imagehost
import mteam
import crossseed
import publish
import monitor
import mteam_enums
import logbus
import library
import intake
import auth

APP_VERSION = "1.5.0-beta"
# 版本更新检测用的 GitHub 仓库（owner/repo）
GITHUB_REPO = "seaside111/jav-search"

app = FastAPI(title="JAV Search", version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 媒体库刮削路由（手动扫描/刮削 + 监控状态）
app.include_router(library.router)
# 发种流水线路由（V1.5）
app.include_router(publish.router)
# 监控路由（V1.5）
app.include_router(monitor.router)


@app.on_event("startup")
async def _on_startup():
    # 按配置初始化日志详略
    try:
        logbus.set_verbose(load_config().get("log_verbose", True))
    except Exception:
        pass
    # 启动时若已配 M-Team 密钥且本地无枚举缓存文件，后台抓取一次（持久化供发种自动选择）
    try:
        _cfg = load_config()
        if _cfg.get("mteam_api_key") and not mteam_enums._load_file():
            import asyncio as _asyncio
            _asyncio.create_task(mteam_enums.refresh_conf(_cfg))
    except Exception as e:
        print(f"[启动] M-Team 枚举预取失败: {e}", flush=True)
    # 按配置拉起后台刮削监控
    print(f"[启动] JAV Search {APP_VERSION} 启动完成，初始化刮削监控…", flush=True)
    try:
        library.start_monitor()
    except Exception as e:
        print(f"[启动] 刮削监控启动失败: {e}", flush=True)
    try:
        publish.start_worker()
    except Exception as e:
        print(f"[启动] 发种 worker 启动失败: {e}", flush=True)
    # 推送入库后台轮询：磁力下完即删（保留文件）+ 给刮削补记下载内容名
    try:
        intake.start_poller(load_config)
    except Exception as e:
        print(f"[启动] 推送入库轮询启动失败: {e}", flush=True)

# ──────────────────────────────────────────────
# 认证：放行白名单 + Cookie 校验中间件
# ──────────────────────────────────────────────
# 不需要登录即可访问的路径
_AUTH_WHITELIST = {"/login", "/api/login", "/api/health", "/api/auth/status"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # 认证未启用：直接放行
    if not auth.AUTH_ENABLED:
        return await call_next(request)

    path = request.url.path

    # 白名单路径放行
    if path in _AUTH_WHITELIST:
        return await call_next(request)

    # 静态资源（如有）放行
    if path.startswith("/static") or path.startswith("/favicon"):
        return await call_next(request)

    # 校验 cookie 中的会话 token
    token = request.cookies.get(auth.COOKIE_NAME, "")
    if auth.verify_token(token):
        return await call_next(request)

    # 未通过：API 返回 401 JSON，页面请求重定向到 /login
    if path.startswith("/api/"):
        return JSONResponse(status_code=401, content={"detail": "未登录或会话已过期", "auth_required": True})
    else:
        return HTMLResponse(
            content='<meta http-equiv="refresh" content="0; url=/login">',
            status_code=200,
        )


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/login")
async def api_login(req: LoginRequest):
    if not auth.AUTH_ENABLED:
        return {"success": True, "auth_enabled": False}
    if auth.verify_credentials(req.username, req.password):
        token = auth.create_token()
        resp = JSONResponse(content={"success": True})
        resp.set_cookie(
            key=auth.COOKIE_NAME,
            value=token,
            max_age=auth.SESSION_TTL,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return resp
    return JSONResponse(status_code=401, content={"success": False, "detail": "账号或密码错误"})


@app.post("/api/logout")
async def api_logout():
    resp = JSONResponse(content={"success": True})
    resp.delete_cookie(key=auth.COOKIE_NAME, path="/")
    return resp


@app.get("/api/auth/status")
async def api_auth_status(request: Request):
    if not auth.AUTH_ENABLED:
        return {"auth_enabled": False, "logged_in": True}
    token = request.cookies.get(auth.COOKIE_NAME, "")
    return {"auth_enabled": True, "logged_in": auth.verify_token(token)}


# ──────────────────────────────────────────────
# 数据模型
# ──────────────────────────────────────────────

class SearchRequest(BaseModel):
    query: str
    mode: Optional[str] = "auto"   # auto | code | actor | keyword
    sources: Optional[list[str]] = None
    max_results: Optional[int] = None  # 覆盖配置中的上限（受硬上限约束）


class DetailItem(BaseModel):
    url: str
    source: str


class DetailsRequest(BaseModel):
    items: list[DetailItem]        # 需补全详情的条目（前台当前页 + 预取下一页）


class TranslateRequest(BaseModel):
    text: str
    provider: str = "baidu"        # baidu | aliyun
    from_lang: str = "auto"
    to_lang: str = "zh"


class JackettSearchRequest(BaseModel):
    query: str                     # 通常填番号
    indexers: Optional[str] = None # 覆盖配置中的 indexers


class ConfigUpdateRequest(BaseModel):
    # 注意：所有字段默认必须是 None。保存接口用 req.dict(exclude_none=True) 合并，
    # 只有 None 才会被排除。若默认给 "" / "baidu" / 12 之类的非 None 值，则当某个
    # 设置页（如「发种中心」/publish）保存时未提交这些字段，Pydantic 会用默认值填上，
    # exclude_none 排不掉，从而把用户已存的 proxy / jackett / 翻译密钥等悄悄清空。
    proxy: Optional[str] = None
    sources: Optional[list[str]] = None
    # V1.4.2 JavDB 反爬增强
    javdb_flaresolverr_url: Optional[str] = None
    javdb_flaresolverr_use_proxy: Optional[bool] = None
    javdb_cookie: Optional[str] = None
    javdb_prefetch_extras: Optional[bool] = None
    # V1.4.3 FC2 数据源
    fc2_flaresolverr_url: Optional[str] = None
    fc2_flaresolverr_use_proxy: Optional[bool] = None
    fc2_cookie: Optional[str] = None
    fc2_missav_enabled: Optional[bool] = None
    fc2_missav_base: Optional[str] = None
    # V1.4.4 FC2 最新片源抓取页数（前 N 页跨页去重 + 编号降序汇总）
    fc2_latest_pages: Optional[int] = None
    # V1.4.4 FC2 最新优先用 sukebei 发现（更新更快、直连不过盾）
    fc2_latest_use_sukebei: Optional[bool] = None
    # V1.4.4 后台预抓 FC2 最新的 MissAV（标题/封面/样品图，低负担）
    fc2_prefetch_missav: Optional[bool] = None
    fc2_prefetch_count: Optional[int] = None
    baidu_app_id: Optional[str] = None
    baidu_secret_key: Optional[str] = None
    aliyun_access_key_id: Optional[str] = None
    aliyun_access_key_secret: Optional[str] = None
    default_translate_provider: Optional[str] = None
    results_per_page: Optional[int] = None
    max_results: Optional[int] = None
    show_latest: Optional[bool] = None
    latest_sources: Optional[list[str]] = None
    latest_per_source: Optional[int] = None
    latest_limits: Optional[dict] = None
    jackett_enabled: Optional[bool] = None
    jackett_url: Optional[str] = None
    jackett_api_key: Optional[str] = None
    jackett_indexers: Optional[str] = None
    jackett_timeout: Optional[int] = None
    # V1.5 下载器类型
    downloader_type: Optional[str] = None
    magnet_upload_limit_kbps: Optional[int] = None   # 磁力推送单种上传限速(KB/s)
    magnet_delete_completed: Optional[bool] = None    # 磁力下载完成后自动删种(保留文件)
    # V1.4 qBittorrent
    qb_url: Optional[str] = None
    qb_username: Optional[str] = None
    qb_password: Optional[str] = None
    qb_save_path: Optional[str] = None
    qb_category: Optional[str] = None
    qb_paused: Optional[bool] = None
    # V1.5 Transmission
    tr_url: Optional[str] = None
    tr_username: Optional[str] = None
    tr_password: Optional[str] = None
    tr_save_path: Optional[str] = None
    tr_category: Optional[str] = None
    # V1.5 M-Team PT
    mteam_api_base: Optional[str] = None
    mteam_api_key: Optional[str] = None
    mteam_uid: Optional[str] = None
    mteam_source_flag: Optional[str] = None
    crossseed_category: Optional[str] = None
    # V1.5 发种流水线
    publish_work_dir: Optional[str] = None
    publish_work_dir_host: Optional[str] = None
    publish_max_active: Optional[int] = None
    publish_stop_ratio: Optional[float] = None
    publish_stop_hours: Optional[float] = None
    publish_delete_after_stop: Optional[bool] = None
    publish_delete_files: Optional[bool] = None
    publish_screenshot_count: Optional[int] = None
    image_host: Optional[str] = None
    image_imgbb_key: Optional[str] = None
    image_imgchest_token: Optional[str] = None
    image_freeimage_key: Optional[str] = None
    image_postimage_key: Optional[str] = None
    publish_auto: Optional[bool] = None
    publish_anonymous: Optional[bool] = None
    publish_category: Optional[str] = None
    publish_countries: Optional[str] = None
    publish_poll_interval: Optional[int] = None
    publish_upload_limit_kbps: Optional[int] = None
    publish_scrape_enabled: Optional[bool] = None
    publish_archive_enabled: Optional[bool] = None
    publish_archive_mode: Optional[str] = None
    publish_archive_by_month: Optional[bool] = None
    publish_archive_dir: Optional[str] = None
    publish_archive_dir_host: Optional[str] = None
    # V1.5 日志详略
    log_verbose: Optional[bool] = None
    # V1.4 刮削
    scrape_enabled: Optional[bool] = None
    scrape_watch_dir: Optional[str] = None
    scrape_output_dir: Optional[str] = None
    scrape_interval: Optional[int] = None
    scrape_settle_seconds: Optional[int] = None
    scrape_stable_checks: Optional[int] = None
    scrape_min_size_mb: Optional[int] = None
    scrape_translate_enabled: Optional[bool] = None
    scrape_translate_provider: Optional[str] = None
    scrape_move_on_fail: Optional[bool] = None
    # V1.5 统一归档（监控 & 发种共用）
    archive_mode: Optional[str] = None        # hardlink | copy | move
    archive_by_month: Optional[bool] = None
    scrape_meta_enabled: Optional[bool] = None   # 刮削总开关：改名番号+写NFO/封面
    archive_enabled: Optional[bool] = None       # 归档总开关：成品放归档目录


# ──────────────────────────────────────────────
# API 路由
# ──────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok", "version": APP_VERSION}


# ──────────────────────────────────────────────
# 版本检测：当前版本 vs GitHub 最新 release（带缓存，可走代理）
# ──────────────────────────────────────────────
# 内存缓存：避免频繁打 GitHub API（其匿名限流 60 次/小时）
_version_cache: dict = {"ts": 0.0, "data": None}
_VERSION_TTL = 3600  # 缓存 1 小时


def _parse_semver(tag: str) -> tuple:
    """把 'V1.4.3' / 'v1.4' / '1.4.2.1' 规整成可比较的整数元组。"""
    nums = re.findall(r"\d+", tag or "")
    return tuple(int(n) for n in nums) if nums else (0,)


def _cmp_version(a: str, b: str) -> int:
    """语义化比较：a>b 返回 1，a<b 返回 -1，相等返回 0。短的按 0 补齐。"""
    ta, tb = _parse_semver(a), _parse_semver(b)
    n = max(len(ta), len(tb))
    ta = ta + (0,) * (n - len(ta))
    tb = tb + (0,) * (n - len(tb))
    return (ta > tb) - (ta < tb)


@app.get("/api/version")
async def api_version(force: bool = Query(False, description="是否强制刷新缓存")):
    """
    返回当前版本与 GitHub 最新 release，判断是否有更新。
    后端代理 + 缓存 1 小时，避免浏览器跨域/被限流，群晖内网也能用（经配置代理出网）。
    """
    import time as _time
    now = _time.time()
    if not force and _version_cache["data"] and (now - _version_cache["ts"] < _VERSION_TTL):
        return _version_cache["data"]

    config = load_config()
    proxy = config.get("proxy") or None
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    result = {
        "current": APP_VERSION,
        "latest": "",
        "update_available": False,
        "release_url": f"https://github.com/{GITHUB_REPO}/releases",
        "error": "",
    }
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=12, follow_redirects=True) as client:
            resp = await client.get(api_url, headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "jav-search-version-check",
            })
        if resp.status_code == 200:
            data = resp.json()
            latest = (data.get("tag_name") or data.get("name") or "").strip()
            result["latest"] = latest
            if data.get("html_url"):
                result["release_url"] = data["html_url"]
            result["update_available"] = bool(latest) and _cmp_version(latest, APP_VERSION) > 0
        else:
            result["error"] = f"GitHub HTTP {resp.status_code}"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    # 仅在成功拿到 latest 时写缓存；失败不缓存，便于下次重试
    if result["latest"]:
        _version_cache.update({"ts": now, "data": result})
    return result


import asyncio as _asyncio
import hashlib as _hashlib

# ── 图片缓存：内存（热）+ 磁盘（持久）两级 ──────────────────────────
# 一级：内存 LRU（最快，进程内）。二级：磁盘持久缓存（容器重启不丢、多用户/多实例共享、
# 命中即零上游请求）。封面是首页最高优先级，两级缓存让冷启动也能秒出。
_img_cache: dict[str, tuple[bytes, str]] = {}
_IMG_CACHE_MAX = 800
_img_disk_tasks: set = set()   # 持有后台落盘任务引用，防止被 GC 提前回收

# 磁盘缓存目录：默认放进已持久化的 CONFIG_DIR 下，现有用户无需改 compose 即生效。
_IMG_DISK_DIR = Path(os.getenv("CONFIG_DIR", "/config")) / "imgcache"
# 磁盘缓存总量上限（MB）：超出按最旧访问时间(LRU)淘汰。可用环境变量覆盖，默认 500MB。
try:
    _IMG_DISK_CACHE_MB = max(0, int(os.getenv("IMG_DISK_CACHE_MB", "500")))
except ValueError:
    _IMG_DISK_CACHE_MB = 500
_IMG_DISK_ENABLED = _IMG_DISK_CACHE_MB > 0
_img_disk_write_counter = 0   # 写入计数：每累计若干次才做一次全量扫描淘汰，避免每写都扫盘

# 图片代理并发上限：封面是首页最高优先级，图片来自 CDN、并发被封风险低，
# 放宽到 18 让「首屏十几张大封面 + 相邻页封面预热」更快齐发（详情样品图共用此闸）。
_img_semaphore = _asyncio.Semaphore(18)

# content-type ↔ 扩展名映射（磁盘文件名用扩展名记录类型，读回时还原 content-type）
_CTYPE_EXT = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
              "image/webp": "webp", "image/gif": "gif", "image/avif": "avif"}
_EXT_CTYPE = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp",
              "gif": "image/gif", "avif": "image/avif"}


def _img_disk_path(url: str, ctype: str = "") -> Path:
    """URL → 磁盘缓存文件路径（sha256 命名，扩展名记录 content-type）。"""
    h = _hashlib.sha256(url.encode("utf-8")).hexdigest()
    ext = _CTYPE_EXT.get((ctype or "").split(";")[0].strip().lower(), "jpg")
    return _IMG_DISK_DIR / f"{h}.{ext}"


def _img_disk_find(url: str) -> Optional[Path]:
    """按 URL 找已落盘的缓存文件（任一已知扩展名命中即可）。"""
    if not _IMG_DISK_ENABLED:
        return None
    h = _hashlib.sha256(url.encode("utf-8")).hexdigest()
    for ext in _EXT_CTYPE:
        p = _IMG_DISK_DIR / f"{h}.{ext}"
        if p.exists():
            return p
    return None


def _img_disk_get_sync(url: str) -> Optional[tuple[bytes, str]]:
    """磁盘命中则读出 (bytes, content-type)，并更新访问时间(LRU)。阻塞操作，放线程执行。"""
    try:
        p = _img_disk_find(url)
        if not p:
            return None
        data = p.read_bytes()
        if not data:
            return None
        ctype = _EXT_CTYPE.get(p.suffix.lstrip(".").lower(), "image/jpeg")
        try:
            os.utime(p, None)   # 触碰 mtime 作为 LRU 近期访问标记
        except OSError:
            pass
        return data, ctype
    except Exception:
        return None


def _img_disk_put_sync(url: str, content: bytes, ctype: str) -> None:
    """原子写入磁盘缓存，并按需触发 LRU 淘汰。阻塞操作，放线程执行。"""
    global _img_disk_write_counter
    if not _IMG_DISK_ENABLED or not content:
        return
    try:
        _IMG_DISK_DIR.mkdir(parents=True, exist_ok=True)
        p = _img_disk_path(url, ctype)
        tmp = p.with_suffix(p.suffix + f".tmp{os.getpid()}")
        tmp.write_bytes(content)
        os.replace(tmp, p)   # 原子替换，避免并发下读到半截文件
        _img_disk_write_counter += 1
        # 每累计 50 次写入做一次全量扫描淘汰（摊薄扫盘开销）
        if _img_disk_write_counter >= 50:
            _img_disk_write_counter = 0
            _img_disk_evict_sync()
    except Exception:
        pass


def _img_mem_put(url: str, content: bytes, ctype: str) -> None:
    """写入内存 LRU 缓存（满则淘汰最旧）。"""
    if len(_img_cache) >= _IMG_CACHE_MAX:
        _img_cache.pop(next(iter(_img_cache)))
    _img_cache[url] = (content, ctype)


def _img_disk_put_bg(url: str, content: bytes, ctype: str) -> None:
    """后台落盘（fire-and-forget），不阻塞图片响应。"""
    if not _IMG_DISK_ENABLED or not content:
        return
    try:
        t = _asyncio.create_task(_asyncio.to_thread(_img_disk_put_sync, url, content, ctype))
        _img_disk_tasks.add(t)
        t.add_done_callback(_img_disk_tasks.discard)
    except RuntimeError:
        pass


def _img_disk_evict_sync() -> None:
    """磁盘缓存超过上限时，按访问时间(mtime)从旧到新删除直到回落到上限之下。"""
    if not _IMG_DISK_ENABLED:
        return
    try:
        cap = _IMG_DISK_CACHE_MB * 1024 * 1024
        files = []
        total = 0
        with os.scandir(_IMG_DISK_DIR) as it:
            for e in it:
                if not e.is_file():
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                files.append((st.st_mtime, st.st_size, e.path))
                total += st.st_size
        if total <= cap:
            return
        files.sort()   # 最旧访问的在前
        for _mtime, size, path in files:
            if total <= cap:
                break
            try:
                os.remove(path)
                total -= size
            except OSError:
                pass
    except Exception:
        pass


def _img_referer_candidates(url: str) -> list[str]:
    """
    返回按优先级排列的 Referer 候选。
    防盗链站点（JavBus/JavDB 及其镜像/CDN）需特定站点 Referer；
    若失败再退回「图片自身 origin」与「空 Referer」，覆盖镜像/CDN 差异。
    """
    from urllib.parse import urlparse
    p = urlparse(url)
    host = (p.netloc or "").lower()
    origin = f"{p.scheme}://{p.netloc}/"

    site = None
    # JavBus 及其常见镜像/图床
    if any(k in host for k in ("javbus", "buscdn", "seedmm", "dmmsee", "busfan")):
        site = "https://www.javbus.com/"
    elif "javdb" in host or "c1ne" in host:
        site = "https://javdb.com/"
    elif "dmm" in host or "fanza" in host:
        site = "https://www.dmm.co.jp/"
    # MissAV 系 CDN（封面 fourhoi、逐帧样品图 nineyu/surrit/sixyik 等）有防盗链，
    # 需带 missav 域名作 Referer 才能取（封面 fourhoi 多数无需，但带上无害）。
    elif any(k in host for k in ("fourhoi", "nineyu", "surrit", "sixyik", "missav")):
        site = "https://missav.ws/"

    cands = []
    for r in (site, origin, ""):
        if r not in cands:
            cands.append(r)
    return cands


@app.get("/api/img")
async def api_img_proxy(url: str = Query(..., description="原始图片 URL")):
    """
    图片代理 — 带正确 Referer 抓取封面绕过防盗链，再转发给浏览器。
    V1.3：多 Referer 候选 + 代理/直连兜底，兼容 JavBus 镜像、CDN、不同地区。
    """
    if not url or not url.startswith("http"):
        raise HTTPException(status_code=400, detail="无效的图片地址")

    # 一级：内存缓存命中（最快）
    if url in _img_cache:
        content, ctype = _img_cache[url]
        return Response(content=content, media_type=ctype,
                        headers={"Cache-Control": "public, max-age=86400"})

    # 二级：磁盘持久缓存命中（容器重启不丢、多用户共享；命中即零上游请求）
    disk = await _asyncio.to_thread(_img_disk_get_sync, url)
    if disk:
        content, ctype = disk
        _img_mem_put(url, content, ctype)   # 回填内存，下次走一级
        return Response(content=content, media_type=ctype,
                        headers={"Cache-Control": "public, max-age=86400"})

    config = load_config()
    proxy = config.get("proxy") or None

    base_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    referers = _img_referer_candidates(url)
    # 先用配置代理，再直连兜底（部分 CDN 直连可达、代理反而失败）
    proxy_opts = [proxy, None] if proxy else [None]

    last_status = None
    # 并发上限 + 单次较短超时：失败快速放行让浏览器自然重试，避免请求堆积拖死整页
    async with _img_semaphore:
        # 再次检查缓存（排队期间可能已被其它请求填充）
        if url in _img_cache:
            content, ctype = _img_cache[url]
            return Response(content=content, media_type=ctype,
                            headers={"Cache-Control": "public, max-age=86400"})
        for pxy in proxy_opts:
            try:
                async with httpx.AsyncClient(proxy=pxy, timeout=12, follow_redirects=True) as client:
                    for ref in referers:
                        headers = dict(base_headers)
                        if ref:
                            headers["Referer"] = ref
                        try:
                            resp = await client.get(url, headers=headers)
                        except Exception:
                            continue
                        last_status = resp.status_code
                        ctype = resp.headers.get("content-type", "")
                        # 必须是 200 且确实是图片（防盗链常返回 HTML 验证页）
                        if resp.status_code == 200 and resp.content and \
                           (ctype.startswith("image/") or "image" in ctype or not ctype):
                            content = resp.content
                            ctype = ctype or "image/jpeg"
                            _img_mem_put(url, content, ctype)     # 内存（热）
                            _img_disk_put_bg(url, content, ctype)  # 磁盘（持久，后台落盘）
                            return Response(content=content, media_type=ctype,
                                            headers={"Cache-Control": "public, max-age=86400"})
            except Exception:
                continue

    raise HTTPException(status_code=404, detail=f"图片获取失败 (HTTP {last_status})")


@app.post("/api/search")
async def api_search(req: SearchRequest):
    """
    列表级搜索（V1.3）：只抓列表页，快速返回卡片级条目（可达 300-500 条）。
    详情（演员/标签/导演/简介）通过 /api/details 按需补全。
    """
    if not req.query or not req.query.strip():
        raise HTTPException(status_code=400, detail="搜索词不能为空")

    config = load_config()
    proxy = config.get("proxy") or None
    sources = req.sources or config.get("sources", ["javbus", "javdb"])
    max_results = int(req.max_results or config.get("max_results", 300))
    max_results = max(1, min(max_results, MAX_RESULTS_HARD_CAP))

    # 自动检测模式
    mode = req.mode
    if mode == "auto" or not mode:
        mode = detect_search_mode(req.query.strip())

    try:
        results = await search(
            query=req.query.strip(),
            mode=mode,
            proxy=proxy,
            sources=sources,
            max_results=max_results,
        )
        return {
            "success": True,
            "mode": mode,
            "total": len(results),
            "results": results,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"搜索失败: {str(e)}")


@app.post("/api/details")
async def api_details(req: DetailsRequest):
    """
    按需补全详情。前台对「当前页 + 预取下一页」的条目调用本接口，
    服务端并发抓取各自来源的详情页并返回（与输入顺序一致，失败项为 null）。
    """
    if not req.items:
        return {"success": True, "results": []}
    # 单次最多补全 60 条，避免被滥用
    items = [{"url": it.url, "source": it.source} for it in req.items[:60]]
    config = load_config()
    proxy = config.get("proxy") or None
    try:
        results = await enrich(items, proxy=proxy)
        return {"success": True, "results": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"详情抓取失败: {str(e)}")


@app.get("/api/latest")
async def api_latest(source: str = Query(None, description="只抓单个来源（用于首页边抓边显示）")):
    """
    首页最新片源（未搜索时展示）。来源/数量取自配置。
    - 不带 source：抓取全部启用来源并合并（兼容旧行为）。
    - 带 source：只抓该来源，前端可对各来源并行请求、边到边显示。
    """
    config = load_config()
    if not config.get("show_latest", True):
        return {"success": True, "results": [], "disabled": True}
    proxy = config.get("proxy") or None
    all_sources = config.get("latest_sources") or config.get("sources", ["javbus", "javdb"])
    per_source = int(config.get("latest_per_source", 40))
    limits = config.get("latest_limits") or {}

    # 单来源模式：校验来源在启用列表内，只抓这一个（不合并）
    if source:
        if source not in all_sources:
            return {"success": False, "source": source, "results": [],
                    "detail": f"来源 {source} 未启用"}
        sources = [source]
    else:
        sources = all_sources

    try:
        results = await get_latest(proxy=proxy, sources=sources,
                                   per_source=per_source, limits=limits)
        return {"success": True, "source": source or "", "sources": all_sources,
                "total": len(results), "results": results}
    except Exception as e:
        # 首页最新失败不应阻塞使用，返回空列表 + 错误信息
        return {"success": False, "source": source or "", "results": [], "detail": str(e)}


@app.get("/api/javdb/test")
async def api_javdb_test():
    """
    JavDB 连通诊断（V1.4.2）。
    返回是否可达 / 是否命中 Cloudflare 盾 / 出口 IP 所在国（判断是否需要日本 IP）/ 解析条数。
    """
    config = load_config()
    proxy = config.get("proxy") or None
    try:
        result = await javdb_scraper.diagnose(proxy=proxy)
        return {"success": True, **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"JavDB 诊断失败: {str(e)}")


@app.get("/api/fc2/test")
async def api_fc2_test():
    """
    FC2（fc2ppvdb.com）连通诊断（V1.4.3）。
    fc2ppvdb 强制 Cloudflare Turnstile，必须走 FlareSolverr；
    返回是否可达 / 是否命中盾 / 取页方式 / 解析条数。
    """
    config = load_config()
    proxy = config.get("proxy") or None
    try:
        result = await fc2_scraper.diagnose(proxy=proxy)
        return {"success": True, **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"FC2 诊断失败: {str(e)}")


@app.post("/api/jackett/search")
async def api_jackett_search(req: JackettSearchRequest):
    """
    资源搜索（磁力/种子）。
      - 默认数据源：sukebei.nyaa.si（直连、磁力齐全），无需任何配置即可用。
      - jackett_enabled 开启且已配置 Jackett 时：Jackett 优先；Jackett 无结果/失败再用 sukebei 兜底。
    """
    if not req.query or not req.query.strip():
        raise HTTPException(status_code=400, detail="搜索词不能为空")

    config = load_config()
    query = req.query.strip()
    proxy = config.get("proxy") or None
    jackett_enabled = bool(config.get("jackett_enabled", False))
    jackett_url = config.get("jackett_url", "").strip()
    api_key = config.get("jackett_api_key", "").strip()

    results, source = [], "sukebei"
    try:
        if jackett_enabled and jackett_url and api_key:
            indexers = req.indexers or config.get("jackett_indexers", "all") or "all"
            timeout = int(config.get("jackett_timeout", 20))
            try:
                results = await search_jackett(
                    query=query, jackett_url=jackett_url, api_key=api_key,
                    indexers=indexers, proxy=None, timeout=timeout,
                )
                source = "jackett"
            except Exception as e:
                print(f"[资源搜索] Jackett 失败，回退 sukebei: {e!r}")
                results = []
            if not results:
                results = await search_sukebei(query, proxy=proxy)
                source = "sukebei(Jackett兜底)"
        else:
            results = await search_sukebei(query, proxy=proxy)
            source = "sukebei"
        return {"success": True, "total": len(results), "results": results, "source": source}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"资源搜索失败: {str(e)}")


@app.post("/api/imagehost/test")
async def api_imagehost_test():
    """图床上传测试：用项目内置测试图，【只测当前选中的优先图床本身】，返回结果与直链。
    不走兜底链——否则选中的图床失败时会回退到别的图床、看不出当前选择是否可用
    （表现为"总是测到 imgbb"）。前端会先保存设置再调用，确保用的是最新的优先图床/Key。"""
    config = load_config()
    proxy = config.get("proxy") or None
    selected = (config.get("image_host") or "").strip().lower()
    if selected not in imagehost.ALL_HOSTS:
        selected = imagehost.order_hosts(selected)[0]   # 选择无效时兜底到默认顺序首个
    asset = Path(__file__).parent / "assets" / "imagehost_test.png"
    if not asset.exists():
        raise HTTPException(status_code=500, detail="测试图片缺失（assets/imagehost_test.png）")
    r = await imagehost.upload_image(str(asset), proxy=proxy, hosts=[selected])
    return {
        "success": bool(r.get("ok")),
        "host": r.get("host", "") or selected,
        "url": r.get("img_url", ""),
        "error": r.get("error", ""),
        "hosts": [selected],
    }


@app.get("/api/jackett/status")
async def api_jackett_status():
    """检测 Jackett 是否可用（轻量探活，不触发真实搜索，详见 jackett.check_status）。"""
    config = load_config()
    timeout = int(config.get("jackett_timeout", 15) or 15)
    return await jackett.check_status(
        config.get("jackett_url", "").strip(),
        config.get("jackett_api_key", "").strip(),
        timeout=timeout,
    )


@app.get("/api/jackett/download")
async def api_jackett_download(
    url: str = Query(..., description="原始 .torrent 直链"),
    name: str = Query("download", description="保存文件名（通常用番号）"),
):
    """
    种子直链代理下载。
    Jackett 返回的 .torrent 直链常带其自身地址（如 http://localhost:9117/...），
    外网浏览器点击打不开。改由后端（与 Jackett 同网/同机）取回，再以附件形式
    回传给浏览器，从而在任意网络下都能下载到种子文件。
    """
    if not url or not url.startswith("http"):
        raise HTTPException(status_code=400, detail="无效的种子地址")
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"种子下载失败: {e}")
    if resp.status_code != 200 or not resp.content:
        raise HTTPException(status_code=502, detail=f"种子下载失败 HTTP {resp.status_code}")

    safe = re.sub(r'[^\w.\-]', '_', name).strip("_") or "download"
    if not safe.lower().endswith(".torrent"):
        safe += ".torrent"
    return Response(
        content=resp.content,
        media_type="application/x-bittorrent",
        headers={"Content-Disposition": f'attachment; filename="{safe}"'},
    )


@app.post("/api/translate")
async def api_translate(req: TranslateRequest):
    config = load_config()
    try:
        result = await translate(
            text=req.text,
            provider=req.provider,
            config=config,
            from_lang=req.from_lang,
            to_lang=req.to_lang,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/config")
async def api_get_config():
    config = load_config()
    # 脱敏返回（隐藏密钥）
    safe_config = dict(config)
    for key in ["baidu_secret_key", "aliyun_access_key_secret", "jackett_api_key", "qb_password", "tr_password", "mteam_api_key", "javdb_cookie", "fc2_cookie", "image_imgbb_key", "image_imgchest_token", "image_freeimage_key", "image_postimage_key"]:
        if safe_config.get(key):
            v = safe_config[key]
            safe_config[key] = "***" + v[-4:] if len(v) > 4 else "****"
    # 禁止缓存，确保保存后立即读到最新设置
    return JSONResponse(content=safe_config, headers={"Cache-Control": "no-store"})


@app.post("/api/config")
async def api_set_config(req: ConfigUpdateRequest):
    config = load_config()

    update = req.dict(exclude_none=True)
    # 如果是脱敏值则不更新
    for key in ["baidu_secret_key", "aliyun_access_key_secret", "jackett_api_key", "qb_password", "tr_password", "mteam_api_key", "javdb_cookie", "fc2_cookie", "image_imgbb_key", "image_imgchest_token", "image_freeimage_key", "image_postimage_key"]:
        v = update.get(key, "")
        if v and v.startswith("***"):
            update.pop(key, None)

    config.update(update)
    ok = save_config(config)
    # 配置变更后同步日志详略
    try:
        logbus.set_verbose(config.get("log_verbose", True))
    except Exception:
        pass
    # 若涉及 M-Team 密钥/地址，后台自动抓取并持久化分类/国家等枚举，供发种自动选择
    if ("mteam_api_key" in update or "mteam_api_base" in update) and config.get("mteam_api_key"):
        import asyncio as _asyncio
        try:
            _asyncio.create_task(mteam_enums.refresh_conf(config))
        except Exception as e:
            print(f"[config] 触发 M-Team 枚举刷新失败: {e}")
    # 配置变更后启停后台监控（刮削/归档任一开启即运行）
    try:
        library.ensure_monitor()
    except Exception as e:
        print(f"[config] 刷新刮削监控失败: {e}")
    return {"success": ok}


# ──────────────────────────────────────────────
# 下载器（V1.4 qBittorrent / V1.5 + Transmission，经 downloader 抽象层调度）
# ──────────────────────────────────────────────

class QbAddRequest(BaseModel):
    download_url: str                  # 磁力链或 .torrent 链接
    save_path: Optional[str] = None    # 覆盖默认保存目录
    category: Optional[str] = None     # 覆盖默认分类
    code: Optional[str] = None         # 该影片番号（来自搜索结果，用于刮削时精确识别）
    title: Optional[str] = None        # 该影片标题（辅助匹配）
    meta: Optional[dict] = None        # 列表/详情里已呈现的整条元数据（供下载后刮削直接复用）


@app.post("/api/qbittorrent/add")
async def api_qb_add(req: QbAddRequest):
    """把磁力链/种子推送到当前下载器（qB 或 Transmission，由 downloader_type 决定）。"""
    config = load_config()
    if not downloader.is_configured(config):
        raise HTTPException(status_code=400,
                            detail=f"未配置下载器（{downloader.active_type(config)}）地址，请在设置中填写")
    print(f"[推送→{downloader.active_type(config)}] 保存目录="
          f"{(req.save_path if req.save_path is not None else downloader.default_save_path(config)) or '默认'}  "
          f"分类={(req.category if req.category is not None else downloader.default_category(config)) or '无'}  "
          f"链接={req.download_url[:60]}", flush=True)
    magnet_limit = int(config.get("magnet_upload_limit_kbps", 0) or 0)
    if magnet_limit > 0:
        print(f"[推送] 单种上传限速={magnet_limit} KB/s", flush=True)
    result = await downloader.add_torrent(
        config,
        download_url=req.download_url,
        save_path=req.save_path,
        category=req.category,
        upload_limit_kbps=magnet_limit,
    )
    if not result.get("success"):
        print(f"[推送] 失败：{result.get('error', '')}", flush=True)
        raise HTTPException(status_code=502, detail=result.get("error", "推送失败"))
    print("[推送] 成功", flush=True)

    # 记下「列表里已呈现的元数据」，供下载完成后刮削直接使用（不再从文件名重识别番号+重刮削，
    #   修纯数字番号识别出错）；磁力链且开了「下完即删」则标记，由后台轮询下完后删种(保留文件)。
    is_magnet = (req.download_url or "").lower().startswith("magnet:")
    meta = dict(req.meta) if isinstance(req.meta, dict) else {}
    if req.code and not meta.get("code"):
        meta["code"] = req.code
    if req.title and not meta.get("title"):
        meta["title"] = req.title
    ih = (result.get("hash") or "").lower()
    if ih:
        autodel = is_magnet and bool(config.get("magnet_delete_completed", False))
        try:
            intake.register(ih, meta, autodelete=autodel)
            if autodel:
                print(f"[推送] 已标记下完即删（保留文件）hash={ih[:12]}", flush=True)
        except Exception as e:
            print(f"[推送] 记录入库元数据失败：{e}", flush=True)
    return result


@app.get("/api/qbittorrent/status")
async def api_qb_status():
    """检测当前下载器连通性（保留旧路径，前端兼容）。"""
    config = load_config()
    return await downloader.get_status(config)


@app.get("/api/downloader/status")
async def api_downloader_status():
    """检测当前下载器连通性（通用路径，返回含 type 字段）。"""
    config = load_config()
    return await downloader.get_status(config)


# ──────────────────────────────────────────────
# M-Team PT 站（V1.5）—— 辅种/读取侧
# ──────────────────────────────────────────────

class MteamSearchRequest(BaseModel):
    keyword: str
    mode: Optional[str] = None
    page_number: Optional[int] = 1
    page_size: Optional[int] = 50


@app.get("/api/mteam/test")
async def api_mteam_test():
    """M-Team 连通诊断：base 可达 + API 密钥有效。"""
    config = load_config()
    try:
        result = await mteam.diagnose(config)
        return {"success": True, **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"M-Team 诊断失败: {str(e)}")


@app.post("/api/mteam/search")
async def api_mteam_search(req: MteamSearchRequest):
    """在 M-Team 搜种（通常用番号）。返回规整后的候选列表。"""
    if not req.keyword or not req.keyword.strip():
        raise HTTPException(status_code=400, detail="搜索词不能为空")
    config = load_config()
    res = await mteam.search(
        config, keyword=req.keyword.strip(), mode=req.mode or mteam.DEFAULT_MODE,
        page_number=int(req.page_number or 1), page_size=int(req.page_size or 50),
    )
    if not res["ok"]:
        raise HTTPException(status_code=502, detail=res["error"])
    return {"success": True, "total": res["total"], "results": res["items"]}


# ──────────────────────────────────────────────
# 辅种 cross-seed（V1.5）
# ──────────────────────────────────────────────

class CrossseedScanRequest(BaseModel):
    dir: str                            # 容器视角的扫描目录
    min_size_mb: Optional[int] = 100


class CrossseedSearchRequest(BaseModel):
    keyword: str                        # 通常填番号
    size: Optional[int] = 0             # 本地文件大小（字节），用于精确吻合标注
    mode: Optional[str] = None


class CrossseedApplyRequest(BaseModel):
    torrent_id: str                     # M-Team 种子 ID
    save_path: str                      # 下载器主机视角的「已有数据所在目录」
    paused: Optional[bool] = False


@app.post("/api/crossseed/scan")
async def api_crossseed_scan(req: CrossseedScanRequest):
    """扫描容器视角目录下的视频文件（取大小，辅助选片辅种）。"""
    res = crossseed.scan_dir(req.dir.strip(), int(req.min_size_mb or 100))
    if not res["ok"]:
        raise HTTPException(status_code=400, detail=res["error"])
    return {"success": True, "total": len(res["files"]), "files": res["files"]}


@app.post("/api/crossseed/search")
async def api_crossseed_search(req: CrossseedSearchRequest):
    """按番号在 M-Team 搜候选，并按本地文件大小标注精确吻合项。"""
    if not req.keyword or not req.keyword.strip():
        raise HTTPException(status_code=400, detail="搜索词不能为空")
    config = load_config()
    res = await crossseed.find_candidates(
        config, keyword=req.keyword.strip(), target_size=int(req.size or 0),
        mode=req.mode,
    )
    if not res["ok"]:
        raise HTTPException(status_code=502, detail=res["error"])
    return {"success": True, "total": res["total"], "candidates": res["candidates"]}


@app.post("/api/crossseed/apply")
async def api_crossseed_apply(req: CrossseedApplyRequest):
    """取选中的 M-Team 种子，加进当前下载器做辅种（保存目录指向本地已有数据）。"""
    config = load_config()
    print(f"[辅种] tid={req.torrent_id} → 保存目录={req.save_path}", flush=True)
    result = await crossseed.apply(
        config, torrent_id=req.torrent_id.strip(),
        save_path=req.save_path.strip(), paused=bool(req.paused),
    )
    if not result.get("success"):
        print(f"[辅种] 失败：{result.get('error', '')}", flush=True)
        raise HTTPException(status_code=502, detail=result.get("error", "辅种失败"))
    print(f"[辅种] 成功：{result.get('message', '')}", flush=True)
    return result


# ──────────────────────────────────────────────
# 前端静态文件服务
# ──────────────────────────────────────────────

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

if FRONTEND_DIR.exists():
    @app.get("/", response_class=HTMLResponse)
    async def serve_index():
        index = FRONTEND_DIR / "index.html"
        if index.exists():
            return HTMLResponse(content=index.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>Frontend not found</h1>", status_code=404)

    @app.get("/login", response_class=HTMLResponse)
    async def serve_login():
        login = FRONTEND_DIR / "login.html"
        if login.exists():
            return HTMLResponse(content=login.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>Login page not found</h1>", status_code=404)

    @app.get("/publish", response_class=HTMLResponse)
    async def serve_publish():
        page = FRONTEND_DIR / "publish.html"
        if page.exists():
            return HTMLResponse(content=page.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>Publish page not found</h1>", status_code=404)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8085")),
        reload=False,
        log_level="info",
    )
