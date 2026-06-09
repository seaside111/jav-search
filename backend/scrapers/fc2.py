"""
FC2-PPV 刮削器（V1.4.3 新增数据源）

数据源：fc2ppvdb.com —— FC2-PPV 专用数据库，字段最规整（番号/标题/封面/女优/卖家/
        贩卖日/收录时间/标签/马赛克有无）。

与 JavBus 系（avsox/avmoo 复用 _javbus_base）完全不同：fc2ppvdb 是 Laravel + Tailwind
站点，且强制 Cloudflare Turnstile 人机验证——直连只能拿到验证页。因此本模块复用
JavDB 那一套「FlareSolverr 优先 + 增强 httpx 兜底」的取页策略；FlareSolverr 地址默认
复用 JavDB 的配置（javdb_flaresolverr_url），也可用 fc2_flaresolverr_url 单独指定。

FC2 站点不提供磁力；下载链路仍走 Jackett/sukebei（用番号 FC2-PPV-xxxxxxx 检索）。
"""
import re
import asyncio
from typing import Optional
import httpx
from bs4 import BeautifulSoup

from . import _missav
from . import _sukebei
from ._fsgate import (flaresolverr_request as _fs_request, discover_auto as _fs_discover,
                      PRIO_DETAIL, PRIO_SEARCH, PRIO_LATEST)

FC2_BASE = "https://fc2ppvdb.com"
SOURCE = "FC2"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "ja,zh-CN;q=0.9,zh;q=0.8,en;q=0.7",
    "Referer": FC2_BASE + "/",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}


