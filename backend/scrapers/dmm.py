"""Optional DMM/FANZA official affiliate API scraper."""
import re
from typing import Optional

import httpx

from config_manager import load as load_config

API = "https://api.dmm.com/affiliate/v3/ItemList"
SOURCE = "DMM/FANZA"


def _credentials() -> tuple[str, str]:
    config = load_config()
    return ((config.get("dmm_api_id") or "").strip(),
            (config.get("dmm_affiliate_id") or "").strip())


def _code_from_content(content_id: str) -> str:
    value = re.sub(r'^h_\d+', '', (content_id or "").lower())
    match = re.match(r'([a-z]+)0*(\d+)$', value)
    return f"{match.group(1).upper()}-{match.group(2)}" if match else value.upper()


def _names(items) -> list[str]:
    return [str(item.get("name") or "").strip() for item in (items or [])
            if isinstance(item, dict) and item.get("name")]


def _item(raw: dict, query_code: str = "") -> dict:
    image = raw.get("imageURL") or {}
    sample = (raw.get("sampleImageURL") or {}).get("sample_l") or {}
    samples = sample.get("image") or []
    if isinstance(samples, str):
        samples = [samples]
    content_id = raw.get("content_id") or raw.get("product_id") or ""
    code = (query_code or _code_from_content(content_id)).upper()
    return {
        "code": code, "title": raw.get("title") or code,
        "cover": image.get("large") or image.get("list") or "",
        "url": f"dmmapi://{content_id}", "source": SOURCE,
        "release_date": (raw.get("date") or "")[:10],
        "duration": f"{raw.get('volume')}分钟" if raw.get("volume") else "",
        "director": "", "studio": ((raw.get("maker") or {}).get("name") or ""),
        "label": ((raw.get("label") or {}).get("name") or ""),
        "series": ((raw.get("series") or {}).get("name") or ""), "score": "",
        "actors": [{"name": name, "avatar": ""} for name in _names(raw.get("actress"))],
        "tags": _names(raw.get("genre")), "samples": [url for url in samples if url],
        "magnets": [], "description": raw.get("description") or "",
        "detail_loaded": True,
    }


async def _api(params: dict, proxy: Optional[str]) -> list[dict]:
    api_id, affiliate_id = _credentials()
    if not api_id or not affiliate_id:
        return []
    query = {"api_id": api_id, "affiliate_id": affiliate_id, "site": "FANZA",
             "service": "digital", "floor": "videoa", "output": "json",
             "hits": 10, **params}
    try:
        async with httpx.AsyncClient(proxy=proxy or None, timeout=12,
                                     follow_redirects=True) as client:
            response = await client.get(API, params=query)
        data = response.json() if response.status_code == 200 else {}
        return ((data.get("result") or {}).get("items") or [])
    except Exception:
        return []


async def search_list(query: str, mode: str, proxy: Optional[str] = None,
                      max_results: int = 20) -> list[dict]:
    if mode == "actor":
        return []
    rows = await _api({"keyword": query, "hits": min(max(1, max_results), 20)}, proxy)
    out = [_item(row) for row in rows]
    if mode == "code":
        wanted = re.sub(r'[^a-z0-9]', '', query.lower())
        exact = [item for item in out
                 if re.sub(r'[^a-z0-9]', '', item["code"].lower()) == wanted
                 or wanted in re.sub(r'[^a-z0-9]', '', item["title"].lower())]
        if exact:
            exact[0]["code"] = query.upper()
        return exact[:1]
    return out[:max_results]


async def fetch_detail(url: str, proxy: Optional[str] = None) -> Optional[dict]:
    if not (url or "").startswith("dmmapi://"):
        return None
    content_id = url.split("://", 1)[1]
    rows = await _api({"cid": content_id, "hits": 1}, proxy)
    return _item(rows[0]) if rows else None
