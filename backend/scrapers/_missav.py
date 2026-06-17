"""
MissAV 元数据补全（V1.4.3）—— 主要用于给 FC2 补封面/标题/女优/标签。

背景：fc2ppvdb 对**已下架**的 FC2-PPV 常只剩番号骨架（无封面/标题）。而 MissAV 对 FC2
覆盖很好，连下架条目也保留了**完整标题 + 高清封面**（封面走 fourhoi.com CDN）。

设计要点：
  - 封面 URL 是**确定性**的：https://fourhoi.com/fc2-ppv-{num}/cover-n.jpg，
    列表卡可零额外请求地直接猜出（404 由前端图片代理优雅降级为无封面）。
  - 详情阶段再抓 MissAV 页面拿**真实标题**（og:title）与女优/标签（无法靠猜）。
  - MissAV 对不存在的番号返回通用站名标题（"MissAV | …"），用「og:title 是否含番号」校验。
  - 部分镜像有 Cloudflare（.ai 直连 403），默认用可直连的镜像（.ws）；失败再退到 FC2 的
    FlareSolverr（与 fc2 模块共用配置）。
"""
import re
import os
import json
import time
import asyncio
from pathlib import Path
from typing import Optional
import httpx

from ._fsgate import (flaresolverr_request as _fs_request, discover_auto as _fs_discover,
                      PRIO_DETAIL, PRIO_LATEST)

# 可直连的镜像优先；域名常变，可由配置 fc2_missav_base 覆盖（逗号分隔）。
# 这里是「兜底默认」，运行期会被后台自动发现（discover_bases）出来的最新域名顶替。
# .ai 为当前站点主推主域名，.ws 次之，再加一个历史镜像兜底。
DEFAULT_BASES = ["https://missav.ai", "https://missav.ws", "https://missav123.com"]
FOURHOI_CDN = "https://fourhoi.com"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,zh-CN;q=0.9,en;q=0.7",
}


# MissAV 总开关缓存：cover_url 会在卡片循环里被逐条调用，不能每次读盘 → 缓存 3s。
_enabled_cache = {"ts": 0.0, "val": True}


def is_enabled() -> bool:
    """MissAV 总开关（配置 fc2_missav_enabled）。关闭后停止一切 MissAV 关联行为：
    补全/样品图、后台域名发现、以及 fourhoi 确定性封面（fourhoi 是 MissAV 的图床）。
    带 ~3s 缓存，避免 cover_url 在卡片循环中每条都读 settings.json。"""
    now = time.time()
    if now - _enabled_cache["ts"] > 3:
        try:
            from config_manager import load as load_config
            _enabled_cache["val"] = bool(load_config().get("fc2_missav_enabled", True))
        except Exception:
            _enabled_cache["val"] = True
        _enabled_cache["ts"] = now
    return _enabled_cache["val"]


def cover_url(num: str) -> str:
    """FC2 封面的确定性 URL（无需抓页面即可得到）。总开关关闭时返回空（不再用 fourhoi）。"""
    if not num or not is_enabled():
        return ""
    return f"{FOURHOI_CDN}/fc2-ppv-{num}/cover-n.jpg"


def _runtime():
    """读取 MissAV 运行配置：是否启用、镜像列表，以及复用的 FlareSolverr。"""
    enabled, bases = True, list(DEFAULT_BASES)
    fs, use_proxy, cookie = "", True, ""
    try:
        from config_manager import load as load_config
        cfg = load_config()
        enabled = cfg.get("fc2_missav_enabled", True)
        custom = (cfg.get("fc2_missav_base") or "").strip()
        if custom:
            bases = [b.strip().rstrip("/") for b in custom.split(",") if b.strip()]
        else:
            # 无人工覆盖：用后台自动发现/缓存出来的最新域名（不可用时回落默认）
            bases = current_bases()
        fs = (cfg.get("fc2_flaresolverr_url") or cfg.get("javdb_flaresolverr_url") or "").strip()
        use_proxy = cfg.get("fc2_flaresolverr_use_proxy",
                            cfg.get("javdb_flaresolverr_use_proxy", True))
    except Exception:
        pass
    return {"enabled": enabled, "bases": bases,
            "flaresolverr_url": fs, "flaresolverr_use_proxy": use_proxy}


def _og(html: str, prop: str) -> str:
    m = re.search(rf'<meta[^>]+property=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']*)["\']',
                  html, re.I)
    if not m:
        m = re.search(rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']{re.escape(prop)}["\']',
                      html, re.I)
    return (m.group(1).strip() if m else "")


def _looks_like_cf(html: str, status: int) -> bool:
    if status in (403, 429, 503):
        return True
    head = (html or "")[:3000].lower()
    return ("just a moment" in head or "/cdn-cgi/challenge-platform/" in head
            or "cf-chl-bypass" in head)


