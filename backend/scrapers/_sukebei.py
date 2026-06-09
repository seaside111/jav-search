"""
sukebei.nyaa.si 最新 FC2 发现源（V1.4.4 新增）

背景：fc2ppvdb 的「新着列表」封顶在某个号（实测首页只到 4894253，且忽略 sort/翻页、
/articles 列表需登录），够不到市面最新号——但它的**详情页能按需现拉出新号**。
sukebei 是种子站，FC2 卖家一上架基本几小时内就有人发种，按 id 倒序即最新、**直连不过盾、最快**。

因此用 sukebei 做「最新番号发现源」：只取番号清单（+种子标题作临时标题），封面用 fourhoi
确定性 URL（零额外请求，404 由前端图片代理优雅降级），**详情仍由 fc2ppvdb/MissAV 在点开
时按需补全**（fc2ppvdb 详情对新号可用）。下载照旧走 Jackett/sukebei 按番号检索。

注意：本模块只负责「发现最新番号 + 临时标题/封面」，不碰磁力（保持与 fc2 卡片一致，
下载链路不变）。
"""
import re
import asyncio
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from . import _missav  # 复用 fourhoi 确定性封面 URL

SUKEBEI_BASE = "https://sukebei.nyaa.si"
SOURCE = "FC2"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,zh-CN;q=0.9,en;q=0.7",
}

# 种子标题里抽 FC2-PPV 番号：FC2-PPV-1234567 / FC2PPV1234567 / FC2 1234567
_NUM_RE = re.compile(r"FC2[-_\s]?PPV[-_\s]?(\d{5,7})", re.I)


def _proxy() -> Optional[str]:
    try:
        from config_manager import load as load_config
        return (load_config().get("proxy") or "").strip() or None
    except Exception:
        return None


def _clean_title(raw: str, num: str) -> str:
    """把种子文件名清成像样的临时标题：去掉番号前缀、扩展名、分辨率/发布站点等技术噪声。
    清不出有意义内容时返回空（交由番号兜底或 MissAV 补全）。"""
    t = raw or ""
    t = re.sub(r"\.(mp4|mkv|avi|wmv|ts|m2ts|iso)\b", " ", t, flags=re.I)   # 扩展名
    t = _NUM_RE.sub(" ", t)                                                # 番号本体
    t = re.sub(r"\bFC2[-_\s]?PPV\b", " ", t, flags=re.I)                   # 残留 FC2-PPV 字样
    # 删明显是技术标签/站点标记的方括号块（含分辨率/编码/站点名），保守起见只删命中关键词的
    t = re.sub(r"[\[\(（【][^\]\)）】]*(?:\d{3,4}p|ThZu|nyaa|sukebei|x264|x265|hevc|"
               r"aac|fhd\b|uhd|web-?dl|无修|無修正|uncensored|leak)[^\]\)）】]*[\]\)）】]",
               " ", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip(" -_.~|·")
    return t


def _parse(html: str, limit: int) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    items, seen = [], set()
    # sukebei 列表：每行的标题链接形如 <a href="/view/123" title="FC2-PPV-… …">…</a>
    # （每行还有个评论数 /view/ 链接，靠「标题里含番号 + 按番号去重」自然过滤掉）
    for a in soup.select("a[href^='/view/']"):
        title_attr = (a.get("title") or "").strip()
        text = a.get_text(" ", strip=True)
        cand = title_attr if _NUM_RE.search(title_attr) else text
        m = _NUM_RE.search(cand)
        if not m:
            continue
        num = m.group(1)
        if num in seen:
            continue
        seen.add(num)
        items.append(_card(num, cand))
        if len(items) >= limit:
            break
    return items


def _card(num: str, raw_title: str) -> dict:
    code = f"FC2-PPV-{num}"
    title = _clean_title(raw_title, num)
    return {
        "code": code,
        "title": title or code,
        "cover": _missav.cover_url(num),          # fourhoi 确定性封面，零额外请求
        "url": f"https://fc2ppvdb.com/articles/{num}",  # 点开→fc2ppvdb 详情(对新号按需可拉)
        "source": SOURCE,
        "release_date": "",
        "duration": "",
        "director": "",
        "studio": "",
        "label": "FC2",
        "series": "",
        "score": "",
        "score_count": "",
        "has_magnet": False,                      # 下载仍走 Jackett/sukebei 按番号检索
        "actors": [],
        "tags": [],
        "samples": [],
        "magnets": [],
        "description": "",
        "detail_loaded": False,
    }


async def fetch_fc2_latest(proxy: Optional[str] = None, limit: int = 60) -> list[dict]:
    """从 sukebei 取最新 FC2 番号（按 id 倒序＝最新在前）。直连、不过盾。失败返回 []。"""
    if proxy is None:
        proxy = _proxy()
    url = f"{SUKEBEI_BASE}/?q=FC2-PPV&s=id&o=desc"
    try:
        async with httpx.AsyncClient(headers=_HEADERS, proxy=proxy or None,
                                     timeout=15, follow_redirects=True) as client:
            resp = await client.get(url)
        if resp.status_code != 200 or not resp.text:
            print(f"[sukebei] HTTP {resp.status_code}")
            return []
        return _parse(resp.text, limit)
    except Exception as e:
        print(f"[sukebei] 取最新失败: {type(e).__name__}: {e}")
        return []
