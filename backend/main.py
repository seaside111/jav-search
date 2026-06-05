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
from translator import translate
from config_manager import load as load_config, save as save_config, MAX_RESULTS_HARD_CAP
from jackett import search_jackett
import qbittorrent
import library
import push_hints
import auth

app = FastAPI(title="JAV Search", version="1.4.2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 媒体库刮削路由（手动扫描/刮削 + 监控状态）
app.include_router(library.router)


@app.on_event("startup")
async def _on_startup():
    # 按配置拉起后台刮削监控
    print("[启动] JAV Search 1.4.2 启动完成，初始化刮削监控…", flush=True)
    try:
        library.start_monitor()
    except Exception as e:
        print(f"[启动] 刮削监控启动失败: {e}", flush=True)

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
    proxy: Optional[str] = ""
    sources: Optional[list[str]] = None
    # V1.4.2 JavDB 反爬增强
    javdb_flaresolverr_url: Optional[str] = None
    javdb_flaresolverr_use_proxy: Optional[bool] = None
    javdb_cookie: Optional[str] = None
    javdb_prefetch_extras: Optional[bool] = None
    baidu_app_id: Optional[str] = ""
    baidu_secret_key: Optional[str] = ""
    aliyun_access_key_id: Optional[str] = ""
    aliyun_access_key_secret: Optional[str] = ""
    default_translate_provider: Optional[str] = "baidu"
    results_per_page: Optional[int] = 12
    max_results: Optional[int] = None
    show_latest: Optional[bool] = None
    latest_sources: Optional[list[str]] = None
    latest_per_source: Optional[int] = None
    latest_limits: Optional[dict] = None
    jackett_url: Optional[str] = ""
    jackett_api_key: Optional[str] = ""
    jackett_indexers: Optional[str] = "all"
    jackett_timeout: Optional[int] = 20
    # V1.4 qBittorrent
    qb_url: Optional[str] = None
    qb_username: Optional[str] = None
    qb_password: Optional[str] = None
    qb_save_path: Optional[str] = None
    qb_category: Optional[str] = None
    qb_paused: Optional[bool] = None
    # V1.4 刮削
    scrape_enabled: Optional[bool] = None
    scrape_watch_dir: Optional[str] = None
    scrape_output_dir: Optional[str] = None
    scrape_interval: Optional[int] = None
    scrape_settle_seconds: Optional[int] = None
    scrape_stable_checks: Optional[int] = None
    scrape_min_size_mb: Optional[int] = None
    scrape_translate_provider: Optional[str] = None
    scrape_move_on_fail: Optional[bool] = None


# ──────────────────────────────────────────────
# API 路由
# ──────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "1.4.2"}


# 图片缓存（内存，简单 LRU 效果）
_img_cache: dict[str, tuple[bytes, str]] = {}
_IMG_CACHE_MAX = 800
# 图片代理并发上限：防止一页十几张封面 + 详情样品图同时打满代理导致整体超时。
import asyncio as _asyncio
_img_semaphore = _asyncio.Semaphore(12)


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

    # 命中缓存
    if url in _img_cache:
        content, ctype = _img_cache[url]
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
                            if len(_img_cache) >= _IMG_CACHE_MAX:
                                _img_cache.pop(next(iter(_img_cache)))
                            _img_cache[url] = (content, ctype)
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
async def api_latest():
    """首页最新片源（未搜索时展示）。来源/数量取自配置。"""
    config = load_config()
    if not config.get("show_latest", True):
        return {"success": True, "results": [], "disabled": True}
    proxy = config.get("proxy") or None
    sources = config.get("latest_sources") or config.get("sources", ["javbus", "javdb"])
    per_source = int(config.get("latest_per_source", 40))
    limits = config.get("latest_limits") or {}
    try:
        results = await get_latest(proxy=proxy, sources=sources,
                                   per_source=per_source, limits=limits)
        return {"success": True, "total": len(results), "results": results}
    except Exception as e:
        # 首页最新失败不应阻塞使用，返回空列表 + 错误信息
        return {"success": False, "results": [], "detail": str(e)}


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


@app.post("/api/jackett/search")
async def api_jackett_search(req: JackettSearchRequest):
    """调用 Jackett 搜索磁力/种子资源"""
    if not req.query or not req.query.strip():
        raise HTTPException(status_code=400, detail="搜索词不能为空")

    config = load_config()
    jackett_url = config.get("jackett_url", "").strip()
    api_key = config.get("jackett_api_key", "").strip()

    if not jackett_url:
        raise HTTPException(status_code=400, detail="未配置 Jackett 地址，请在设置中填写")
    if not api_key:
        raise HTTPException(status_code=400, detail="未配置 Jackett API Key，请在设置中填写")

    indexers = req.indexers or config.get("jackett_indexers", "all") or "all"
    timeout = int(config.get("jackett_timeout", 20))
    # Jackett 通常不需要翻墙代理，留空即可；如有需要可复用 proxy
    proxy = None  # config.get("proxy") or None

    try:
        results = await search_jackett(
            query=req.query.strip(),
            jackett_url=jackett_url,
            api_key=api_key,
            indexers=indexers,
            proxy=proxy,
            timeout=timeout,
        )
        return {
            "success": True,
            "total": len(results),
            "results": results,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Jackett 搜索失败: {str(e)}")


@app.get("/api/jackett/status")
async def api_jackett_status():
    """检测 Jackett 是否可用"""
    config = load_config()
    jackett_url = config.get("jackett_url", "").strip()
    api_key = config.get("jackett_api_key", "").strip()

    if not jackett_url or not api_key:
        return {"configured": False, "online": False, "message": "未配置"}

    import httpx
    try:
        url = jackett_url.rstrip("/") + f"/api/v2.0/indexers/all/results?apikey={api_key}&Query=test"
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(url)
        if resp.status_code == 200:
            return {"configured": True, "online": True, "message": "连接正常"}
        else:
            return {"configured": True, "online": False, "message": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"configured": True, "online": False, "message": str(e)}


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
    for key in ["baidu_secret_key", "aliyun_access_key_secret", "jackett_api_key", "qb_password", "javdb_cookie"]:
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
    for key in ["baidu_secret_key", "aliyun_access_key_secret", "jackett_api_key", "qb_password", "javdb_cookie"]:
        v = update.get(key, "")
        if v and v.startswith("***"):
            update.pop(key, None)

    config.update(update)
    ok = save_config(config)
    # 配置变更后按最新的 scrape_enabled 启停后台监控
    try:
        library.ensure_monitor()
    except Exception as e:
        print(f"[config] 刷新刮削监控失败: {e}")
    return {"success": ok}


# ──────────────────────────────────────────────
# qBittorrent 下载器（V1.4）
# ──────────────────────────────────────────────

class QbAddRequest(BaseModel):
    download_url: str                  # 磁力链或 .torrent 链接
    save_path: Optional[str] = None    # 覆盖默认保存目录
    category: Optional[str] = None     # 覆盖默认分类
    code: Optional[str] = None         # 该影片番号（来自搜索结果，用于刮削时精确识别）
    title: Optional[str] = None        # 该影片标题（辅助匹配）


@app.post("/api/qbittorrent/add")
async def api_qb_add(req: QbAddRequest):
    """把磁力链/种子推送到 qBittorrent"""
    config = load_config()
    qb_url = config.get("qb_url", "").strip()
    if not qb_url:
        raise HTTPException(status_code=400, detail="未配置 qBittorrent 地址，请在设置中填写")
    save_path = req.save_path if req.save_path is not None else config.get("qb_save_path", "")
    category = req.category if req.category is not None else config.get("qb_category", "")
    print(f"[qB推送] → {qb_url}  保存目录={save_path or 'qB默认'}  分类={category or '无'}  "
          f"链接={req.download_url[:60]}", flush=True)
    result = await qbittorrent.add_torrent(
        qb_url=qb_url,
        username=config.get("qb_username", ""),
        password=config.get("qb_password", ""),
        download_url=req.download_url,
        save_path=(save_path or "").strip(),
        category=(category or "").strip(),
        paused=bool(config.get("qb_paused", False)),
    )
    if not result.get("success"):
        print(f"[qB推送] 失败：{result.get('error', '')}", flush=True)
        raise HTTPException(status_code=502, detail=result.get("error", "推送失败"))
    print("[qB推送] 成功", flush=True)
    # 记录「番号 ↔ 资源」标记：刮削下载完成的文件时据此精确识别番号，
    # 避免靠文件名「字母+数字」误判（如 hhd800.com@390JAC-234 被猜成 HHD-800）。
    if req.code:
        try:
            if push_hints.record(req.code, req.download_url, req.title or ""):
                print(f"[qB推送] 已标记番号 {req.code} 供刮削识别", flush=True)
        except Exception as e:
            print(f"[qB推送] 番号标记失败（不影响推送）：{e}", flush=True)
    return result


@app.get("/api/qbittorrent/status")
async def api_qb_status():
    """检测 qBittorrent 连通性与登录"""
    config = load_config()
    qb_url = config.get("qb_url", "").strip()
    if not qb_url:
        return {"configured": False, "online": False, "message": "未配置"}
    res = await qbittorrent.get_version(
        qb_url=qb_url,
        username=config.get("qb_username", ""),
        password=config.get("qb_password", ""),
    )
    res["configured"] = True
    return res


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8085")),
        reload=False,
        log_level="info",
    )
