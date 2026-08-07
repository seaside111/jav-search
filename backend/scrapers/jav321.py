"""Optional JAV321 scraper, primarily used for artwork backfill."""
import re
from typing import Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

BASE = "https://www.jav321.com"
SOURCE = "JAV321"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _headers() -> dict:
    return {"User-Agent": _UA, "Referer": BASE + "/", "Accept-Language": "ja,en;q=0.8"}


def _abs(url: str) -> str:
    return urljoin(BASE + "/", (url or "").strip()) if url else ""


def _parse(html: str, query: str = "", url: str = "") -> Optional[dict]:
    soup = BeautifulSoup(html or "", "html.parser")
    title_node = soup.select_one("h3") or soup.select_one("h1") or soup.select_one("title")
    title = title_node.get_text(" ", strip=True) if title_node else ""
    text = soup.get_text("\n", strip=True)
    code = (query or "").upper().strip()
    if query:
        wanted = re.sub(r'[^a-z0-9]', '', query.lower())
        if wanted not in re.sub(r'[^a-z0-9]', '', text.lower()):
            return None
    match = re.search(r'(?:品番|番號|番号)\s*[:：]?\s*([A-Z0-9]+(?:[-_][A-Z0-9]+)+)', text, re.I)
    if match:
        code = match.group(1).upper().replace("_", "-")
    if not code and not title:
        return None

    cover = ""
    for img in soup.select("div.col-md-3 img, img.img-responsive, meta[property='og:image']"):
        candidate = (img.get("content") or img.get("data-original")
                     or img.get("data-src") or img.get("src"))
        if candidate:
            cover = _abs(candidate)
            break
    samples = []
    for node in soup.select("#sample-waterfall a, .sample-box a, a[href*='sample'], img[src*='sample']"):
        candidate = (node.get("href") or node.get("data-original")
                     or node.get("data-src") or node.get("src"))
        full = _abs(candidate)
        if full and full != cover and full not in samples:
            samples.append(full)

    actors = []
    for actor in soup.select("a[href*='/star/'], a[href*='/actor/']"):
        name = actor.get_text(" ", strip=True)
        if name and name not in [x["name"] for x in actors]:
            actors.append({"name": name, "avatar": ""})
    return {
        "code": code, "title": title or code, "cover": cover,
        "url": url or BASE + "/search", "source": SOURCE,
        "release_date": "", "duration": "", "director": "", "studio": "",
        "label": "", "series": "", "score": "", "actors": actors,
        "tags": [], "samples": samples, "magnets": [], "description": "",
        "detail_loaded": True,
    }


async def search_list(query: str, mode: str, proxy: Optional[str] = None,
                      max_results: int = 20) -> list[dict]:
    if mode == "actor":
        return []
    try:
        async with httpx.AsyncClient(proxy=proxy or None, timeout=12,
                                     follow_redirects=True) as client:
            response = await client.post(BASE + "/search", data={"sn": query}, headers=_headers())
        item = _parse(response.text, query=query, url=str(response.url)) if response.status_code == 200 else None
        return [item] if item else []
    except Exception:
        return []


async def fetch_detail(url: str, proxy: Optional[str] = None) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(proxy=proxy or None, timeout=12,
                                     follow_redirects=True) as client:
            response = await client.get(url, headers=_headers())
        return _parse(response.text, url=str(response.url)) if response.status_code == 200 else None
    except Exception:
        return None