async def _fetch_via_flaresolverr(url: str, fs_url: str, proxy: Optional[str],
                                  priority: int = PRIO_LATEST) -> str:
    """统一走 _fsgate 的智能适配 + 全局串行；MissAV 仅需 HTML，丢弃 status/err。"""
    html, _status, _err = await _fs_request(url, fs_url, proxy, None,
                                            max_timeout=40000, read_timeout=70.0,
                                            priority=priority)
    return html or ""


async def _get_html(num: str, proxy: Optional[str], opts: dict,
                    allow_flaresolverr: bool = True,
                    priority: int = PRIO_LATEST) -> str:
    """按镜像依次直连抓 MissAV 的 FC2 页面；命中 CF 时退到 FlareSolverr。
    allow_flaresolverr=False（列表批量补全用）：命中 CF 直接放弃，绝不回退 FlareSolverr，
    避免一次列表补全产生几十个 FlareSolverr 请求把它压垮。"""
    paths = [f"/ja/FC2-PPV-{num}", f"/FC2-PPV-{num}"]
    for base in opts["bases"]:
        for path in paths:
            url = base.rstrip("/") + path
            try:
                async with httpx.AsyncClient(headers=_HEADERS, proxy=proxy or None,
                                             timeout=12, follow_redirects=True) as client:
                    resp = await client.get(url)
                if resp.status_code == 200 and not _looks_like_cf(resp.text, 200):
                    if str(num) in resp.text:
                        return resp.text
                elif allow_flaresolverr and _looks_like_cf(resp.text, resp.status_code):
                    fs_url = opts.get("flaresolverr_url") or await _fs_discover()  # 留空则自动探测
                    if not fs_url:
                        continue
                    fs_proxy = proxy if opts.get("flaresolverr_use_proxy", True) else None
                    html = await _fetch_via_flaresolverr(url, fs_url, fs_proxy, priority)
                    if html and str(num) in html:
                        return html
            except Exception:
                continue
    return ""


def _clean_title(title: str, num: str) -> str:
    """去掉站名后缀与开头的番号前缀，留下纯标题。"""
    t = title
    for suf in (" - MissAV", "| MissAV", " - missav", "｜MissAV"):
        idx = t.find(suf)
        if idx > 0:
            t = t[:idx]
    # 去掉开头的 FC2-PPV-num / FC2-num 前缀
    t = re.sub(rf"^\s*FC2[-\s]?(PPV[-\s]?)?{re.escape(str(num))}\s*", "", t, flags=re.I)
    return t.strip()


async def fetch_fc2(num: str, proxy: Optional[str] = None,
                    allow_flaresolverr: bool = True,
                    priority: int = PRIO_LATEST) -> Optional[dict]:
    """
    抓 MissAV 补全 FC2 元数据。返回 {title, cover, actors, tags} 或 None（不存在/未启用）。
    封面优先用页面 og:image，没有则回退确定性 fourhoi URL。
    allow_flaresolverr=False：列表批量补全时禁用 FlareSolverr 回退（保护其不被压垮）。
    priority：FlareSolverr 回退时的闸优先级；详情路径传 PRIO_DETAIL 以便插队。
    """
    if not num:
        return None
    opts = _runtime()
    if not opts.get("enabled"):
        return None
    html = await _get_html(num, proxy, opts, allow_flaresolverr=allow_flaresolverr,
                           priority=priority)
    if not html:
        return None

    title_raw = _og(html, "og:title")
    # 校验：真实条目标题含番号；通用站名（MissAV | …）视为「无此条目」
    if not title_raw or str(num) not in title_raw:
        return None

    cover = _og(html, "og:image") or cover_url(num)
    # 注：不从 MissAV 抽取女优/体裁——其 /actresses//genres/ 链接里混有「女優ランキング」
    # 等导航/排行项，噪声大且难以可靠区分。MissAV 对 FC2 的高价值、可靠字段是「标题 + 封面」。

    # 样品图：MissAV 无传统图廊，但播放器进度条有一组逐帧截图（storyboard），
    # 形如 https://{cdn}/{uuid}/seek/_N.jpg（CDN 主机随视频变，如 nineyu.com）。
    # 这些就是可用的「样品图」。注意：这些 CDN 有防盗链，需带 Referer: missav 域名才能取
    #（由后端图片代理 /api/img 统一处理）。
    raw = html.replace("\\/", "/")
    samples = []
    for u in re.findall(r'https://[a-z0-9.-]+/[a-z0-9-]+/seek/_\d+\.jpg', raw):
        if u not in samples:
            samples.append(u)
    samples.sort(key=lambda u: int(re.search(r'_(\d+)\.jpg', u).group(1)))

    # 预览视频片段（确定性 URL，前端可选用作动态预览）
    preview_video = f"{FOURHOI_CDN}/fc2-ppv-{num}/preview.mp4"

    return {
        "title": _clean_title(title_raw, num),
        "cover": cover,
        "samples": samples[:40],
        "preview_video": preview_video,
        "source_url": f"{opts['bases'][0].rstrip('/')}/ja/FC2-PPV-{num}",
    }


