"""
Jackett 资源搜索模块
文档：https://github.com/Jackett/Jackett#api
"""
import asyncio
from typing import Optional
import httpx

# Jackett 返回的字段映射
SIZE_UNITS = ["B", "KB", "MB", "GB", "TB"]


def normalize_url(jackett_url: str) -> str:
    """规整 Jackett 地址：缺协议头时补 http://（用户常只填 host:9117，
    httpx 对无协议的地址会直接报错 → 表现为「连接不稳定/测不通」）。去掉尾部斜杠。"""
    u = (jackett_url or "").strip().rstrip("/")
    if not u:
        return ""
    if not u.lower().startswith(("http://", "https://")):
        u = "http://" + u
    return u


async def check_status(jackett_url: str, api_key: str, timeout: int = 15) -> dict:
    """
    轻量连通性检测。
    不再用「对全部索引器发起真实搜索」来测连通——那会触发每个索引器的实时抓取，
    慢索引器一拖就超时，表现为「设置里时好时坏」。改用 Jackett 自身的索引器列表端点
    /api/v2.0/indexers，只校验「服务可达 + API Key 有效」，快且稳定。
    返回 {configured, online, message}。
    """
    base = normalize_url(jackett_url)
    if not base or not (api_key or "").strip():
        return {"configured": False, "online": False, "message": "未配置"}
    url = f"{base}/api/v2.0/indexers?apikey={api_key}&configured=true"
    last_err = ""
    # 偶发抖动重试一次，进一步降低「时好时坏」
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                resp = await client.get(url)
            if resp.status_code == 200:
                try:
                    n = len(resp.json())
                    return {"configured": True, "online": True,
                            "message": f"连接正常 · 已配置 {n} 个索引器"}
                except Exception:
                    return {"configured": True, "online": True, "message": "连接正常"}
            if resp.status_code in (401, 403):
                return {"configured": True, "online": False,
                        "message": "API Key 无效或无权限（请核对设置里的 API Key）"}
            last_err = f"HTTP {resp.status_code}"
        except httpx.TimeoutException:
            last_err = f"连接超时（{timeout}s）"
        except httpx.ConnectError as e:
            last_err = f"无法连接（请核对地址/协议 http(s) 与端口）：{e}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        if attempt == 0:
            await asyncio.sleep(0.8)
    return {"configured": True, "online": False, "message": last_err or "连接失败"}


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

    url = normalize_url(jackett_url) + f"/api/v2.0/indexers/{indexers}/results"
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
