"""
刮削器聚合器（V1.3）— 列表抓取 / 详情抓取分离架构

设计：
  - search()      只抓「列表页」，快速返回卡片级条目（番号/标题/封面/日期/评分），
                  可一次拿 300-500 条；详情留待按需抓取。
  - enrich()      按需抓取若干条目的详情（演员/标签/导演/简介），供前台翻页时
                  预取当前页+下一页，或打开详情弹窗时懒加载。
  - get_latest()  抓取数据源首页最新片源，用于未搜索时的首页展示。
"""
import asyncio
import re
from typing import Optional

from . import javbus, javdb, avsox, avmoo

SEARCH_MODE_CODE = "code"
SEARCH_MODE_ACTOR = "actor"
SEARCH_MODE_KEYWORD = "keyword"

# 数据源注册表：name -> module
SOURCE_MODULES = {
    "javbus": javbus,
    "javdb": javdb,
    "avsox": avsox,
    "avmoo": avmoo,
}

# 合并时来源优先级（数字小者优先，作为主条目保留封面/标题）
_SOURCE_PRIORITY = {"javbus": 0, "javdb": 1, "avmoo": 2, "avsox": 3}


def _normalize_code(code: str) -> str:
    return (code or "").upper().strip().replace(" ", "-")


def _merge_lists(lists_by_source: list[tuple[str, list[dict]]]) -> list[dict]:
    """
    多来源列表按番号合并去重。保留优先级高的来源为主条目，
    其它来源补充缺失字段，并记录命中的来源集合。
    无番号的条目（少数）按 URL 独立保留。
    """
    merged: dict[str, dict] = {}
    order: list[str] = []

    # 先按来源优先级排序，保证主条目来自高优先级源
    lists_by_source = sorted(lists_by_source, key=lambda x: _SOURCE_PRIORITY.get(x[0], 99))

    for _src, items in lists_by_source:
        for item in items:
            key = _normalize_code(item.get("code", "")) or ("url:" + item.get("url", ""))
            if not key:
                continue
            if key not in merged:
                merged[key] = dict(item)
                merged[key]["sources"] = [item.get("source", "")]
                order.append(key)
            else:
                ex = merged[key]
                if item.get("source") and item["source"] not in ex.get("sources", []):
                    ex.setdefault("sources", []).append(item["source"])
                # 补充缺失字段
                for f in ("cover", "title", "release_date", "score",
                          "duration", "director", "studio", "label", "series"):
                    if not ex.get(f) and item.get(f):
                        ex[f] = item[f]

    return [merged[k] for k in order]


async def search(
    query: str,
    mode: str = SEARCH_MODE_KEYWORD,
    proxy: Optional[str] = None,
    sources: list[str] = None,
    max_results: int = 300,
) -> list[dict]:
    """统一列表搜索入口（仅抓列表页，速度快）"""
    if not sources:
        sources = ["javbus", "javdb"]

    tasks, labels = [], []
    for src in sources:
        mod = SOURCE_MODULES.get(src)
        if not mod:
            continue
        tasks.append(mod.search_list(query, mode, proxy, max_results))
        labels.append(src)

    results = await asyncio.gather(*tasks, return_exceptions=True)

    collected = []
    for label, res in zip(labels, results):
        if isinstance(res, list):
            collected.append((label, res))
        else:
            print(f"[search] source {label} failed: {res!r}")

    if not collected:
        return []
    if len(collected) == 1:
        out = collected[0][1]
        for it in out:
            it.setdefault("sources", [it.get("source", "")])
        return out[:max_results]

    return _merge_lists(collected)[:max_results]


async def enrich(items: list[dict], proxy: Optional[str] = None,
                 concurrency: int = 6, per_timeout: float = 9.0) -> list[dict]:
    """
    按需抓取详情。items 为待补全的条目（需含 url + source），
    返回与输入等长、顺序一致的详情列表（失败/超时项为 None）。

    单条超时（默认 9s）确保每条都能在限定时间内返回，
    避免个别慢/挂起的详情请求拖垮整批、导致前端长时间等待。
    """
    sem = asyncio.Semaphore(concurrency)

    async def one(item):
        url = item.get("url", "")
        source = (item.get("source", "") or "").lower()
        mod = SOURCE_MODULES.get(source)
        if not mod or not url:
            return None
        async with sem:
            await asyncio.sleep(0.05)
            try:
                return await asyncio.wait_for(mod.fetch_detail(url, proxy), timeout=per_timeout)
            except asyncio.TimeoutError:
                print(f"[enrich] {source} {url} timeout")
                return None
            except Exception as e:
                print(f"[enrich] {source} {url} failed: {e!r}")
                return None

    return await asyncio.gather(*[one(it) for it in items])


async def get_latest(
    proxy: Optional[str] = None,
    sources: list[str] = None,
    per_source: int = 40,
    limits: dict = None,
) -> list[dict]:
    """
    抓取各数据源首页最新片源，合并返回（用于首页展示）。
    limits: 各来源单独的条数上限（如 {'javbus':100,'avsox':40}）；
            未指定的来源回退到 per_source。
    """
    if not sources:
        sources = ["javbus", "javdb"]
    limits = limits or {}

    tasks, labels = [], []
    for src in sources:
        mod = SOURCE_MODULES.get(src)
        if not mod or not hasattr(mod, "get_latest"):
            continue
        lim = int(limits.get(src, per_source))
        tasks.append(mod.get_latest(proxy, lim))
        labels.append(src)

    results = await asyncio.gather(*tasks, return_exceptions=True)
    collected = []
    for label, res in zip(labels, results):
        if isinstance(res, list):
            collected.append((label, res))
        else:
            print(f"[latest] source {label} failed: {res!r}")

    if not collected:
        return []
    if len(collected) == 1:
        out = collected[0][1]
        for it in out:
            it.setdefault("sources", [it.get("source", "")])
        return out
    return _merge_lists(collected)


def detect_search_mode(query: str) -> str:
    """自动检测搜索模式"""
    query = query.strip()
    if re.match(r'^[A-Za-z]{2,8}[-\s]?\d{2,6}$', query):
        return SEARCH_MODE_CODE
    if re.search(r'[぀-ヿ一-鿿]', query) and len(query) <= 10:
        return SEARCH_MODE_ACTOR
    return SEARCH_MODE_KEYWORD