def _abs(href: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return FC2_BASE + href
    return FC2_BASE + "/" + href


def _extract_number(query: str) -> str:
    """从各种写法里抽取 FC2 纯数字番号：
       FC2-PPV-1234567 / FC2PPV1234567 / fc2 1234567 / 1234567 → 1234567"""
    if not query:
        return ""
    m = re.search(r"(\d{5,7})", query)
    return m.group(1) if m else ""


def _display_code(num: str) -> str:
    """统一展示/检索用番号：FC2-PPV-1234567（sukebei/Jackett 最通用的写法）。"""
    return f"FC2-PPV-{num}" if num else "FC2"


# ──────────────────────────────────────────────
# 运行时配置（FlareSolverr / 手动 Cookie）
# FlareSolverr 默认复用 JavDB 的配置，免去重复填写
# ──────────────────────────────────────────────
def _runtime_options() -> dict:
    try:
        from config_manager import load as load_config
        cfg = load_config()
        fs = (cfg.get("fc2_flaresolverr_url") or cfg.get("javdb_flaresolverr_url") or "").strip()
        use_proxy = cfg.get("fc2_flaresolverr_use_proxy",
                            cfg.get("javdb_flaresolverr_use_proxy", True))
        return {
            "flaresolverr_url": fs,
            "cookie": (cfg.get("fc2_cookie") or "").strip(),
            "flaresolverr_use_proxy": use_proxy,
        }
    except Exception:
        return {"flaresolverr_url": "", "cookie": "", "flaresolverr_use_proxy": True}


def _merge_cookies(extra_cookie: str = "") -> dict:
    cookies = {}
    if extra_cookie:
        for part in extra_cookie.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                k, v = k.strip(), v.strip()
                if k:
                    cookies[k] = v
    return cookies


def _is_cf_challenge(html: str, status: int = 200) -> bool:
    """
    是否为 Cloudflare 自动验证/拦截页（interstitial），而非真正的 fc2ppvdb 页面。

    关键：fc2ppvdb **正常页面也内嵌 Cloudflare Turnstile 的 widget**
    （challenges.cloudflare.com/turnstile/v0/api.js、cf-turnstile、turnstile 等），
    所以绝不能把 'turnstile' 当被盾标记——否则会把 FlareSolverr **过盾后拿到的真实页面**
    误判成挑战页（HTTP 200 却报 0 条 + cf_blocked）。

    真正的 CF interstitial 特征：
      - 标题级提示语（just a moment / checking your browser / attention required）；
      - 注入 /cdn-cgi/challenge-platform/ 脚本或 cf-chl-bypass（这是 CF 自动挑战，
        区别于站点自带的 challenges.cloudflare.com/turnstile widget）。
    且页面没有任何业务内容（无 /articles/ 链接）。只要含 /articles/ 文章链接即视为正常页。
    """
    if status in (403, 429, 503):
        return True
    if not html:
        return False
    low = html.lower()
    # 含真实业务链接（文章）→ 一定是过盾后的真实页面，直接放行
    if "/articles/" in low:
        return False
    head = low[:6000]
    interstitial = (
        "just a moment",
        "checking your browser",
        "attention required",
        "enable javascript and cookies to continue",
        "/cdn-cgi/challenge-platform/",   # CF 自动挑战脚本（非站点 Turnstile widget）
        "cf-chl-bypass",
        "cf-browser-verification",
        "<title>too many requests",
    )
    return any(m in head for m in interstitial)


# ──────────────────────────────────────────────
# 取页层：FlareSolverr 优先，否则增强 httpx + 重试
# 返回 (html, status, error)。error == 'cf_challenge' 表示命中盾。
# ──────────────────────────────────────────────
async def _fetch_via_flaresolverr(url: str, flaresolverr_url: str, proxy: Optional[str],
                                  cookies: Optional[dict] = None,
                                  priority: int = PRIO_LATEST) -> tuple[str, int, str]:
    """统一走 _fsgate 的智能适配 + 全局串行；FC2 用更长的 maxTimeout/读超时（页面较慢）。
    priority 透传给闸：详情点击高优先级，可插到首页最新/搜索的批量任务前面。"""
    return await _fs_request(url, flaresolverr_url, proxy, cookies,
                             max_timeout=45000, read_timeout=75.0, priority=priority)


async def _fetch_html(url: str, proxy: Optional[str], opts: Optional[dict] = None,
                      retries: int = 2, priority: int = PRIO_LATEST) -> tuple[str, int, str]:
    opts = opts or _runtime_options()
    cookies = _merge_cookies(opts.get("cookie", ""))
    flaresolverr = opts.get("flaresolverr_url", "")
    # JavDB/FC2 地址都留空 → 自动探测（FC2 强制需要 FlareSolverr，探到才有戏）
    if not flaresolverr:
        flaresolverr = await _fs_discover()
    if flaresolverr:
        fs_proxy = proxy if opts.get("flaresolverr_use_proxy", True) else None
        html, status, err = await _fetch_via_flaresolverr(url, flaresolverr, fs_proxy,
                                                          cookies or None, priority)
        if err and cookies and "flaresolverr:" in err:
            html2, status2, err2 = await _fetch_via_flaresolverr(url, flaresolverr, fs_proxy,
                                                                 None, priority)
            if not err2:
                html, status, err = html2, status2, ""
        if err:
            return html, status, err
        if status and status >= 400:
            if status in (403, 429, 503) or _is_cf_challenge(html, status):
                return html, status, "cf_challenge"
            return html, status, f"HTTP {status}"
        if _is_cf_challenge(html, status):
            return html, status or 200, "cf_challenge"
        return html, status or 200, ""

    proxy_arg = proxy or None
    last_err, last_status = "", 0
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(
                headers=HEADERS, cookies=cookies, proxy=proxy_arg,
                timeout=20, follow_redirects=True,
            ) as client:
                resp = await client.get(url)
            last_status = resp.status_code
            if resp.status_code != 200:
                last_err = f"HTTP {resp.status_code}"
                await asyncio.sleep(0.6 * (attempt + 1))
                continue
            if _is_cf_challenge(resp.text, resp.status_code):
                last_err = "cf_challenge"
                await asyncio.sleep(0.6 * (attempt + 1))
                continue
            return resp.text, resp.status_code, ""
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            await asyncio.sleep(0.6 * (attempt + 1))
    return "", last_status, last_err


# ──────────────────────────────────────────────
# 标签字段提取（列表卡 + 详情共用）
# fc2ppvdb 用「<div>女優：<span>...</span></div>」这种「标签前缀 + span」结构，
# 这种基于文本前缀的提取对 Tailwind class 频繁变动免疫。
# ──────────────────────────────────────────────
_LABELS = {
    "actress": ("女優：", "女优："),
    "tags": ("タグ：", "标签："),
    "release_date": ("販売日：", "贩卖日："),
    "seller": ("販売者：", "贩卖者："),
    "duration": ("収録時間：", "收录时间："),
    "mosaic": ("モザイク：",),
}


def _scan_labels(scope) -> dict:
    """在给定节点范围内扫描带已知前缀的 div，提取字段。"""
    out = {"actress": [], "tags": [], "release_date": "", "seller": "",
           "duration": "", "mosaic": ""}
    for div in scope.find_all("div"):
        txt = div.get_text(" ", strip=True)
        if not txt or "：" not in txt:
            continue
        for field, prefixes in _LABELS.items():
            if any(txt.startswith(p) for p in prefixes):
                if field in ("actress", "tags"):
                    if not out[field]:
                        out[field] = [a.get_text(strip=True)
                                      for a in div.find_all("a") if a.get_text(strip=True)]
                else:
                    if not out[field]:
                        span = div.find("span")
                        val = (span.get_text(strip=True) if span
                               else txt.split("：", 1)[-1].strip())
                        out[field] = val
                break
    return out


def _dur_to_text(s: str) -> str:
    """收录时间原样保留（如 '1:23:45' 或 '83分'），列表/详情直接展示。"""
    return (s or "").strip()


# 列表卡封面：优先取懒加载的真实地址，跳过 data:URI / 占位图 / svg 图标。
# fc2ppvdb 首页卡片多用懒加载——未滚动到视口的卡片 src 仍是占位图，真实地址在 data-*，
# 只读 src 会取到占位图导致前端「有图却加载失败」，故须先读 data-* 并过滤占位。
_IMG_PLACEHOLDER = ("placeholder", "blank", "loading", "spacer", "lazy", "noimage", "1x1")


def _clean_img_src(img) -> str:
    if not img:
        return ""
    for attr in ("data-src", "data-original", "data-lazy-src", "data-lazy",
                 "data-echo", "src"):
        v = (img.get(attr) or "").strip()
        if not v:
            continue
        low = v.lower()
        if low.startswith("data:") or low.endswith(".svg"):
            continue
        if any(k in low for k in _IMG_PLACEHOLDER):
            continue
        return v
    return ""


# ──────────────────────────────────────────────
# 列表页解析（首页最新 / 搜索结果）
# ──────────────────────────────────────────────
def _parse_list(html: str, max_results: int = 300) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    items, seen = [], set()

    # 以「指向 /articles/{数字} 的链接」为种子（不强制锚点内含 <img>——首页卡片的
    # 封面与标题常拆成两个链接，图片也可能是懒加载/背景图），按番号去重。
    for a in soup.find_all("a", href=True):
        m = re.search(r"/articles/(\d+)", a["href"])
        if not m:
            continue
        num = m.group(1)
        if num in seen:
            continue

        # 向上收敛到「只包含本番号」的最紧凑容器：一旦父节点开始包含其它番号即停止，
        # 防止把相邻卡片的标签信息混进来。
        card, parent, hops = a, a.parent, 0
        while parent is not None and hops < 6:
            nums = set(re.findall(r"/articles/(\d+)", str(parent)))
            if len(nums) > 1:
                break
            card, parent = parent, parent.parent
            hops += 1

        # 封面：① 容器内首张图（优先懒加载真实地址、过滤占位图）；② 否则取背景图；
        # ③ 仍无则用 MissAV 的确定性封面 URL 兜底（零额外请求，404 由前端图片代理优雅降级）。
        # 整块加保护：任何边角解析异常都不得拖垮整页解析/诊断，最差退回确定性封面。
        img = card.find("img")
        cover = ""
        try:
            cover = _abs(_clean_img_src(img))
            if not cover:
                mbg = re.search(r'background-image\s*:\s*url\((["\']?)([^"\')]+)\1\)',
                                str(card), re.I)
                if mbg:
                    cover = _abs(mbg.group(2))
        except Exception:
            cover = ""
        if not cover:
            cover = _missav.cover_url(num)

        # 标题：容器内首个「有文字且非纯番号」的文章链接；兜底 img alt / 番号
        title = ""
        for ta in card.find_all("a", href=True):
            if not re.search(r"/articles/\d+", ta["href"]):
                continue
            t = ta.get_text(strip=True)
            if t and not re.fullmatch(r"\d+", t):
                title = t
                break
        if not title and img:
            title = img.get("alt", "").strip()

        labels = _scan_labels(card)
        seen.add(num)
        items.append({
            "code": _display_code(num),
            "title": title or _display_code(num),
            "cover": cover,
            "url": f"{FC2_BASE}/articles/{num}",
            "source": SOURCE,
            "release_date": labels["release_date"],
            "duration": _dur_to_text(labels["duration"]),
            "director": labels["seller"],
            "studio": labels["seller"],
            "label": "FC2",
            "series": "",
            "score": "",
            "score_count": "",
            "has_magnet": False,
            "actors": [{"name": n, "avatar": ""} for n in labels["actress"]],
            "tags": labels["tags"],
            "samples": [],
            "magnets": [],
            "description": "",
            "detail_loaded": False,
        })
        if len(items) >= max_results:
            break
    return items


async def _fetch_list_pages(url_builder, proxy, max_results, max_pages=10,
                            priority: int = PRIO_LATEST,
                            fs_max_pages: int = 1) -> list[dict]:
    opts = _runtime_options()
    # 走 FlareSolverr 时每页都要过盾、很慢：默认只抓 1 页（约 20-30 条）避免超单源超时被丢弃；
    # 首页最新可由 fs_max_pages 放宽到前 N 页（页与页之间经 _fsgate 串行过盾，N 越大越慢）。
    if opts.get("flaresolverr_url"):
        max_pages = min(max_pages, max(1, fs_max_pages))
    all_items, seen = [], set()
    for page in range(1, max_pages + 1):
        page_url = url_builder(page)
        html, status, err = await _fetch_html(page_url, proxy, opts, priority=priority)
        if err:
            print(f"[FC2] list page{page} 失败: {err} (HTTP {status}) {page_url}")
            break
        page_items = _parse_list(html, max_results)
        if not page_items:
            break
        added = 0
        for it in page_items:
            key = it["code"]
            if key in seen:
                continue
            seen.add(key)
            all_items.append(it)
            added += 1
            if len(all_items) >= max_results:
                break
        if len(all_items) >= max_results or added == 0:
            break
    return all_items[:max_results]


# ──────────────────────────────────────────────
# 列表轻量增强：用 MissAV（直连，不经 FlareSolverr）补列表卡的「标题/封面」
# fc2ppvdb 首页卡片常封面懒加载占位、标题缺失；而 MissAV 对 FC2 的「标题 + 封面」覆盖
# 可靠，且默认走直连镜像（仅命中 CF 才回退 FlareSolverr）——因此在列表阶段补全几乎不
# 增加 FlareSolverr 负担。样品图仍留到打开详情时再抓。
#  · 只对「标题缺失」的卡片发起补全（首页已带标题时零网络开销）。
#  · 进程内缓存 num→结果，翻页/刷新命中即零开销。
#  · 信号量限并发，封面无论是否命中都已有确定性 fourhoi 兜底（前端代理拉取）。
# ──────────────────────────────────────────────
_LIST_ENRICH_CACHE: dict = {}
_LIST_ENRICH_CACHE_MAX = 2000


def _cache_put(num: str, data: dict) -> None:
    """写入 MissAV 结果缓存（带容量上限 LRU 近似：满了丢最早的）。"""
    if not num:
        return
    if num in _LIST_ENRICH_CACHE:
        _LIST_ENRICH_CACHE[num] = data
        return
    if len(_LIST_ENRICH_CACHE) >= _LIST_ENRICH_CACHE_MAX:
        _LIST_ENRICH_CACHE.pop(next(iter(_LIST_ENRICH_CACHE)))
    _LIST_ENRICH_CACHE[num] = data


def _missav_enabled() -> bool:
    try:
        from config_manager import load as load_config
        return bool(load_config().get("fc2_missav_enabled", True))
    except Exception:
        return True


def _needs_title(it: dict) -> bool:
    t = (it.get("title") or "").strip()
    return (not t) or t == (it.get("code") or "")


async def _enrich_one(it: dict, proxy: Optional[str], sem: asyncio.Semaphore) -> None:
    num = _extract_number(it.get("code") or it.get("url") or "")
    if not num:
        return
    data = _LIST_ENRICH_CACHE.get(num)
    if data is None:
        async with sem:
            data = _LIST_ENRICH_CACHE.get(num)   # 排队期间可能已被其它请求填充
            if data is None:
                try:
                    # 列表补全严格直连 MissAV，禁用 FlareSolverr 回退，避免压垮 FlareSolverr
                    data = await _missav.fetch_fc2(num, proxy,
                                                   allow_flaresolverr=False) or {}
                except Exception:
                    data = {}
                _cache_put(num, data)
    if not data:
        return
    if data.get("title") and _needs_title(it):
        it["title"] = data["title"]
    if data.get("cover"):
        it["cover"] = data["cover"]           # 升级为 MissAV 真实封面（og:image）
    srcs = it.get("sources") or [it.get("source", SOURCE)]
    if "MissAV" not in srcs:
        it["sources"] = list(srcs) + ["MissAV"]


async def _enrich_list_missav(items: list[dict], proxy: Optional[str],
                              limit: int = 60, concurrency: int = 8) -> list[dict]:
    if not items or not _missav_enabled():
        return items
    targets = [it for it in items if _needs_title(it)][:limit]
    if not targets:
        return items
    sem = asyncio.Semaphore(concurrency)
    await asyncio.gather(*[_enrich_one(it, proxy, sem) for it in targets],
                         return_exceptions=True)
    return items


# ──────────────────────────────────────────────
# 后台 MissAV 预抓（V1.4.4，用户选「只预抓便宜部分」）
# sukebei 最新卡只有种子标题 + fourhoi 封面；MissAV 详情(直连、不过盾、便宜)能补
# 干净标题 + 真封面 + 样品图。这里后台**串行 + 节流 + 直连**慢慢把最新 N 条的 MissAV
# 结果灌进 _LIST_ENRICH_CACHE：① 后续刷新时列表卡升级成干净标题/封面；② 点开详情时
# 样品图秒出（女优/标签仍点开走 fc2ppvdb，按用户选择不预抓）。全程不碰 FlareSolverr。
# ──────────────────────────────────────────────
_PREWARM_LOCK = asyncio.Lock()       # 同一时刻只跑一个预抓 runner，避免并发请求重复预抓
_PREWARM_TASKS: set = set()          # 持有后台任务引用，防止被 GC 提前回收
_PREWARM_THROTTLE = 0.5              # 每条之间睡 0.5s，进一步降低存在感


def _prefetch_enabled() -> bool:
    try:
        from config_manager import load as load_config
        return bool(load_config().get("fc2_prefetch_missav", True))
    except Exception:
        return True


def _prefetch_count() -> int:
    try:
        from config_manager import load as load_config
        n = int(load_config().get("fc2_prefetch_count", 20))
    except Exception:
        n = 20
    return max(0, min(n, 60))


def _apply_cached_missav(items: list[dict]) -> None:
    """用**已预热**的 MissAV 缓存就地升级列表卡（仅命中缓存，零网络）。
    FC2 最新卡的临时标题来自种子文件名、质量低 → 有 MissAV 干净标题就替换。"""
    for it in items:
        num = _extract_number(it.get("code") or it.get("url") or "")
        data = _LIST_ENRICH_CACHE.get(num) if num else None
        if not data:
            continue
        if data.get("title"):
            it["title"] = data["title"]
        if data.get("cover"):
            it["cover"] = data["cover"]
        srcs = it.get("sources") or [it.get("source", SOURCE)]
        if "MissAV" not in srcs:
            it["sources"] = list(srcs) + ["MissAV"]


async def _prewarm_missav_bg(nums: list[str], proxy: Optional[str]) -> None:
    if _PREWARM_LOCK.locked():
        return
    async with _PREWARM_LOCK:
        for num in nums:
            if _LIST_ENRICH_CACHE.get(num):     # 已有正向缓存，跳过
                continue
            try:
                data = await _missav.fetch_fc2(num, proxy, allow_flaresolverr=False)
            except Exception:
                data = None
            if data:                            # 只缓存正向结果；空的留给详情走 FS 兜底
                _cache_put(num, data)
            await asyncio.sleep(_PREWARM_THROTTLE)


def _schedule_prewarm(items: list[dict], proxy: Optional[str]) -> None:
    """安排后台预抓（fire-and-forget）。在运行中的事件循环里调度，请求立刻返回不等它。"""
    if not _prefetch_enabled() or not _missav_enabled():
        return
    if _PREWARM_LOCK.locked():
        return
    nums = []
    for it in items[:_prefetch_count()]:
        num = _extract_number(it.get("code") or it.get("url") or "")
        if num and not _LIST_ENRICH_CACHE.get(num):
            nums.append(num)
    if not nums:
        return
    try:
        task = asyncio.create_task(_prewarm_missav_bg(nums, proxy))
        _PREWARM_TASKS.add(task)
        task.add_done_callback(_PREWARM_TASKS.discard)
    except RuntimeError:
        pass                                    # 无运行中的事件循环（理论上不会发生）


async def _missav_for_detail(num: str, proxy: Optional[str]) -> Optional[dict]:
    """详情用的 MissAV 取数：优先用预热缓存（零网络/秒出样品图）；
    未预热/预热为空才正常抓（详情为交互请求，允许回退 FlareSolverr）。"""
    if not num:
        return None
    cached = _LIST_ENRICH_CACHE.get(num)
    if cached:                                  # 命中且有内容
        return cached
    data = await _missav.fetch_fc2(num, proxy, priority=PRIO_DETAIL)
    if data:
        _cache_put(num, data)
    return data


# ──────────────────────────────────────────────
# 列表级搜索
# ──────────────────────────────────────────────
async def search_list(query: str, mode: str, proxy: Optional[str] = None,
                      max_results: int = 300) -> list[dict]:
    q = (query or "").strip()
    num = _extract_number(q)

    # 番号搜索：直接命中 /articles/{num} 详情页，解析成单条卡片返回（最稳最快）。
    # 判定为番号：mode==code，或查询本身就是 FC2 写法 / 纯 6-7 位数字。
    looks_like_code = bool(num) and (
        mode == "code"
        or re.search(r"fc2", q, re.I)
        or re.fullmatch(r"\d{5,7}", q)
    )
    if looks_like_code:
        detail = await fetch_detail(f"{FC2_BASE}/articles/{num}", proxy)
        if detail:
            detail = dict(detail)
            detail["detail_loaded"] = True
            return [detail]
        return []

    # 关键词 / 女优：站内搜索
    from urllib.parse import quote
    kw = quote(q)

    def build(page):
        base = f"{FC2_BASE}/search?stext={kw}"
        return base if page == 1 else f"{base}&page={page}"

    items = await _fetch_list_pages(build, proxy, max_results, max_pages=10,
                                    priority=PRIO_SEARCH)
    return await _enrich_list_missav(items, proxy)


# ──────────────────────────────────────────────
# 首页最新片源
# ──────────────────────────────────────────────
def _sort_by_number_desc(items: list[dict]) -> list[dict]:
    """按 FC2-PPV 编号(数字)降序重排（V1.4.4）。

    FC2 的编号是平台在卖家上架/注册时分配的自增 ID——整体上编号越大越新。
    fc2ppvdb 首页是按它自己的「收录/販売日」排的、混着大小号，最大的新号未必在最前；
    而「编号越大越新」是用户的核心诉求，故抓回这批后统一按编号降序，让真正的新号浮到最前。
    无法解析出编号的条目（理论上不该有）排到最后，保持稳定。"""
    def _key(it: dict) -> int:
        num = _extract_number(it.get("code") or it.get("url") or "")
        return int(num) if num else -1
    return sorted(items, key=_key, reverse=True)


def _latest_pages() -> int:
    """首页 FC2 最新抓取页数（V1.4.4，配置 fc2_latest_pages，默认 1，硬上限 3）。

    实测（probe_fc2_pages）：fc2ppvdb **首页 `/` 不支持翻页**——`?page=2` 与 `?page=1`
    返回完全相同的内容，`?per_page=`/`?limit=` 也被忽略。所以主路径默认只取 1 页足矣
    （首页一次就给约 100 条）。此开关仅对「需登录的 `/articles` 兜底列表」可能有效，
    保留作未来扩展；默认 1，每页都要过 FlareSolverr 的盾、串行较慢，硬封顶 3。"""
    try:
        from config_manager import load as load_config
        n = int(load_config().get("fc2_latest_pages", 1))
    except Exception:
        n = 1
    return max(1, min(n, 3))


def _sukebei_enabled() -> bool:
    """是否启用 sukebei 最新发现源（V1.4.4，配置 fc2_latest_use_sukebei，默认 True）。"""
    try:
        from config_manager import load as load_config
        return bool(load_config().get("fc2_latest_use_sukebei", True))
    except Exception:
        return True


def _merge_latest(rich_items: list[dict], extra_items: list[dict]) -> list[dict]:
    """合并两路最新：以 rich_items（fc2ppvdb，字段全）为主，按番号去重，
    extra_items（sukebei，字段少但更新）只补充 rich 里没有的番号。"""
    out = list(rich_items)
    seen = {it.get("code") for it in rich_items}
    for it in extra_items:
        if it.get("code") not in seen:
            out.append(it)
            seen.add(it.get("code"))
    return out


async def _fetch_fc2ppvdb_latest(proxy: Optional[str], pool: int) -> list[dict]:
    """fc2ppvdb 首页最新（经 FlareSolverr，较慢）。一次约 100 条「最新+人気」混排、不分页。
    先全量收集（不在文档顺序上提前截断），交由上层统一编号降序。"""
    npages = _latest_pages()

    def build(page):
        return FC2_BASE + ("/" if page == 1 else f"/?page={page}")

    items = await _fetch_list_pages(build, proxy, pool, max_pages=npages,
                                    priority=PRIO_LATEST, fs_max_pages=npages)
    if not items:
        # 兜底：文章列表页（通常需登录；登录后此路径可能按日期分页，故沿用 npages）
        items = await _fetch_list_pages(
            lambda p: f"{FC2_BASE}/articles" + ("" if p == 1 else f"?page={p}"),
            proxy, pool, max_pages=npages, priority=PRIO_LATEST, fs_max_pages=npages)
    return items


async def get_latest(proxy: Optional[str] = None, max_results: int = 40) -> list[dict]:
    """FC2 首页最新片源（V1.4.4 改为 sukebei 优先）。

    数据源短板：fc2ppvdb 的新着列表封顶在某个号（实测只到 4894253）、够不到市面最新。
    sukebei 种子站按 id 倒序＝最新、**直连不过盾、最快**，能拿到 fc2ppvdb 够不到的新号。

    策略：① 先抓 sukebei（快）；够 max_results 就直接用，**完全跳过慢的 fc2ppvdb 首页**，
    既新又快。② sukebei 不够 / 关闭 / 失败时，再抓 fc2ppvdb 首页补足（字段更全）。
    两路按番号去重后统一编号降序、截取最新 max_results。sukebei 卡的标题/封面较朴素
    （种子标题 + fourhoi 封面），完整信息在点开详情时由 fc2ppvdb/MissAV 按需补全。"""
    pool = max(max_results * 2, 200)

    sukebei_items = []
    if _sukebei_enabled():
        try:
            sukebei_items = await _sukebei.fetch_fc2_latest(proxy, limit=pool)
        except Exception as e:
            # sukebei 出任何问题都只降级到 fc2ppvdb，绝不让 FC2 最新整个消失
            print(f"[FC2] sukebei 最新失败，降级 fc2ppvdb: {type(e).__name__}: {e}")
            sukebei_items = []

    # sukebei 已够量 → 不再碰慢的 fc2ppvdb 首页，直接排序返回（最新且最快）
    if len(sukebei_items) >= max_results:
        items = sukebei_items
    else:
        # 不够（或未启用/失败）：抓 fc2ppvdb 首页补全，rich 为主、sukebei 补新号
        rich = await _fetch_fc2ppvdb_latest(proxy, pool)
        items = _merge_latest(rich, sukebei_items)

    # 编号降序后截取最新 max_results：让真正最新的番号排在最前（详见 _sort_by_number_desc）
    items = _sort_by_number_desc(items)[:max_results]
    items = await _enrich_list_missav(items, proxy)   # 缺标题的卡内联补全（sukebei 卡有标题→跳过，快）
    _apply_cached_missav(items)                        # 已预热的卡升级成干净标题/封面（零网络）
    _schedule_prewarm(items, proxy)                    # 后台慢慢抓 MissAV(含样品图)入缓存
    return items


# ──────────────────────────────────────────────
# 详情
# ──────────────────────────────────────────────
async def fetch_detail(url: str, proxy: Optional[str] = None) -> Optional[dict]:
    """
    FC2 详情（V1.4.4：改为 **MissAV-only**，不再走 fc2ppvdb / FlareSolverr）。

    背景：FC2 番号已由 sukebei 发现、标题/封面/样品图由 MissAV 提供（且可后台预热）；
    fc2ppvdb 在详情里唯一独有的是女优/标签/販売日，而 FC2 这类无此需求，且它对最新片
    常为空、却要花一次慢速过盾——故详情移除 fc2ppvdb，**点开即出、彻底不碰 FlareSolverr**。
    （fc2ppvdb 仍保留用于关键词/女优名搜索与首页最新兜底。）下载仍走 Jackett/sukebei。

    MissAV 优先用后台预热缓存（命中则样品图秒出、零网络），未预热才现抓。
    """
    num = _extract_number(url)
    mv = await _missav_for_detail(num, proxy) if num else None
    info = {
        "code": _display_code(num),
        "title": _display_code(num),
        "cover": "",
        "url": url if url.startswith("http") else f"{FC2_BASE}/articles/{num}",
        "source": SOURCE, "release_date": "", "duration": "", "director": "",
        "studio": "", "label": "FC2", "series": "", "score": "", "score_count": "",
        "actors": [], "tags": [], "samples": [], "magnets": [], "description": "",
        "detail_loaded": True,
    }
    info = _merge_missav(info, mv, num)
    if not info.get("cover"):
        info["cover"] = _missav.cover_url(num)   # MissAV 无 og:image 时回退 fourhoi 确定性封面
    return info


def _merge_missav(info: dict, mv: Optional[dict], num: str) -> dict:
    """把 MissAV 数据并入 FC2 详情骨架：只补缺，不覆盖已有字段（标题/封面/样品图/预览）。"""
    if not mv:
        return info
    real_title = info.get("title") and info["title"] != _display_code(num)
    if not real_title and mv.get("title"):
        info["title"] = mv["title"]
    if not info.get("cover") and mv.get("cover"):
        info["cover"] = mv["cover"]
    if not info.get("actors") and mv.get("actors"):
        info["actors"] = [{"name": n, "avatar": ""} for n in mv["actors"]]
    if not info.get("tags") and mv.get("tags"):
        info["tags"] = mv["tags"]
    # 样品图（MissAV 播放器逐帧截图）：fc2ppvdb 没有时用 MissAV 的
    if not info.get("samples") and mv.get("samples"):
        info["samples"] = mv["samples"]
    if not info.get("preview_video") and mv.get("preview_video"):
        info["preview_video"] = mv["preview_video"]
    # 标记来源，便于前端展示「FC2 + MissAV」
    srcs = info.get("sources") or [info.get("source", SOURCE)]
    if "MissAV" not in srcs:
        srcs = list(srcs) + ["MissAV"]
    info["sources"] = srcs
    return info


# ──────────────────────────────────────────────
# 连通诊断（与 JavDB 一致的接口风格，便于前端复用）
# 注：诊断仍探 fc2ppvdb（关键词搜索与首页最新兜底仍用它）；详情已不再用 fc2ppvdb。
# ──────────────────────────────────────────────
def _inspect_page(html: str) -> dict:
    """解析不到片源时，回显页面标题/可见文本片段/文章链接数，辅助区分
       「真挑战页 / 登录墙 / 选择器不匹配 / 空页」。"""
    soup = BeautifulSoup(html or "", "html.parser")
    t = soup.select_one("title")
    title = t.get_text(strip=True) if t else ""
    # 文章链接数：FlareSolverr 真过盾后的列表页应 >0
    article_links = len(set(re.findall(r"/articles/(\d+)", html or "")))
    for tag in soup(["script", "style", "noscript"]):
        tag.extract()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:300]

    low = (title + " " + text).lower()
    kind = ""
    if any(k in low for k in ("just a moment", "checking your browser",
                              "attention required", "/cdn-cgi/challenge-platform/")):
        kind = "cf_interstitial"
    elif any(k in low for k in ("ログイン", "log in", "sign in", "登录", "登入",
                                "パスワード", "password", "メールアドレス")):
        kind = "login_wall"
    return {"title": title, "snippet": text, "article_links": article_links, "kind": kind}


