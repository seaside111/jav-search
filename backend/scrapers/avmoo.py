"""
AVMOO 刮削器。

2026 年 AVMOO 与 AVSOX 一同改为 Vue SPA 并共用同一套 javu JSON 后端，旧静态
HTML 网格失效。本文件仅提供域名与来源名，逻辑全在 _javu_base（见其注释）。
注意：改版后 AVMOO 与 AVSOX 接口返回的数据高度重合（同一后端），首页合并时
会按番号去重。域名常变，默认 avmoo.website，可用环境变量 AVMOO_BASE 覆盖。
"""
import os
from typing import Optional
from . import _javu_base as javu

AVMOO_BASE = os.getenv("AVMOO_BASE", "https://avmoo.website")
SOURCE = "AVMOO"

# 与 AVSOX 互为备份：主域名 avmoo.website 不可达时自动切到 avsox.click（同一 javu 后端）。
_AVSOX_BACKUP = os.getenv("AVSOX_BASE", "https://avsox.click")
BASES = [AVMOO_BASE, _AVSOX_BACKUP]


async def search_list(query: str, mode: str, proxy: Optional[str] = None,
                      max_results: int = 300) -> list[dict]:
    return await javu.search_list(BASES, SOURCE, query, mode, proxy, max_results)


async def get_latest(proxy: Optional[str] = None, max_results: int = 40) -> list[dict]:
    return await javu.get_latest(BASES, SOURCE, proxy, max_results)


async def fetch_detail(url: str, proxy: Optional[str] = None) -> Optional[dict]:
    return await javu.fetch_detail(BASES, SOURCE, url, proxy)
