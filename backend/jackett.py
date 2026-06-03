"""
Jackett 资源搜索模块
文档：https://github.com/Jackett/Jackett#api
"""
import asyncio
from typing import Optional
import httpx

# Jackett 返回的字段映射
SIZE_UNITS = ["B", "KB", "MB", "GB", "TB"]


def _fmt_size(size_bytes: int) -> str:
    if not size_bytes or size_bytes <= 0:
        return ""
    n = float(size_bytes)
    for unit in SIZE_UNITS:
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} PB"


def _infer_quality(title: str) -> str:
    """从标题推断画质标签"""
    t = title.upper()
    for q in ["4K", "2160P", "1080P", "1080I", "720P", "480P", "576P"]:
        if q in t:
            return q
    if "UHD" in t:
        return "4K"
    if "FHD" in t or "FULLHD" in t:
        return "1080P"
    if "HD" in t:
        return "720P"
    return ""


def _infer_codec(title: str) -> str:
    t = title.upper()
    for c in ["HEVC", "H.265", "H265", "X265", "AV1", "H.264", "H264", "X264", "AVC"]:
        if c.replace(".", "") in t.replace(".", ""):
            return c
    return ""


async def search_jackett(
    query: str,
    jackett_url: str,
    api_key: str,
    indexers: str = "all",
    proxy: Optional[str] = None,
    timeout: int = 20,
) -> list[dict]:
    """
    调用 Jackett API 搜索资源

    :param query:       搜索词，通常填番号
    :param jackett_url: Jackett 地址，如 http://192.168.1.100:9117
    :param api_key:     Jackett API Key（设置页面可查看）
    :param indexers:    索引器名称，多个用逗号分隔，或 'all'
    :param proxy:       可选代理
    :param timeout:     超时秒数
    :return:            资源列表
    """
    if not jackett_url or not api_key:
        return []

    url = jackett_url.rstrip("/") + f"/api/v2.0/indexers/{indexers}/results"
    params = {
        "apikey": api_key,
        "Query": query,
    }
    proxy_arg = proxy or None

    try:
        async with httpx.AsyncClient(
            proxy=proxy_arg, timeout=timeout, follow_redirects=True
        ) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                print(f"[Jackett] HTTP {resp.status_code}: {resp.text[:200]}")
                return []
            data = resp.json()
    except httpx.TimeoutException:
        print(f"[Jackett] 搜索超时 ({timeout}s): {query}")
        return []
    except Exception as e:
        print(f"[Jackett] 请求失败: {type(e).__name__}: {e!r}")
        return []

    results_raw = data.get("Results", [])
    results = []

    for item in results_raw:
        title = item.get("Title", "")
        magnet = item.get("MagnetUri", "")
        link = item.get("Link", "")           # 可能是 .torrent 直链
        info_url = item.get("Details", "")
        size_bytes = item.get("Size", 0)
        seeders = item.get("Seeders", 0)
        leechers = item.get("Peers", 0)
        pub_date = item.get("PublishDate", "")
        indexer = item.get("Tracker", item.get("TrackerId", ""))
        category = item.get("CategoryDesc", "")

        # 跳过没有下载地址的条目
        if not magnet and not link:
            continue

        # 格式化发布日期
        if pub_date and "T" in pub_date:
            pub_date = pub_date.split("T")[0]

        results.append({
            "title": title,
            "magnet": magnet,
            "link": link,
            "info_url": info_url,
            "size": _fmt_size(int(size_bytes)) if size_bytes else "",
            "size_bytes": int(size_bytes) if size_bytes else 0,
            "seeders": int(seeders) if seeders else 0,
            "leechers": int(leechers) if leechers else 0,
            "pub_date": pub_date[:10] if pub_date else "",
            "indexer": indexer,
            "category": category,
            "quality": _infer_quality(title),
            "codec": _infer_codec(title),
        })

    # 排序：有磁链 > 做种数从多到少 > 大小从大到小
    results.sort(key=lambda r: (
        0 if r["magnet"] else 1,
        -r["seeders"],
        -r["size_bytes"],
    ))

    return results
