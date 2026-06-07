"""
JavDB 刮削器（V1.4.2 — 重点优化）

相对 1.4.1 的改进：
  1. 反爬增强：自动携带 over18 / locale / theme 等 Cookie，补全更像浏览器的请求头，
     失败自动重试，从根上解决「Cloudflare 拦截 / 年龄门 → 列表 0 条」的问题。
  2. 可选 FlareSolverr：配置了 javdb_flaresolverr_url 时，所有 JavDB 请求改走
     FlareSolverr 拿到过盾后的 HTML（带 cf_clearance），适合服务器自建 CF 突破服务。
  3. 连通诊断 diagnose()：返回 HTTP 状态 / 是否命中 CF 盾 / 出口 IP 所在国 / 解析条数，
     用于判断「是否需要日本 IP 才能访问」。
  4. 详情抓取扩充：磁力链、样品图、评分人数、演员、片商/发行/系列等字段更完整，
     供合并时补全其它来源缺失的信息。

JavDB 特有信息（磁力链 magnets / 样品图 samples / 评分人数 score_count）
仅在 fetch_detail 详情抓取阶段返回，列表页保持轻量。
"""
import re
import asyncio
from typing import Optional
import httpx
from bs4 import BeautifulSoup

from ._fsgate import (flaresolverr_request as _fs_request, fs_candidates as _fs_candidates,
                      normalize_fs_url as _normalize_fs_url, resolved_endpoint as _fs_resolved,
                      discover_auto as _fs_discover)

JAVDB_BASE = "https://javdb.com"
SOURCE = "JavDB"

# 模拟桌面 Chrome 的完整请求头，降低被 WAF 识别为脚本的概率
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
    "Referer": "https://javdb.com/",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

# 绕过年龄确认门 + 锁定中文界面（界面语言影响详情字段中文标签匹配）
BASE_COOKIES = {
    "over18": "1",
    "locale": "zh",
    "theme": "auto",
}


