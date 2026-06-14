"""
AVSOX 刮削器（无码片源）。

2026 年 AVSOX 整站改为 Vue SPA，旧静态 HTML 网格失效，改走 javu JSON 接口，
具体协议见 _javu_base。本文件仅提供域名与来源名，逻辑全在 _javu_base。
域名常变，默认 avsox.click，可用环境变量 AVSOX_BASE 覆盖。
"""
import os
from typing import Optional
from . import _javu_base as javu

AVSOX_BASE = os.getenv("AVSOX_BASE", "https://avsox.click")
SOURCE = "AVSOX"

# AVSOX 与 AVMOO 共用同一套 javu 后端，故把 AVMOO 域名作为 AVSOX 的【备用域名】：
# 主域名 avsox.click 不可达时自动切到 avmoo.website 取数据（互为备份，见 avmoo.py）。
# 这样首页/搜索只需启用其中一个源，另一个域名纯作冗余兜底。
_AVMOO_BACKUP = os.getenv("AVMOO_BASE", "https://avmoo.website")
BASES = [AVSOX_BASE, _AVMOO_BACKUP]


async def search_list(query: str, mode: str, proxy: Optional[str] = None,
                      max_results: int = 300) -> list[dict]:
    return await javu.search_list(BASES, SOURCE, query, mode, proxy, max_results)


async def get_latest(proxy: Optional[str] = None, max_results: int = 40) -> list[dict]:
    return await javu.get_latest(BASES, SOURCE, proxy, max_results)


async def fetch_detail(url: str, proxy: Optional[str] = None) -> Optional[dict]:
    return await javu.fetch_detail(BASES, SOURCE, url, proxy)
