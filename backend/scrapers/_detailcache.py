"""
详情缓存（V1.4.5）：按条目 url 缓存 enrich 抓到的详情（演员/标签/简介/样品图/磁力…）。

内存 + 磁盘两级（与封面缓存同理），供 enrich() 统一使用，于是三处共享同一份缓存：
  - 前台浏览/翻页时后台预抓的当前页详情；
  - 用户点开单条详情按需补抓；
  - 下载完成后刮削时按 url 回源补全。
即「后台已抓过的内容，刮削直接拿；没抓过才回源」。url 天然是每个条目的唯一编号。

详情数据相对静态，默认缓存 7 天（DETAIL_CACHE_TTL_DAYS 覆盖）。失败结果不缓存以便重试。
"""
import os
import json
import time
import hashlib
from pathlib import Path
from typing import Optional

_DIR = Path(os.getenv("CONFIG_DIR", "/config")) / "detailcache"
_TTL = float(os.getenv("DETAIL_CACHE_TTL_DAYS", "7")) * 86400
_mem: dict[str, tuple[float, dict]] = {}
_MEM_MAX = 600


def _key(url: str) -> str:
    return hashlib.sha256((url or "").encode("utf-8")).hexdigest()


def get(url: str) -> Optional[dict]:
    if not url:
        return None
    now = time.time()
    hit = _mem.get(url)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    f = _DIR / f"{_key(url)}.json"
    try:
        if f.exists() and now - f.stat().st_mtime < _TTL:
            data = json.loads(f.read_text(encoding="utf-8"))
            _mem[url] = (now, data)
            return data
    except Exception:
        pass
    return None


def put(url: str, data: dict) -> None:
    if not url or not isinstance(data, dict):
        return
    if len(_mem) >= _MEM_MAX:
        _mem.pop(next(iter(_mem)), None)
    _mem[url] = (time.time(), data)
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
        f = _DIR / f"{_key(url)}.json"
        tmp = f.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(f)
    except Exception:
        pass