def _abs(href: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("http"):
        return href
    return JAVDB_BASE + href


# ──────────────────────────────────────────────
# JavDB 运行时配置（FlareSolverr / 手动 Cookie）
# 由 config_manager 懒加载，避免改动聚合器与上层签名
# ──────────────────────────────────────────────
def _runtime_options() -> dict:
    """读取 JavDB 专属运行配置：FlareSolverr 地址、手动 cf_clearance Cookie 等。"""
    try:
        from config_manager import load as load_config
        cfg = load_config()
        return {
            "flaresolverr_url": (cfg.get("javdb_flaresolverr_url") or "").strip(),
            "cookie": (cfg.get("javdb_cookie") or "").strip(),
            # FlareSolverr 是否复用主代理（默认是）。某些部署 FlareSolverr 自带出口，应关掉走直连。
            "flaresolverr_use_proxy": cfg.get("javdb_flaresolverr_use_proxy", True),
        }
    except Exception:
        return {"flaresolverr_url": "", "cookie": "", "flaresolverr_use_proxy": True}



def _merge_cookies(extra_cookie: str = "") -> dict:
    """合并基础 Cookie 与用户手动填入的 Cookie 字符串（如 cf_clearance=xxx; ...）。"""
    cookies = dict(BASE_COOKIES)
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
    判断返回内容是否为 Cloudflare 验证页 / 拦截页（而非真正的 JavDB 页面）。
    注意：空内容不再一律当作 CF——空内容可能是代理/上游 5xx 错误，
    交由上层按 status 区分，避免把代理故障误报成「CF 盾」。
    """
    if status in (403, 429, 503):
        return True
    if not html:
        return False
    head = html[:8000].lower()
    markers = (
        "just a moment",
        "cf-browser-verification",
        "challenge-platform",
        "cf-chl",
        "checking your browser",
        "attention required",
        "enable javascript and cookies to continue",
    )
    return any(m in head for m in markers)


# ──────────────────────────────────────────────
# 统一取页层：FlareSolverr 优先，否则增强 httpx + 重试
# 返回 (html, status, error)
# ──────────────────────────────────────────────
async def _fetch_via_flaresolverr(url: str, flaresolverr_url: str, proxy: Optional[str],
                                  cookies: Optional[dict] = None) -> tuple[str, int, str]:
    """
    通过 FlareSolverr 获取过盾后的 HTML（统一走 _fsgate 的智能适配 + 全局串行）。
    cookies 用于传入 over18 等绕过年龄门；地址智能适配 / 候选回退 / 缓存 / 串行均由共享层处理。
    """
    return await _fs_request(url, flaresolverr_url, proxy, cookies,
                             max_timeout=40000, read_timeout=70.0)


async def _fetch_html(url: str, proxy: Optional[str], opts: Optional[dict] = None,
                      retries: int = 2) -> tuple[str, int, str]:
    """
    取单页 HTML。优先 FlareSolverr，其次增强 httpx（带 Cookie/请求头/重试）。
    返回 (html, status_code, error_msg)。命中 CF 盾时 error_msg 标记 'cf_challenge'。
    """
    opts = opts or _runtime_options()
    cookies = _merge_cookies(opts.get("cookie", ""))
    flaresolverr = opts.get("flaresolverr_url", "")
    # 设置页留空 → 自动探测本机/同宿主机的 FlareSolverr（带缓存/负缓存，探不到则回退直连）
    if not flaresolverr:
        flaresolverr = await _fs_discover()
    if flaresolverr:
        # FlareSolverr 用的代理：默认复用主代理；关掉则让其走自身网络出口（直连）
        fs_proxy = proxy if opts.get("flaresolverr_use_proxy", True) else None
        html, status, err = await _fetch_via_flaresolverr(url, flaresolverr, fs_proxy, cookies)
        # 带 Cookie 报错时，自动重试一次不带 Cookie：
        # 某些 FlareSolverr 版本对 cookies 字段敏感会直接 500，去掉 Cookie 往往就能过盾
        # （代价是可能停在年龄门，但至少能拿到页面、由上层识别）。
        # 仅在「确实连上了」FlareSolverr 却报错时重试（err 形如 'flaresolverr: ...'）；
        # 连不上（'flaresolverr 异常: ConnectTimeout' 等）再探一轮也是白费，跳过。
        if err and cookies and "flaresolverr:" in err:
            html2, status2, err2 = await _fetch_via_flaresolverr(url, flaresolverr, fs_proxy, None)
            if not err2:
                html, status, err = html2, status2, ""
        if err:
            return html, status, err
        # 上游/代理返回 4xx/5xx：区分 CF 盾与普通错误
        if status and status >= 400:
            if status in (403, 429, 503) or _is_cf_challenge(html, status):
                return html, status, "cf_challenge"
            return html, status, f"HTTP {status}"
        if _is_cf_challenge(html, status):
            return html, status or 200, "cf_challenge"
        return html, status or 200, ""

    proxy_arg = proxy or None
    last_err = ""
    last_status = 0
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
                # 4xx/5xx 大多是被盾，短暂退避后再试
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
# 列表页解析（卡片级）
# ──────────────────────────────────────────────
def _parse_list(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for item in soup.select("div.item"):
        a = item.select_one("a[href]")
        if not a:
            continue
        url = _abs(a.get("href", ""))
        if "/v/" not in url:
            continue

        cover = ""
        img = item.select_one("div.cover img") or item.select_one("img")
        if img:
            cover = img.get("src") or img.get("data-src") or ""
            if cover.startswith("//"):
                cover = "https:" + cover

        code = ""
        title = ""
        title_tag = item.select_one("div.video-title")
        if title_tag:
            strong = title_tag.find("strong")
            if strong:
                code = strong.get_text(strip=True)
                title = title_tag.get_text(" ", strip=True).replace(code, "", 1).strip()
            else:
                title = title_tag.get_text(strip=True)

        score = ""
        score_count = ""
        score_tag = item.select_one("div.score .value") or item.select_one("div.score")
        if score_tag:
            score_text = score_tag.get_text(strip=True)
            m = re.search(r"([\d.]+)\s*分", score_text) or re.search(r"([\d.]+)", score_text)
            if m:
                score = m.group(1)
            mc = re.search(r"([\d,]+)\s*人", score_text)
            if mc:
                score_count = mc.group(1).replace(",", "")

        release_date = ""
        meta = item.select_one("div.meta")
        if meta:
            release_date = meta.get_text(strip=True)

        # 列表页角标：是否带磁力（JavDB 用绿色 tag 标记）
        has_magnet = bool(item.select_one("span.tag.is-success, .tags .is-success"))

        items.append({
            "code": code,
            "title": title or code,
            "cover": cover,
            "url": url,
            "source": SOURCE,
            "release_date": release_date,
            "duration": "",
            "director": "",
            "studio": "",
            "label": "",
            "series": "",
            "score": score,
            "score_count": score_count,
            "has_magnet": has_magnet,
            "actors": [],
            "tags": [],
            "samples": [],
            "magnets": [],
            "description": "",
            "detail_loaded": False,
        })
    return items


async def _fetch_list_only(list_url_builder, proxy, max_results, max_pages=20) -> list[dict]:
    opts = _runtime_options()
    # 走 FlareSolverr 时每页都很慢（浏览器渲染+过盾），只抓 1 页（约 28-40 条，足够首页/首屏），
    # 避免多页累加超过单源超时被整体丢弃；直连（增强 httpx）则保持原页数上限。
    if opts.get("flaresolverr_url"):
        max_pages = 1
    all_items, seen = [], set()
    for page in range(1, max_pages + 1):
        page_url = list_url_builder(page)
        html, status, err = await _fetch_html(page_url, proxy, opts)
        if err:
            print(f"[JavDB] list page{page} 失败: {err} (HTTP {status}) {page_url}")
            break
        page_items = _parse_list(html)
        if not page_items:
            break
        added = 0
        for it in page_items:
            key = (it.get("code") or it.get("url")).upper()
            if key in seen:
                continue
            seen.add(key)
            all_items.append(it)
            added += 1
            if len(all_items) >= max_results:
                break
        if len(all_items) >= max_results:
            break
        if added < 10:
            break
    return all_items[:max_results]


# ──────────────────────────────────────────────
# 列表级搜索
# ──────────────────────────────────────────────
async def search_list(query: str, mode: str, proxy: Optional[str] = None, max_results: int = 300) -> list[dict]:
    f = "actor" if mode == "actor" else "all"
    search_base = f"{JAVDB_BASE}/search?q={query}&f={f}"

    def build(page):
        return search_base if page == 1 else f"{search_base}&page={page}"

    # 番号搜索结果通常很少，限制页数
    pages = 2 if mode == "code" else 20
    return await _fetch_list_only(build, proxy, max_results, max_pages=pages)


# ──────────────────────────────────────────────
# 首页最新片源
# ──────────────────────────────────────────────
async def get_latest(proxy: Optional[str] = None, max_results: int = 40) -> list[dict]:
    pages = max(3, min(max_results // 28 + 2, 8))

    def build(page):
        # 最新有码列表
        return f"{JAVDB_BASE}/censored?sort_type=0" + ("" if page == 1 else f"&page={page}")
    items = await _fetch_list_only(build, proxy, max_results, max_pages=pages)
    if not items:
        # 兜底用首页
        items = await _fetch_list_only(
            lambda p: JAVDB_BASE + ("" if p == 1 else f"/?page={p}"),
            proxy, max_results, max_pages=pages)
    return items


# ──────────────────────────────────────────────
# 详情
# ──────────────────────────────────────────────
async def fetch_detail(url: str, proxy: Optional[str] = None) -> Optional[dict]:
    html, status, err = await _fetch_html(url, proxy, _runtime_options())
    if err or not html:
        print(f"[JavDB] detail 失败 {url}: {err} (HTTP {status})")
        return None
    return _parse_detail(html, url)


def _parse_detail(html: str, url: str) -> Optional[dict]:
    soup = BeautifulSoup(html, "html.parser")

    title_tag = soup.select_one("h2.title strong") or soup.select_one("title")
    if not title_tag:
        return None
    title = title_tag.get_text(strip=True)

    cover = ""
    cover_tag = soup.select_one("div.column-video-cover img") or soup.select_one("img.video-cover")
    if cover_tag:
        cover = cover_tag.get("src") or cover_tag.get("data-src") or ""
        if cover.startswith("//"):
            cover = "https:" + cover

    info = {
        "title": title, "cover": cover, "url": url, "source": SOURCE,
        "code": "", "release_date": "", "duration": "", "director": "",
        "studio": "", "label": "", "series": "", "score": "", "score_count": "",
        "actors": [], "tags": [], "samples": [], "magnets": [],
        "description": "", "detail_loaded": True,
    }

    panels = soup.select("nav.panel.movie-panel-info .panel-block")
    for panel in panels:
        strong = panel.find("strong")
        if not strong:
            continue
        label = strong.get_text(strip=True)
        value_span = panel.find("span", class_="value")
        if value_span:
            # 多个链接（演员/类别）用「, 」拼接，否则取整段文本
            links = value_span.find_all("a")
            if links:
                value = ", ".join(a.get_text(strip=True) for a in links if a.get_text(strip=True))
            else:
                value = value_span.get_text(strip=True)
        else:
            value = panel.get_text(strip=True).replace(label, "").strip(": ：")

        if "番號" in label or "番号" in label or "ID" in label:
            info["code"] = value
        elif "時間" in label or "时间" in label or "分鐘" in label or "時長" in label:
            info["duration"] = value
        elif "日期" in label:
            info["release_date"] = value
        elif "導演" in label or "导演" in label:
            info["director"] = value
        elif "片商" in label or "制作" in label or "メーカー" in label or "製作商" in label:
            info["studio"] = value
        elif "發行" in label or "发行" in label:
            info["label"] = value
        elif "系列" in label:
            info["series"] = value
        elif "評分" in label or "评分" in label:
            m = re.search(r"([\d.]+)", value)
            if m:
                info["score"] = m.group(1)
            mc = re.search(r"([\d,]+)\s*人", value)
            if mc:
                info["score_count"] = mc.group(1).replace(",", "")

    # 评分兜底（部分页面评分在独立节点）
    if not info["score"]:
        score_tag = soup.select_one("span.score-stars")
        if score_tag and score_tag.parent:
            txt = score_tag.parent.get_text(" ", strip=True)
            m = re.search(r"([\d.]+)", txt)
            if m:
                info["score"] = m.group(1)
            mc = re.search(r"([\d,]+)\s*人", txt)
            if mc:
                info["score_count"] = mc.group(1).replace(",", "")

    # 演员（限定在「演員」面板内，过滤掉非演员链接）
    actors = []
    actor_block = None
    for panel in panels:
        strong = panel.find("strong")
        if strong and ("演員" in strong.get_text() or "演员" in strong.get_text()):
            actor_block = panel
            break
    actor_scope = actor_block or soup
    for a in actor_scope.select("a[href*='/actors/']"):
        name = a.get_text(strip=True)
        if name and name not in [x["name"] for x in actors]:
            actors.append({"name": name, "avatar": ""})
    info["actors"] = actors

    # 类别 / 标签
    tags = []
    for a in (soup.select("a[href*='/tags']") or soup.select("a[href*='/genres/']")):
        t = a.get_text(strip=True)
        if t and t not in tags:
            tags.append(t)
    info["tags"] = tags

    # 样品图（预览图）
    samples = []
    for a in soup.select("div.preview-images a.tile-item, .tile-images.preview-images a"):
        href = a.get("href") or ""
        img = a.find("img")
        thumb = (img.get("src") or img.get("data-src") or "") if img else ""
        full = href or thumb
        if full:
            samples.append(_abs(full))
    info["samples"] = samples[:30]

    # 磁力链
    magnets = []
    for item in soup.select("#magnets-content .item, div.magnet-links .item"):
        a = item.select_one("a[href^='magnet:']") or item.select_one("a[href]")
        if not a:
            continue
        link = a.get("href", "")
        if not link.startswith("magnet:"):
            continue
        name_tag = item.select_one(".name") or a
        name = name_tag.get_text(strip=True)
        meta_tag = item.select_one(".meta")
        size = meta_tag.get_text(strip=True) if meta_tag else ""
        date_tag = item.select_one(".date .time, .time")
        date = date_tag.get_text(strip=True) if date_tag else ""
        tag_texts = [t.get_text(strip=True) for t in item.select(".tags .tag")]
        magnets.append({
            "name": name,
            "link": link,
            "size": size,
            "date": date,
            "hd": any("高清" in t or "HD" in t.upper() for t in tag_texts),
            "subtitle": any("字幕" in t for t in tag_texts),
        })
    info["magnets"] = magnets

    # 简介（JavDB 多数无简介，保留兜底）
    desc_tag = soup.select_one("div.movie-summary p") or soup.select_one("div.summary p")
    if desc_tag:
        info["description"] = desc_tag.get_text(strip=True)

    return info


# ──────────────────────────────────────────────
# 连通诊断：判断「是否需要日本 IP / 是否被 CF 拦截」
# ──────────────────────────────────────────────
async def _probe_exit_ip(proxy: Optional[str]) -> dict:
    """通过相同代理查询出口 IP 所在国家，判断是否日本节点。"""
    try:
        async with httpx.AsyncClient(proxy=proxy or None, timeout=10, follow_redirects=True) as client:
            resp = await client.get("http://ip-api.com/json/?fields=status,country,countryCode,query,isp")
        if resp.status_code == 200:
            d = resp.json()
            if d.get("status") == "success":
                return {
                    "ip": d.get("query", ""),
                    "country": d.get("country", ""),
                    "country_code": d.get("countryCode", ""),
                    "isp": d.get("isp", ""),
                    "is_japan": d.get("countryCode", "") == "JP",
                }
        return {"error": f"ip-api HTTP {resp.status_code}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _inspect_page(html: str) -> dict:
    """
    在解析不到片源时，提取页面标题与可见文本片段，
    并判断返回的是什么页面（年龄门 / 登录墙 / 地区限制 / 验证）。
    """
    soup = BeautifulSoup(html or "", "html.parser")
    title = ""
    t = soup.select_one("title")
    if t:
        title = t.get_text(strip=True)
    # 去掉脚本/样式后的可见文本片段
    for tag in soup(["script", "style", "noscript"]):
        tag.extract()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:300]

    low = (title + " " + text).lower()
    kind = ""
    # 浏览器层错误（FlareSolverr 内 Chrome 报错，多为代理不兼容/连不上）
    if any(k in low for k in ("err_no_supported_proxies", "err_proxy_connection_failed",
                              "err_tunnel_connection_failed", "err_connection_",
                              "this site can’t be reached", "this site can't be reached",
                              "无法访问此网站")):
        kind = "proxy_error"
    elif any(k in low for k in ("over 18", "満18", "已滿18", "已满18", "age verification",
                                "enter site", "確認", "确认", "成人")):
        kind = "age_gate"      # 年龄确认门
    elif any(k in low for k in ("登入", "登录", "sign in", "log in", "登錄")):
        kind = "login_wall"    # 需要登录
    elif any(k in low for k in ("copyright restrictions", "版權限制", "版权限制",
                                "prohibited in the country", "禁止了你的", "禁止了您的",
                                "access to this site is prohibited")):
        kind = "geo_block"     # 因版权封禁所在国家（JavDB 封禁日本本土 IP！）
    elif any(k in low for k in ("not available in your", "地区", "region", "country",
                                "forbidden", "禁止", "无法访问")):
        kind = "region_block"  # 地区/风控拦截
    elif any(k in low for k in ("verify", "human", "turnstile", "captcha", "robot")):
        kind = "captcha"       # 人机验证
    return {"title": title, "snippet": text, "kind": kind}


async def diagnose(proxy: Optional[str] = None) -> dict:
    """
    JavDB 连通诊断。返回：
      - reachable:   是否成功拿到真正的 JavDB 页面
      - cf_blocked:  是否命中 Cloudflare 盾
      - http_status: 列表页 HTTP 状态码
      - item_count:  解析到的列表条目数
      - via:         取页方式（flaresolverr / httpx）
      - exit_ip:     出口 IP 信息（含是否日本节点）
      - page_title / page_snippet / page_kind: 0 条时回显实际页面，辅助定位
      - message:     人类可读结论
    """
    opts = _runtime_options()
    fs_raw = opts.get("flaresolverr_url") or ""
    # 地址留空：诊断时强制重新自动探测一次，并把探到的地址用于本次取页
    fs_auto = ""
    if not fs_raw:
        fs_auto = await _fs_discover(force=True)
        if fs_auto:
            opts = {**opts, "flaresolverr_url": fs_auto}
    fs_effective = fs_raw or fs_auto
    via = "flaresolverr" if fs_effective else "httpx"
    test_url = f"{JAVDB_BASE}/censored?sort_type=0"

    html, status, err = await _fetch_html(test_url, proxy, opts, retries=1)
    exit_ip = await _probe_exit_ip(proxy)

    # FlareSolverr 实际连通的地址（手填走智能适配结果；留空走自动探测结果）：用于回显
    fs_endpoint = (_fs_resolved(fs_raw) or _normalize_fs_url(fs_raw)) if fs_raw else fs_auto
    fs_cands = _fs_candidates(fs_raw) if fs_raw else ([fs_auto] if fs_auto else [])
    fs_adapted = bool(fs_raw and fs_endpoint and fs_endpoint != _normalize_fs_url(fs_raw))

    cf_blocked = (err == "cf_challenge") or _is_cf_challenge(html, status)
    items = _parse_list(html) if html and not cf_blocked else []
    reachable = bool(items)

    # 解析不到片源时，看看实际拿到的是什么页面
    page = {"title": "", "snippet": "", "kind": ""}
    if not reachable and html:
        page = _inspect_page(html)

    # JavDB 因版权封禁所在国家——重点：JavDB 封禁「日本本土 IP」，要换成非日本节点
    _cc = exit_ip.get("country_code", "")
    _country = exit_ip.get("country", "该国家")
    geo_msg = (f"JavDB 因版权限制封禁了你出口 IP 所在国家（{_country}{('/'+_cc) if _cc else ''}）的访问。"
               "特别注意：JavDB 会封禁『日本本土 IP』，所以日本节点反而不可用。"
               "请改用非日本节点（如美国/香港/台湾/新加坡等）的住宅 IP 后重测。")
    kind_msg = {
        "proxy_error": ("FlareSolverr 内置浏览器无法使用该代理（ERR_NO_SUPPORTED_PROXIES 等）。"
                        "若代理带账号密码，本版已自动拆分重试；仍失败请：① 关闭「FlareSolverr 复用代理」让其走自身出口，"
                        "或 ② 换成无认证的 http 代理。"),
        "geo_block": geo_msg,
        "age_gate": "返回的是「年龄确认门」页面：FlareSolverr 未带 over18 Cookie 或未通过年龄确认。",
        "login_wall": "返回的是「登录页」：该 IP 被要求登录，建议在设置中填入登录后的 Cookie。",
        "region_block": "返回的是「地区限制 / 风控拦截」页面：机房 IP 常被限制，建议换其它地区的住宅 IP。",
        "captcha": "返回的是「人机验证」页面：需要 FlareSolverr 正确过验证或更换 IP。",
    }

    if reachable:
        message = f"连接正常，解析到 {len(items)} 条最新片源。"
    elif err and err.startswith("flaresolverr"):
        # 直接回传 FlareSolverr 自身的报错，便于定位（如 cookies 格式 / 过盾失败 / 超时）
        message = f"FlareSolverr 报错：{err.split('flaresolverr:', 1)[-1].strip() or err}"
    elif cf_blocked:
        message = ("命中 Cloudflare 盾。建议配置 FlareSolverr，或在设置中填入浏览器导出的 "
                   "cf_clearance Cookie。")
    elif page.get("kind") in kind_msg:
        message = kind_msg[page["kind"]]
    elif err and err.startswith("HTTP"):
        message = f"JavDB 返回 {err}，可能为风控拦截或节点不可达。"
    elif err:
        message = f"请求失败：{err}。检查代理是否可访问 javdb.com。"
    else:
        message = "未解析到条目，页面结构可能变化或被拦截。"

    # 给出「IP 国家是否合适」/「代理是否异常」的提示
    ip_hint = ""
    if exit_ip.get("error"):
        # 连出口 IP 都查不到（经代理访问 ip-api 失败）→ 代理本身可能挂了
        ip_hint = (f"出口 IP 探测失败（{exit_ip.get('error')}）：很可能是代理本身不通/不稳。"
                   f"请先确认代理可正常访问外网，再重测 JavDB。")
    elif exit_ip.get("country_code"):
        if reachable:
            ip_hint = f"当前出口 IP 在 {exit_ip.get('country')}，可正常访问。"
        elif exit_ip.get("is_japan"):
            # JavDB 封禁日本本土 IP——这是日本节点不可用的根因
            ip_hint = "当前出口 IP 在日本，而 JavDB 因版权封禁日本本土 IP，请换非日本节点（美国/香港/台湾/新加坡等）。"
        elif page.get("kind") not in ("geo_block", "proxy_error"):
            ip_hint = (f"当前出口 IP 在 {exit_ip.get('country')}（{exit_ip.get('country_code')}），"
                       f"ISP 为 {exit_ip.get('isp', '')}。若被限制可尝试更换其它地区的住宅 IP。")

    # 上游 5xx（HTTP 500/502/504）多为代理或 FlareSolverr 到上游链路问题，单独点明
    if not reachable and not cf_blocked and (err or "").startswith("HTTP 5"):
        message = f"上游返回 {err}（多为代理/FlareSolverr 到 JavDB 的链路错误，非 CF 盾）。"

    # FlareSolverr 地址提示
    fs_hint = ""
    if fs_auto and fs_endpoint:
        # 地址留空、由程序自动探测到的：告知探到的地址，用户可不填、保持自动
        fs_hint = (f"已自动探测到 FlareSolverr：{fs_endpoint}（地址栏留空即自动使用，无需手填；"
                   f"也可把它固定填进设置）。")
    elif fs_adapted and fs_endpoint:
        fs_hint = f"已自动把 FlareSolverr 地址适配为 {fs_endpoint} 并连通（建议把设置里的地址直接改成它）。"
    elif not fs_effective and not reachable:
        # 留空且没探到：提示自动探测的前提
        fs_hint = ("未自动探测到可用的 FlareSolverr（地址栏留空时会自动找本机/同宿主机的 8191）。"
                   "若已自行部署，请确认它在运行；或直接在地址栏手填它的 URL。")

    return {
        "reachable": reachable,
        "cf_blocked": cf_blocked,
        "http_status": status,
        "item_count": len(items),
        "via": via,
        "fs_endpoint": fs_endpoint,          # 实际连通的 FlareSolverr 地址（智能适配结果）
        "fs_candidates": fs_cands,           # 本次自动尝试过的候选地址（按顺序）
        "fs_adapted": fs_adapted,            # 是否对用户填写的地址做了自动改写
        "error": err,
        "exit_ip": exit_ip,
        "page_title": page.get("title", ""),
        "page_snippet": page.get("snippet", ""),
        "page_kind": page.get("kind", ""),
        "message": (message + (" " + fs_hint if fs_hint else "")
                    + (" " + ip_hint if ip_hint else "")).strip(),
    }
