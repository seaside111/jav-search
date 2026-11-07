"""DMM/FANZA metadata from GraphQL or the configured affiliate API."""
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from config_manager import load as load_config

API = "https://api.dmm.com/affiliate/v3/ItemList"
GRAPHQL_API = "https://api.video.dmm.co.jp/graphql"
SOURCE = "DMM/FANZA"
_JST = timezone(timedelta(hours=9))
_GRAPHQL_URL_PREFIX = "https://video.dmm.co.jp/av/content/?id="
_GRAPHQL_FIELDS = """
    id title description deliveryStartAt
    maker { name }
    actresses { name }
    packageImage { largeUrl }
    sampleImages { largeUrl }
    review { average count }
"""


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


async def _api(params: dict, proxy: Optional[str],
               credentials: Optional[tuple[str, str]] = None) -> list[dict]:
    api_id, affiliate_id = credentials if credentials is not None else _credentials()
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


def _graphql_code(content_id: str) -> str:
    # FANZA pads the serial to five digits and may prepend a numeric maker ID.
    value = re.sub(r"^(?:h_)?\d+(?=[a-z])", "", content_id.lower())
    match = re.fullmatch(r"([a-z][a-z0-9]*?)(\d{1,5})", value)
    if not match:
        return content_id.upper()
    prefix, number = match.groups()
    return f"{prefix.upper()}-{int(number):03d}"


def _delivery_time(raw: dict) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(raw.get("deliveryStartAt") or "").astimezone(_JST)
    except (TypeError, ValueError):
        return None


def _graphql_item(raw: dict) -> dict:
    cid = raw["id"]
    code = _graphql_code(cid)
    delivery = _delivery_time(raw)
    review = raw.get("review") or {}
    return {
        "code": code, "title": raw.get("title") or code,
        "cover": (raw.get("packageImage") or {}).get("largeUrl") or "",
        "url": f"{_GRAPHQL_URL_PREFIX}{cid}", "source": SOURCE,
        "release_date": delivery.date().isoformat() if delivery else "",
        "duration": "", "director": "",
        "studio": (raw.get("maker") or {}).get("name") or "",
        "label": "", "series": "",
        "score": (str(review["average"])
                  if review.get("count") and review.get("average") is not None else ""),
        "score_count": review.get("count") or 0,
        "actors": [{"name": name, "avatar": ""} for name in _names(raw.get("actresses"))],
        "tags": [], "magnets": [],
        "samples": [image["largeUrl"] for image in (raw.get("sampleImages") or [])
                    if image and image.get("largeUrl")],
        "description": raw.get("description") or "",
        "detail_loaded": True,
    }


async def _graphql(client: httpx.AsyncClient, limit: int, args: str) -> list[dict]:
    query = f"""{{ legacySearchPPV(limit: {limit}, floor: AV, {args}) {{
        result {{ contents {{ {_GRAPHQL_FIELDS} }} }}
    }} }}"""
    response = await client.post(GRAPHQL_API, json={"query": query})
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise ValueError("DMM GraphQL returned query errors")
    rows = payload["data"]["legacySearchPPV"]["result"]["contents"]
    return [row for row in rows if isinstance(row, dict)
            and re.fullmatch(r"[a-z0-9_]+", row.get("id") or "", re.I)]


async def _graphql_search(query: str, mode: str, proxy: Optional[str],
                          max_results: int) -> list[dict]:
    wanted = re.sub(r"[^a-z0-9]", "", query.lower())
    term = query
    if mode == "code":
        match = re.fullmatch(r"([a-z][a-z0-9]*?)[-_\s]?(\d+)\.?", query.lower())
        if not match:
            return []
        prefix, number = match.groups()
        term = f"{prefix}{int(number):05d}"
    async with httpx.AsyncClient(proxy=proxy or None, timeout=10) as client:
        rows = await _graphql(client, min(max(max_results, 5), 40),
                              f"sort: SALES_RANK_SCORE, queryWord: {json.dumps(term, ensure_ascii=False)}")
    items = [_graphql_item(row) for row in rows]
    if mode == "code":
        items = [item for item in items
                 if re.sub(r"[^a-z0-9]", "", item["code"].lower()) == wanted]
    return items[:max_results]


async def get_latest(proxy: Optional[str] = None, max_results: int = 40) -> list[dict]:
    # Unfiltered date sorting puts future releases first. Query actual release
    # days in JST, then fill from preceding days when today's list is short.
    if max_results <= 0:
        return []
    now = datetime.now(_JST)
    items = []
    async with httpx.AsyncClient(proxy=proxy or None, timeout=5) as client:
        for days_ago in range(7):
            date = now.date() - timedelta(days=days_ago)
            rows = await _graphql(client, min(max_results, 100),
                                  f'sort: DELIVERY_START_DATE, filter: {{ deliveryStartDate: "{date}" }}')
            for row in rows:
                delivery = _delivery_time(row)
                if delivery and delivery.date() == date and delivery <= now:
                    items.append(_graphql_item(row))
            if len(items) >= max_results:
                break
    return items[:max_results]


async def search_list(query: str, mode: str, proxy: Optional[str] = None,
                      max_results: int = 20) -> list[dict]:
    credentials = _credentials()
    if not all(credentials):
        return await _graphql_search(query, mode, proxy, max_results)
    if mode == "actor":
        return []
    rows = await _api({"keyword": query, "hits": min(max(1, max_results), 20)}, proxy, credentials)
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
    if (url or "").startswith(_GRAPHQL_URL_PREFIX):
        cid = url[len(_GRAPHQL_URL_PREFIX):].lower()
        if not re.fullmatch(r"[a-z0-9_]+", cid):
            return None
        async with httpx.AsyncClient(proxy=proxy or None, timeout=10) as client:
            rows = await _graphql(client, 5, f'sort: SALES_RANK_SCORE, queryWord: "{cid}"')
        return next((_graphql_item(row) for row in rows if row["id"].lower() == cid), None)
    if not (url or "").startswith("dmmapi://"):
        return None
    content_id = url.split("://", 1)[1]
    rows = await _api({"cid": content_id, "hits": 1}, proxy)
    return _item(rows[0]) if rows else None