# ───────────────────────── 最新域名自动发现 ─────────────────────────
# MissAV 因被诉/封锁，域名换得很勤（.com→.ws→.ai…）。一旦旧域名失效，FC2 的封面/
# 标题/样品图补全就断链。这里做一个「后台定时自发现」：
#   发现：从当前还能访问的镜像首页里，抽出站点**自荐**的新域名（最权威——是站点本尊
#         在页面里宣传的，不会是钓鱼镜像）；
#   验证：对每个候选抓首页探活，能直连(不吃CF)优先于需 FlareSolverr 过盾，死域名丢弃；
#   写回：排好序去重，落地到 /config 缓存并顶替运行期 bases。
# 设计同 qbittorrent.ensure_trackers_fresh：TTL 文件缓存 + 失败回落旧缓存/内置兜底，
# 绝不因一次发现失败而清空可用域名。
_BASES_TTL = 86400  # 缓存/重抓周期：1 天（域名换得勤，但也不必更频繁）
# 运行期内存态：source ∈ {fallback, file, discovered, user}
_bases_state = {"ts": 0.0, "list": list(DEFAULT_BASES), "source": "fallback"}

# 从 HTML 里抽 missav 系域名；排除 CDN/静态资源/第三方域名
_DOMAIN_RE = re.compile(r'https?://([a-z0-9-]*missav[a-z0-9-]*\.[a-z]{2,8})', re.I)
_BAD_HOST_SUBSTR = ("fourhoi", "nineyu", "cloudflare", "google", "gstatic",
                    "fonts", "jsdelivr", "gravatar", "doubleclick", "facebook",
                    "twitter", "telegram", "t.me")


def _bases_file() -> Path:
    return Path(os.getenv("CONFIG_DIR", "/config")) / "missav_bases_cache.json"


def _read_bases_file():
    """读上次成功发现的缓存 (ts, list)；无则 (0, [])。不判 TTL。"""
    try:
        p = _bases_file()
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            lst = [b.rstrip("/") for b in (d.get("list") or []) if b]
            if lst:
                return float(d.get("ts", 0)), lst
    except Exception:
        pass
    return 0.0, []


def current_bases() -> list:
    """_runtime 取当前生效镜像列表。首次惰性加载文件缓存；空则回落内置默认。"""
    if _bases_state["source"] == "fallback" and not _bases_state["ts"]:
        fts, flst = _read_bases_file()
        if flst:
            _bases_state.update(ts=fts, list=flst, source="file")
    return _bases_state["list"] or list(DEFAULT_BASES)


def _extract_candidate_bases(html: str) -> list:
    """从首页 HTML 抽站点自荐的 missav 域名（og:url / 公告链接 / JS 常量等）。"""
    out = []
    for host in _DOMAIN_RE.findall(html or ""):
        host = host.lower()
        if any(b in host for b in _BAD_HOST_SUBSTR):
            continue
        url = "https://" + host
        if url not in out:
            out.append(url)
    return out


async def _fetch_home(base: str, opts: dict, proxy: Optional[str]):
    """抓某镜像首页 HTML；命中 CF 退 FlareSolverr。返回 (html, direct)。
    direct=True 表示无需过盾即可直连（排序时优先）。"""
    url = base.rstrip("/") + "/ja/"
    try:
        async with httpx.AsyncClient(headers=_HEADERS, proxy=proxy or None,
                                     timeout=12, follow_redirects=True) as client:
            resp = await client.get(url)
        if resp.status_code == 200 and not _looks_like_cf(resp.text, 200):
            return resp.text, True
        if _looks_like_cf(resp.text, resp.status_code):
            fs_url = opts.get("flaresolverr_url") or await _fs_discover()
            if fs_url:
                fs_proxy = proxy if opts.get("flaresolverr_use_proxy", True) else None
                html = await _fetch_via_flaresolverr(url, fs_url, fs_proxy)
                if html:
                    return html, False
    except Exception:
        pass
    return "", False


def _is_missav(html: str) -> bool:
    """粗判页面确是 MissAV 本尊（带 fourhoi CDN 或站名），过滤停放/钓鱼页。"""
    h = (html or "").lower()
    return ("fourhoi" in h) or ("missav" in h and "<title" in h)


