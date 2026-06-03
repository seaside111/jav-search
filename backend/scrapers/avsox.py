"""
AVSOX 刮削器（V1.3 新增数据源）
AVSOX 专注「无码」片源，与 JavBus 同源模板，复用 _javbus_base。
域名常变，默认 avsox.click，可通过环境/配置覆盖。
"""
import os
from typing import Optional
from . import _javbus_base as base

AVSOX_BASE = os.getenv("AVSOX_BASE", "https://avsox.click")
SOURCE = "AVSOX"


async def search_list(query: str, mode: str, proxy: Optional[str] = None, max_results: int = 300) -> list[dict]:
    if mode == "code":
        # AVSOX 搜索接口：/cn/search/关键词
        items = await _list(f"{AVSOX_BASE}/cn/search/{query}", proxy, max_results)
        return items
    # 演员/关键词统一走站内搜索
    return await _list(f"{AVSOX_BASE}/cn/search/{query}", proxy, max_results)


async def _list(search_base: str, proxy, max_results) -> list[dict]:
    def build(page):
        return search_base if page == 1 else f"{search_base}/{page}"
    return await base.fetch_list_only(AVSOX_BASE, SOURCE, build, proxy, max_results)


async def get_latest(proxy: Optional[str] = None, max_results: int = 40) -> list[dict]:
    def build(page):
        return f"{AVSOX_BASE}/cn" + ("" if page == 1 else f"/page/{page}")
    pages = max(3, min(max_results // 28 + 2, 8))
    return await base.fetch_list_only(AVSOX_BASE, SOURCE, build, proxy, max_results, max_pages=pages)


async def fetch_detail(url: str, proxy: Optional[str] = None) -> Optional[dict]:
    return await base.fetch_detail(url, AVSOX_BASE, SOURCE, proxy)
