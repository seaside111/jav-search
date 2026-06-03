"""
AVMOO 刮削器（V1.3 新增数据源）
AVMOO 收录大量有码作品（含较老番号），与 JavBus 同源模板，复用 _javbus_base。
域名常变，默认 avmoo.website，可通过环境/配置覆盖。
"""
import os
from typing import Optional
from . import _javbus_base as base

AVMOO_BASE = os.getenv("AVMOO_BASE", "https://avmoo.website")
SOURCE = "AVMOO"


async def search_list(query: str, mode: str, proxy: Optional[str] = None, max_results: int = 300) -> list[dict]:
    # AVMOO 搜索接口：/cn/search/关键词
    return await _list(f"{AVMOO_BASE}/cn/search/{query}", proxy, max_results)


async def _list(search_base: str, proxy, max_results) -> list[dict]:
    def build(page):
        return search_base if page == 1 else f"{search_base}/{page}"
    return await base.fetch_list_only(AVMOO_BASE, SOURCE, build, proxy, max_results)


async def get_latest(proxy: Optional[str] = None, max_results: int = 40) -> list[dict]:
    def build(page):
        return f"{AVMOO_BASE}/cn" + ("" if page == 1 else f"/page/{page}")
    pages = max(3, min(max_results // 28 + 2, 8))
    return await base.fetch_list_only(AVMOO_BASE, SOURCE, build, proxy, max_results, max_pages=pages)


async def fetch_detail(url: str, proxy: Optional[str] = None) -> Optional[dict]:
    return await base.fetch_detail(url, AVMOO_BASE, SOURCE, proxy)