async def discover_bases(config: Optional[dict] = None, force: bool = False) -> dict:
    """
    后台自发现最新 MissAV 域名。返回 _bases_state。
    优先级：用户自填(fc2_missav_base) > 自动发现 > 文件缓存 > 内置默认。
    TTL 内零成本直接返回；失败永远回落旧缓存/兜底，绝不清空。
    """
    config = config or {}
    # 0) 总开关关闭 → 不探测任何 missav 域名（MissAV 关站期间停掉一切关联）
    if not config.get("fc2_missav_enabled", True):
        return _bases_state
    # 1) 用户自填覆盖 → 直接用，不发现
    custom = (config.get("fc2_missav_base") or "").strip()
    if custom:
        bases = [b.strip().rstrip("/") for b in custom.split(",") if b.strip()]
        _bases_state.update(ts=time.time(), list=bases, source="user")
        return _bases_state

    now = time.time()
    # 2) TTL 内且已是发现/文件来源 → 复用
    if (not force and _bases_state["source"] in ("discovered", "file")
            and now - _bases_state["ts"] < _BASES_TTL):
        return _bases_state

    # 3) 关闭自动发现 → 用文件缓存或内置兜底
    if not config.get("fc2_missav_auto_discover", True):
        fts, flst = _read_bases_file()
        _bases_state.update(ts=fts, list=(flst or list(DEFAULT_BASES)),
                            source="file" if flst else "fallback")
        return _bases_state

    fts, flst = _read_bases_file()
    # 4) 文件缓存仍在 TTL 内 → 用它免抓
    if not force and flst and now - fts < _BASES_TTL:
        _bases_state.update(ts=fts, list=flst, source="file")
        return _bases_state

    proxy = config.get("proxy") or None
    opts = _runtime()  # 复用 flaresolverr 配置
    # 种子：文件缓存 + 当前态 + 内置默认（去重保序）
    seed = []
    for b in (flst + _bases_state["list"] + list(DEFAULT_BASES)):
        b = (b or "").rstrip("/")
        if b and b not in seed:
            seed.append(b)

    # 发现：从第一个可达种子的首页抽站点自荐域名
    candidates = list(seed)
    for base in seed:
        html, _direct = await _fetch_home(base, opts, proxy)
        if html and _is_missav(html):
            for c in _extract_candidate_bases(html):
                if c not in candidates:
                    candidates.append(c)
            break  # 有一个可达站点自荐即够

    # 验证 + 排序：直连优先 > 需 FS 过盾 > 丢弃死域名
    direct, viafs = [], []
    for base in candidates:
        html, is_direct = await _fetch_home(base, opts, proxy)
        if not (html and _is_missav(html)):
            continue
        (direct if is_direct else viafs).append(base)
    ranked = direct + [b for b in viafs if b not in direct]

    if not ranked:
        # 全部验证失败：保留旧缓存/兜底，绝不清空
        if flst:
            _bases_state.update(ts=fts, list=flst, source="file")
        else:
            _bases_state.update(list=list(DEFAULT_BASES), source="fallback")
        return _bases_state

    ranked = ranked[:6]
    _bases_state.update(ts=now, list=ranked, source="discovered")
    try:
        p = _bases_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"ts": now, "list": ranked}, ensure_ascii=False),
                     encoding="utf-8")
    except Exception:
        pass
    return _bases_state


async def _discover_loop(load_config):
    """后台常驻：启动即发现一次，之后每 _BASES_TTL 重抓，保持域名最新。"""
    while True:
        try:
            cfg = load_config() if callable(load_config) else (load_config or {})
            if not cfg.get("fc2_missav_enabled", True):
                print("[missav] 总开关关闭，跳过域名发现", flush=True)
            else:
                st = await discover_bases(cfg, force=True)
                print(f"[missav] 域名发现完成：source={st['source']} "
                      f"bases={st['list']}", flush=True)
        except Exception as e:
            print(f"[missav] 域名发现失败: {e}", flush=True)
        await asyncio.sleep(_BASES_TTL)


def start_discover_loop(load_config):
    """供 main 启动时挂载后台发现任务。"""
    return asyncio.create_task(_discover_loop(load_config))


async def refresh_bases(config: dict) -> dict:
    """手动刷新（设置页按钮/接口用）。返回 {ok, count, source, bases}。"""
    st = await discover_bases(config, force=True)
    src_label = {"discovered": "自动发现", "file": "本地缓存",
                 "user": "用户自填", "fallback": "内置兜底"}.get(st["source"], st["source"])
    return {"ok": True, "count": len(st["list"]), "source": st["source"],
            "source_label": src_label, "bases": st["list"]}