async def diagnose(proxy: Optional[str] = None) -> dict:
    opts = _runtime_options()
    # 地址留空：诊断时强制自动探测一次，用于本次取页与回显
    fs_auto = ""
    if not opts.get("flaresolverr_url"):
        fs_auto = await _fs_discover(force=True)
        if fs_auto:
            opts = {**opts, "flaresolverr_url": fs_auto}
    via = "flaresolverr" if opts.get("flaresolverr_url") else "httpx"
    html, status, err = await _fetch_html(FC2_BASE + "/", proxy, opts, retries=1)
    cf_blocked = (err == "cf_challenge") or _is_cf_challenge(html, status)
    items = _parse_list(html, 5) if html and not cf_blocked else []
    reachable = bool(items)

    page = _inspect_page(html) if html else {"title": "", "snippet": "",
                                             "article_links": 0, "kind": ""}

    if reachable:
        message = f"连接正常，解析到 {len(items)} 条最新片源。"
    elif not opts.get("flaresolverr_url"):
        message = ("FC2PPVDB 启用了 Cloudflare Turnstile 人机验证，直连无法通过；"
                   "且未自动探测到本机/同宿主机的 FlareSolverr。请确认已部署 FlareSolverr（地址栏留空会"
                   "自动探测），或直接在地址栏手填它的 URL（可复用 JavDB 的）。")
    elif err and err.startswith("flaresolverr"):
        message = f"FlareSolverr 报错：{err.split('flaresolverr:', 1)[-1].strip() or err}"
    elif cf_blocked:
        message = ("命中 Cloudflare 自动挑战页，FlareSolverr 未能过验证——"
                   "建议升级 FlareSolverr 到最新版、确认其内置 Chrome 正常，或更换出口 IP/节点。")
    elif page.get("article_links", 0) > 0:
        # FlareSolverr 已过盾拿到真实页面（含文章链接），但列表选择器没匹配到条目
        message = (f"FlareSolverr 已过盾并拿到页面（检测到 {page['article_links']} 个文章链接），"
                   "但列表解析未命中条目——可能 fc2ppvdb 列表页结构有变。"
                   "请把下方「页面标题/片段」反馈以便调整选择器；详情页与番号搜索通常仍可用。")
    elif page.get("kind") == "login_wall":
        message = ("返回的是登录页：该出口 IP 被要求登录。可在设置填入浏览器登录后导出的 "
                   "fc2ppvdb Cookie，或更换出口节点。")
    elif err:
        message = f"请求失败：{err}。检查代理与 FlareSolverr 是否可访问 fc2ppvdb.com。"
    else:
        message = "未解析到条目，页面结构可能变化或被拦截（见下方页面回显）。"

    return {
        "reachable": reachable,
        "cf_blocked": cf_blocked,
        "http_status": status,
        "item_count": len(items),
        "via": via,
        "fs_endpoint": fs_auto,              # 自动探测到的 FlareSolverr 地址（手填则为空）
        "error": err,
        "page_title": page.get("title", ""),
        "page_snippet": page.get("snippet", ""),
        "article_links": page.get("article_links", 0),
        "page_kind": page.get("kind", ""),
        "message": (message + (f" 已自动探测到 FlareSolverr：{fs_auto}。" if fs_auto and reachable else "")),
    }
