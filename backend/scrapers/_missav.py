"""
MissAV 元数据补全（V1.4.3）—— 主要用于给 FC2 补封面/标题/女优/标签。

背景：fc2ppvdb 对**已下架**的 FC2-PPV 常只剩番号骨架（无封面/标题）。而 MissAV 对 FC2
覆盖很好，连下架条目也保留了**完整标题 + 高清封面**（封面走 fourhoi.com CDN）。

设计要点：
  - 封面 URL 是**确定性**的：https://fourhoi.com/fc2-ppv-{num}/cover-n.jpg，
    列表卡可零额外请求地直接猜出（404 由前端图片代理优雅降级为无封面）。
  - 详情阶段再抓 MissAV 页面拿**真实标题**（og:title）与女优/标签（无法靠猜）。
  - MissAV 对不存在的番号返回通用站名标题（"MissAV | …"），用「og:title 是否含番号」校验。
  - 部分镜像有 Cloudflare（.ai 直连 403），默认用可直连的镜像（.ws）；失败再退到 FC2 的
    FlareSolverr（与 fc2 模块共用配置）。
"""
import re
from typing import Optional
import httpx

from ._fsgate import flaresolverr_request as _fs_request, discover_auto as _fs_discover

# 可直连的镜像优先；域名常变，可由配置 fc2_missav_base 覆盖（逗号分隔）
DEFAULT_BASES = ["https://missav.ws", "https://missav123.com"]
FOURHOI_CDN = "https://fourhoi.com"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,zh-CN;q=0.9,en;q=0.7",
}


def cover_url(num: str) -> str:
    """FC2 封面的确定性 URL（无需抓页面即可得到）。"""
    return f"{FOURHOI_CDN}/fc2-ppv-{num}/cover-n.jpg" if num else ""


def _runtime():
    """读取 MissAV 运行配置：是否启用、镜像列表，以及复用的 FlareSolverr。"""
    enabled, bases = True, list(DEFAULT_BASES)
    fs, use_proxy, cookie = "", True, ""
    try:
        from config_manager import load as load_config
        cfg = load_config()
        enabled = cfg.get("fc2_missav_enabled", True)
        custom = (cfg.get("fc2_missav_base") or "").strip()
        if custom:
            bases = [b.strip().rstrip("/") for b in custom.split(",") if b.strip()]
        fs = (cfg.get("fc2_flaresolverr_url") or cfg.get("javdb_flaresolverr_url") or "").strip()
        use_proxy = cfg.get("fc2_flaresolverr_use_proxy",
                            cfg.get("javdb_flaresolverr_use_proxy", True))
    except Exception:
        pass
    return {"enabled": enabled, "bases": bases,
            "flaresolverr_url": fs, "flaresolverr_use_proxy": use_proxy}


def _og(html: str, prop: str) -> str:
    m = re.search(rf'<meta[^>]+property=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']*)["\']',
                  html, re.I)
    if not m:
        m = re.search(rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']{re.escape(prop)}["\']',
                      html, re.I)
    return (m.group(1).strip() if m else "")


def _looks_like_cf(html: str, status: int) -> bool:
    if status in (403, 429, 503):
        return True
    head = (html or "")[:3000].lower()
    return ("just a moment" in head or "/cdn-cgi/challenge-platform/" in head
            or "cf-chl-bypass" in head)


async def _fetch_via_flaresolverr(url: str, fs_url: str, proxy: Optional[str]) -> str:
    """统一走 _fsgate 的智能适配 + 全局串行；MissAV 仅需 HTML，丢弃 status/err。"""
    html, _status, _err = await _fs_request(url, fs_url, proxy, None,
                                            max_timeout=40000, read_timeout=70.0)
    return html or ""


async def _get_html(num: str, proxy: Optional[str], opts: dict) -> str:
    """按镜像依次直连抓 MissAV 的 FC2 页面；命中 CF 时退到 FlareSolverr。"""
    paths = [f"/ja/FC2-PPV-{num}", f"/FC2-PPV-{num}"]
    for base in opts["bases"]:
        for path in paths:
            url = base.rstrip("/") + path
            try:
                async with httpx.AsyncClient(headers=_HEADERS, proxy=proxy or None,
                                             timeout=12, follow_redirects=True) as client:
                    resp = await client.get(url)
                if resp.status_code == 200 and not _looks_like_cf(resp.text, 200):
                    if str(num) in resp.text:
                        return resp.text
                elif _looks_like_cf(resp.text, resp.status_code):
                    fs_url = opts.get("flaresolverr_url") or await _fs_discover()  # 留空则自动探测
                    if not fs_url:
                        continue
                    fs_proxy = proxy if opts.get("flaresolverr_use_proxy", True) else None
                    html = await _fetch_via_flaresolverr(url, fs_url, fs_proxy)
                    if html and str(num) in html:
                        return html
            except Exception:
                continue
    return ""


def _clean_title(title: str, num: str) -> str:
    """去掉站名后缀与开头的番号前缀，留下纯标题。"""
    t = title
    for suf in (" - MissAV", "| MissAV", " - missav", "｜MissAV"):
        idx = t.find(suf)
        if idx > 0:
            t = t[:idx]
    # 去掉开头的 FC2-PPV-num / FC2-num 前缀
    t = re.sub(rf"^\s*FC2[-\s]?(PPV[-\s]?)?{re.escape(str(num))}\s*", "", t, flags=re.I)
    return t.strip()


async def fetch_fc2(num: str, proxy: Optional[str] = None) -> Optional[dict]:
    """
    抓 MissAV 补全 FC2 元数据。返回 {title, cover, actors, tags} 或 None（不存在/未启用）。
    封面优先用页面 og:image，没有则回退确定性 fourhoi URL。
    """
    if not num:
        return None
    opts = _runtime()
    if not opts.get("enabled"):
        return None
    html = await _get_html(num, proxy, opts)
    if not html:
        return None

    title_raw = _og(html, "og:title")
    # 校验：真实条目标题含番号；通用站名（MissAV | …）视为「无此条目」
    if not title_raw or str(num) not in title_raw:
        return None

    cover = _og(html, "og:image") or cover_url(num)
    # 注：不从 MissAV 抽取女优/体裁——其 /actresses//genres/ 链接里混有「女優ランキング」
    # 等导航/排行项，噪声大且难以可靠区分。MissAV 对 FC2 的高价值、可靠字段是「标题 + 封面」。

    # 样品图：MissAV 无传统图廊，但播放器进度条有一组逐帧截图（storyboard），
    # 形如 https://{cdn}/{uuid}/seek/_N.jpg（CDN 主机随视频变，如 nineyu.com）。
    # 这些就是可用的「样品图」。注意：这些 CDN 有防盗链，需带 Referer: missav 域名才能取
    #（由后端图片代理 /api/img 统一处理）。
    raw = html.replace("\\/", "/")
    samples = []
    for u in re.findall(r'https://[a-z0-9.-]+/[a-z0-9-]+/seek/_\d+\.jpg', raw):
        if u not in samples:
            samples.append(u)
    samples.sort(key=lambda u: int(re.search(r'_(\d+)\.jpg', u).group(1)))

    # 预览视频片段（确定性 URL，前端可选用作动态预览）
    preview_video = f"{FOURHOI_CDN}/fc2-ppv-{num}/preview.mp4"

    return {
        "title": _clean_title(title_raw, num),
        "cover": cover,
        "samples": samples[:40],
        "preview_video": preview_video,
        "source_url": f"{opts['bases'][0].rstrip('/')}/ja/FC2-PPV-{num}",
    }
