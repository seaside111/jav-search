"""
JavBus 系刮削器通用基类
JavBus / AVSOX / AVMOO 三个站点共用同一套 HTML 模板（同源代码衍生），
只是 base_url、Referer 不同，因此抽出公共逻辑，由各站点模块传参复用。

V1.3 架构要点：列表抓取 与 详情抓取 彻底分离
  - fetch_list_only(): 只翻列表页，解析卡片级信息（番号/标题/封面/日期），
    速度快，可一次抓 300-500 条。
  - fetch_detail():    单条详情页抓取，按需调用（前台翻页时预取 / 打开详情时懒加载）。
"""
import re
import asyncio
from typing import Optional
import httpx
from bs4 import BeautifulSoup


def make_headers(base_url: str) -> dict:
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
        "Referer": base_url.rstrip("/") + "/",
    }


def _abs_url(src: str, base_url: str) -> str:
    if not src:
        return ""
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("http"):
        return src
    return base_url.rstrip("/") + src


# ──────────────────────────────────────────────
# 列表页解析（卡片级，无需进详情页）
# ──────────────────────────────────────────────
def parse_list(html: str, base_url: str, source: str) -> list[dict]:
    """
    解析列表页中的 movie-box 卡片，返回轻量条目列表。
    每个条目带 detail_loaded=False，标记详情尚未抓取。
    """
    soup = BeautifulSoup(html, "html.parser")
    items = []

    boxes = soup.select("a.movie-box") or soup.select("div.movie-box a") or soup.select("div.item a.movie-box")
    for box in boxes:
        href = box.get("href", "")
        if not href:
            continue
        url = _abs_url(href, base_url)

        # 封面 + 标题
        cover = ""
        title = ""
        img = box.select_one(".photo-frame img") or box.select_one("img")
        if img:
            cover = img.get("src") or img.get("data-src") or ""
            cover = _abs_url(cover, base_url)
            title = (img.get("title") or "").strip()

        # 番号 + 日期：photo-info 里的 <date> 标签
        code = ""
        release_date = ""
        dates = box.select(".photo-info date") or box.select("date")
        if dates:
            code = dates[0].get_text(strip=True)
            if len(dates) > 1:
                release_date = dates[1].get_text(strip=True)

        # 标题兜底：photo-info span 文本
        if not title:
            info_span = box.select_one(".photo-info span")
            if info_span:
                # 去掉 date 子节点后的纯文本
                for d in info_span.select("date"):
                    d.extract()
                title = info_span.get_text(" ", strip=True)

        # 番号兜底：从 URL 提取
        if not code:
            m = re.search(r"/([A-Za-z0-9]+-?\d+)/?$", url)
            if m:
                code = m.group(1).upper()

        items.append({
            "code": code,
            "title": title or code,
            "cover": cover,
            "url": url,
            "source": source,
            "release_date": release_date,
            "duration": "",
            "director": "",
            "studio": "",
            "label": "",
            "series": "",
            "score": "",
            "actors": [],
            "tags": [],
            "description": "",
            "detail_loaded": False,
        })

    return items


async def fetch_list_only(
    base_url: str,
    source: str,
    list_url_builder,
    proxy: Optional[str],
    max_results: int,
    max_pages: int = 20,
) -> list[dict]:
    """
    只翻列表页抓取卡片级条目，直到达到 max_results 或没有更多页。
    list_url_builder(page) -> 该页的 URL。
    """
    headers = make_headers(base_url)
    proxy_arg = proxy or None
    all_items: list[dict] = []
    seen = set()

    try:
        async with httpx.AsyncClient(
            headers=headers, proxy=proxy_arg, timeout=15, follow_redirects=True
        ) as client:
            for page in range(1, max_pages + 1):
                page_url = list_url_builder(page)
                try:
                    resp = await client.get(page_url)
                except Exception:
                    break
                if resp.status_code != 200:
                    break

                page_items = parse_list(resp.text, base_url, source)
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
                # 本页新条目过少，视为最后一页
                if added < 10:
                    break
    except Exception as e:
        print(f"[{source}] list error: {type(e).__name__}: {e!r}")

    return all_items[:max_results]


