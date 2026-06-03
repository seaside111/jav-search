"""
JavDB 刮削器（V1.3）
列表抓取 / 详情抓取分离；新增首页最新片源抓取。
列表页已含番号/标题/封面/评分/日期，足够渲染卡片，详情按需抓取。
"""
import re
import asyncio
from typing import Optional
import httpx
from bs4 import BeautifulSoup

JAVDB_BASE = "https://javdb.com"
SOURCE = "JavDB"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://javdb.com/",
}


def _abs(href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    return JAVDB_BASE + href


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
        score_tag = item.select_one("div.score .value") or item.select_one("div.score")
        if score_tag:
            m = re.search(r"([\d.]+)", score_tag.get_text(strip=True))
            if m:
                score = m.group(1)

        release_date = ""
        meta = item.select_one("div.meta")
        if meta:
            release_date = meta.get_text(strip=True)

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
            "actors": [],
            "tags": [],
            "description": "",
            "detail_loaded": False,
        })
    return items


async def _fetch_list_only(list_url_builder, proxy, max_results, max_pages=20) -> list[dict]:
    proxy_arg = proxy or None
    all_items, seen = [], set()
    try:
        async with httpx.AsyncClient(
            headers=HEADERS, proxy=proxy_arg, timeout=15, follow_redirects=True
        ) as client:
            for page in range(1, max_pages + 1):
                page_url = list_url_builder(page)
                try:
                    resp = await client.get(page_url)
                except Exception:
                    break
                if resp.status_code != 200:
                    break
                page_items = _parse_list(resp.text)
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
    except Exception as e:
        print(f"[JavDB] list error: {type(e).__name__}: {e!r}")
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
    proxy_arg = proxy or None
    try:
        async with httpx.AsyncClient(
            headers=HEADERS, proxy=proxy_arg, timeout=15, follow_redirects=True
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            return _parse_detail(resp.text, url)
    except Exception as e:
        print(f"[JavDB] detail error {url}: {type(e).__name__}: {e!r}")
        return None


def _parse_detail(html: str, url: str) -> Optional[dict]:
    soup = BeautifulSoup(html, "html.parser")

    title_tag = soup.select_one("h2.title strong") or soup.select_one("title")
    if not title_tag:
        return None
    title = title_tag.get_text(strip=True)

    cover = ""
    cover_tag = soup.select_one("div.column-video-cover img")
    if cover_tag:
        cover = cover_tag.get("src") or cover_tag.get("data-src") or ""
        if cover.startswith("//"):
            cover = "https:" + cover

    info = {
        "title": title, "cover": cover, "url": url, "source": SOURCE,
        "code": "", "release_date": "", "duration": "", "director": "",
        "studio": "", "label": "", "series": "", "score": "",
        "actors": [], "tags": [], "description": "", "detail_loaded": True,
    }

    panels = soup.select("nav.panel.movie-panel-info .panel-block")
    for panel in panels:
        strong = panel.find("strong")
        if not strong:
            continue
        label = strong.get_text(strip=True)
        value_span = panel.find("span", class_="value") or panel.find("a")
        if value_span:
            value = value_span.get_text(strip=True)
        else:
            value = panel.get_text(strip=True).replace(label, "").strip(": ：")

        if "番號" in label or "番号" in label or "ID" in label:
            info["code"] = value
        elif "時間" in label or "时间" in label or "分鐘" in label:
            info["duration"] = value
        elif "日期" in label:
            info["release_date"] = value
        elif "導演" in label or "导演" in label:
            info["director"] = value
        elif "片商" in label or "制作" in label or "メーカー" in label:
            info["studio"] = value
        elif "系列" in label:
            info["series"] = value

    score_tag = soup.select_one("span.score-stars") or soup.select_one("div.score strong")
    if score_tag:
        m = re.search(r"([\d.]+)", score_tag.get_text(strip=True))
        if m:
            info["score"] = m.group(1)

    actors = []
    for a in soup.select("div.panel-block a[href*='/actors/']"):
        name = a.get_text(strip=True)
        if name:
            actors.append({"name": name, "avatar": ""})
    info["actors"] = actors

    tags = []
    for a in (soup.select("div.panel-block a[href*='/tags/']") or
              soup.select("div.panel-block a[href*='/genres/']")):
        t = a.get_text(strip=True)
        if t:
            tags.append(t)
    info["tags"] = tags

    desc_tag = soup.select_one("div.movie-summary p") or soup.select_one("div.summary p")
    if desc_tag:
        info["description"] = desc_tag.get_text(strip=True)

    return info
