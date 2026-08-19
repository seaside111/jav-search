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


def _image_url(value: str) -> str:
    """Normalize an image/srcset value and reject page, icon and placeholder URLs."""
    raw = (value or "").strip().split(",", 1)[0].strip().split(" ", 1)[0]
    if not raw or raw.startswith(("data:", "javascript:")):
        return ""
    low = raw.lower().split("?", 1)[0]
    if not low.endswith((".jpg", ".jpeg", ".png", ".webp")):
        return ""
    if any(word in low for word in ("logo", "favicon", "loading", "noimage", "nowprinting")):
        return ""
    return _abs(raw)


def _parse(html: str, query: str = "", url: str = "") -> Optional[dict]:
    soup = BeautifulSoup(html or "", "html.parser")
    title_node = soup.select_one("h3") or soup.select_one("h1") or soup.select_one("title")
    title = title_node.get_text(" ", strip=True) if title_node else ""
    text = soup.get_text("\n", strip=True)
    code = (query or "").upper().strip()
    wanted = re.sub(r'[^a-z0-9]', '', query.lower()) if query else ""
    match = re.search(r'(?:品番|番號|番号)\s*[:：]?\s*([A-Z0-9]+(?:[-_][A-Z0-9]+)+)', text, re.I)
    if match:
        code = match.group(1).upper().replace("_", "-")
    # JAV321 can return a nearby fuzzy result. Validate the parsed 品番 itself;
    # a query substring elsewhere in the page is not sufficient evidence.
    if wanted and re.sub(r'[^a-z0-9]', '', code.lower()) != wanted:
        return None
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
    # Current JAV321 pages do not consistently include "sample" in image URLs.
    # The screenshots live in the wide information/media columns, including
    # lazy-loaded img/picture nodes and links to originals.
    sample_nodes = soup.select(
        "#sample-waterfall a, #sample-waterfall img, #sample-waterfall source, "
        ".sample-box a, .sample-box img, .sample-box source, "
        "#video_info a, #video_info img, #video_info source, "
        "div.col-md-9 a, div.col-md-9 img, div.col-md-9 source, "
        "div.col-xs-12.col-md-12 a, div.col-xs-12.col-md-12 img, "
        "div.col-xs-12.col-md-12 source, "
        "a[href*='sample'], img[src*='sample']"
    )
    for node in sample_nodes:
        # The wrapping link already contributed the original image; do not add
        # its nested thumbnail as a second sample.
        if node.name in {"img", "source"} and node.find_parent("a") is not None:
            continue
        values = [node.get("href"), node.get("data-original"), node.get("data-src"),
                  node.get("data-lazy-src"), node.get("srcset"), node.get("src")]
        if node.name == "a":
            child = node.find(["img", "source"])
            if child:
                values.extend([child.get("data-original"), child.get("data-src"),
                               child.get("data-lazy-src"), child.get("srcset"),
                               child.get("src")])
        full = next((_image_url(value) for value in values if _image_url(value)), "")
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
    async with httpx.AsyncClient(proxy=proxy or None, timeout=12,
                                 follow_redirects=True) as client:
        response = await client.post(BASE + "/search", data={"sn": query}, headers=_headers())
    if response.status_code != 200:
        raise RuntimeError(f"JAV321 HTTP {response.status_code}")
    item = _parse(response.text, query=query, url=str(response.url))
    return [item] if item else []


async def fetch_detail(url: str, proxy: Optional[str] = None) -> Optional[dict]:
    async with httpx.AsyncClient(proxy=proxy or None, timeout=12,
                                 follow_redirects=True) as client:
        response = await client.get(url, headers=_headers())
    if response.status_code != 200:
        raise RuntimeError(f"JAV321 HTTP {response.status_code}")
    return _parse(response.text, url=str(response.url))