# ──────────────────────────────────────────────
# 详情页解析
# ──────────────────────────────────────────────
async def fetch_detail(url: str, base_url: str, source: str, proxy: Optional[str] = None) -> Optional[dict]:
    headers = make_headers(base_url)
    proxy_arg = proxy or None
    try:
        async with httpx.AsyncClient(
            headers=headers, proxy=proxy_arg, timeout=15, follow_redirects=True
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            return parse_detail(resp.text, url, base_url, source)
    except Exception as e:
        print(f"[{source}] detail error {url}: {type(e).__name__}: {e!r}")
        return None


def parse_detail(html: str, url: str, base_url: str, source: str) -> Optional[dict]:
    soup = BeautifulSoup(html, "html.parser")

    title_tag = soup.select_one("div.container h3")
    if not title_tag:
        return None
    title = title_tag.get_text(strip=True)

    cover = ""
    big_image = soup.select_one("a.bigImage img") or soup.select_one("div.bigImage img")
    if big_image:
        cover = big_image.get("src") or big_image.get("data-src") or ""
        cover = _abs_url(cover, base_url)

    info = {
        "title": title,
        "cover": cover,
        "url": url,
        "source": source,
        "code": "",
        "release_date": "",
        "duration": "",
        "director": "",
        "studio": "",
        "label": "",
        "series": "",
        "score": "",
        "actors": [],
        "tags": [],
        "description": "",
        "detail_loaded": True,
    }

    info_block = soup.select_one("div.col-md-3.info") or soup.select_one("div.info")
    if info_block:
        for p in info_block.find_all("p"):
            text = p.get_text(" ", strip=True)
            span = p.find("span", class_="header")
            if not span:
                continue
            label = span.get_text(strip=True)
            value_tag = p.find("span", class_=False) or p.find("a")
            value = value_tag.get_text(strip=True) if value_tag else ""

            if "識別碼" in label or "番號" in label or "番号" in label:
                info["code"] = value or text.split(":")[-1].strip()
            elif "發行日期" in label or "发行日期" in label or "発売日" in label:
                info["release_date"] = value or text.split(":")[-1].strip()
            elif "長度" in label or "时长" in label or "時長" in label:
                info["duration"] = value or text.split(":")[-1].strip()
            elif "導演" in label or "导演" in label:
                info["director"] = value
            elif "製作商" in label or "制作商" in label or "メーカー" in label:
                info["studio"] = value
            elif "發行商" in label or "发行商" in label:
                info["label"] = value
            elif "系列" in label:
                info["series"] = value

    if not info["code"]:
        m = re.search(r"/([A-Z]+-\d+|[A-Z]+\d+)$", url.upper())
        if m:
            info["code"] = m.group(1)

    # 演员 + 头像
    actor_imgs = {}
    star_boxes = soup.select("div.star-box-item") or soup.select("div.avatar-box")
    for box in star_boxes:
        name_tag = box.select_one("div.star-name a") or box.select_one("span.star-name a")
        img_tag = box.select_one("img")
        if name_tag and img_tag:
            name = name_tag.get_text(strip=True)
            img = img_tag.get("src") or img_tag.get("data-src") or ""
            actor_imgs[name] = _abs_url(img, base_url)

    actors = []
    for a in soup.select("div.star-name a") or soup.select("a[href*='/star/']"):
        name = a.get_text(strip=True)
        if name and name not in actors:
            actors.append(name)
    info["actors"] = [{"name": n, "avatar": actor_imgs.get(n, "")} for n in actors]

    tags = []
    for a in soup.select("span.genre a") or soup.select("div.genre a"):
        t = a.get_text(strip=True)
        if t:
            tags.append(t)
    info["tags"] = tags

    return info
