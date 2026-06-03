"""
JavBus 刮削器（V1.3）
列表抓取 / 详情抓取分离；新增首页最新片源抓取。
"""
from typing import Optional
from . import _javbus_base as base

JAVBUS_BASE = "https://www.javbus.com"
SOURCE = "JavBus"


# ──────────────────────────────────────────────
# 列表级搜索（快，只抓列表页）
# ──────────────────────────────────────────────
async def search_list(query: str, mode: str, proxy: Optional[str] = None, max_results: int = 300) -> list[dict]:
    if mode == "code":
        # 番号优先走详情精确页，失败再退回关键词列表
        detail = await fetch_detail(f"{JAVBUS_BASE}/{query.upper()}", proxy)
        if detail:
            return [detail]
        return await _keyword_list(query, proxy, max_results)
    elif mode == "actor":
        # 先无码站内搜索，无结果再普通搜索
        items = await _list(f"{JAVBUS_BASE}/search/{query}/uncensored", proxy, max_results)
        if not items:
            items = await _keyword_list(query, proxy, max_results)
        return items
    else:
        return await _keyword_list(query, proxy, max_results)


async def _keyword_list(query: str, proxy, max_results) -> list[dict]:
    return await _list(f"{JAVBUS_BASE}/search/{query}", proxy, max_results)


async def _list(search_base: str, proxy, max_results) -> list[dict]:
    def build(page):
        return search_base if page == 1 else f"{search_base}/{page}"
    return await base.fetch_list_only(JAVBUS_BASE, SOURCE, build, proxy, max_results)


# ──────────────────────────────────────────────
# 首页最新片源
# ──────────────────────────────────────────────
async def get_latest(proxy: Optional[str] = None, max_results: int = 40) -> list[dict]:
    def build(page):
        return JAVBUS_BASE + ("/" if page == 1 else f"/page/{page}")
    pages = max(3, min(max_results // 28 + 2, 8))   # 每页约 30 条，按需翻页
    return await base.fetch_list_only(JAVBUS_BASE, SOURCE, build, proxy, max_results, max_pages=pages)


# ──────────────────────────────────────────────
# 详情
# ──────────────────────────────────────────────
async def fetch_detail(url: str, proxy: Optional[str] = None) -> Optional[dict]:
    return await base.fetch_detail(url, JAVBUS_BASE, SOURCE, proxy)
