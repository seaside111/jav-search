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

from . import javbus, javdb, avsox, avmoo, fc2

SEARCH_MODE_CODE = "code"
SEARCH_MODE_ACTOR = "actor"
SEARCH_MODE_KEYWORD = "keyword"

# 数据源注册表：name -> module
SOURCE_MODULES = {
    "javbus": javbus,
    "javdb": javdb,
    "avsox": avsox,
    "avmoo": avmoo,
    "fc2": fc2,        # V1.4.3：FC2-PPV 专用源（fc2ppvdb.com，无码/素人）
}

# 合并时来源优先级（数字小者优先，作为主条目保留封面/标题）
# FC2 番号体系独立、不与其它源重叠，优先级随意，置末即可
_SOURCE_PRIORITY = {"javbus": 0, "javdb": 1, "avmoo": 2, "avsox": 3, "fc2": 4}


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
                # 记录各来源各自的详情页 URL（供合并卡按需补抓非主来源的样品图/磁力）
                merged[key]["source_urls"] = {item.get("source", ""): item.get("url", "")}
                order.append(key)
            else:
                ex = merged[key]
                if item.get("source") and item["source"] not in ex.get("sources", []):
                    ex.setdefault("sources", []).append(item["source"])
                if item.get("source"):
                    ex.setdefault("source_urls", {})[item["source"]] = item.get("url", "")
                # 补充缺失的标量字段（含 1.4.2 新增的评分人数）
                for f in ("cover", "title", "release_date", "score", "score_count",
                          "duration", "director", "studio", "label", "series"):
                    if not ex.get(f) and item.get(f):
                        ex[f] = item[f]
                # 列表级磁力角标：任一来源有磁力即标记（JavDB 才提供）
                if item.get("has_magnet"):
                    ex["has_magnet"] = True
                # 补充缺失的列表型字段（演员/标签/样品图/磁力）
                for f in ("actors", "tags", "samples", "magnets"):
                    if not ex.get(f) and item.get(f):
                        ex[f] = item[f]

    return [merged[k] for k in order]


# 单个来源列表抓取的超时（秒）。防止某个慢源（如 JavDB 走 FlareSolverr）
# 把整个结果拖住——超时的源直接丢弃，其余源照常合并返回。
# 搜索要快，超时短些；首页最新是后台加载，可多等以便慢源（JavDB/FlareSolverr）也能进来。
_PER_SOURCE_TIMEOUT = 25.0          # 搜索用
_PER_SOURCE_TIMEOUT_LATEST = 40.0   # 首页最新用


async def _run_source(label: str, coro, timeout: float):
    """给单个来源套超时，超时/异常都返回 (label, None)，不拖累其它来源。"""
    try:
        res = await asyncio.wait_for(coro, timeout=timeout)
        return label, res
    except asyncio.TimeoutError:
        print(f"[source] {label} 超时（>{timeout}s），已跳过")
        return label, None
    except Exception as e:
        print(f"[source] {label} 失败: {e!r}")
        return label, None


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
        tasks.append(_run_source(src, mod.search_list(query, mode, proxy, max_results), _PER_SOURCE_TIMEOUT))
        labels.append(src)

    paired = await asyncio.gather(*tasks, return_exceptions=True)

    collected = []
    for item in paired:
        if isinstance(item, tuple):
            label, res = item
            if isinstance(res, list) and res:
                collected.append((label, res))

    if not collected:
        return []
    if len(collected) == 1:
        out = collected[0][1]
        for it in out:
            it.setdefault("sources", [it.get("source", "")])
            it.setdefault("source_urls", {it.get("source", ""): it.get("url", "")})
        return out[:max_results]

    return _merge_lists(collected)[:max_results]


# 走 FlareSolverr 的来源（JavDB/FC2）专用「全局串行闸」。
# FlareSolverr 是单浏览器实例，不支持并发——多个请求同时打过去只会在它内部排队、
# 互相挤到全部超时。用进程级 Semaphore(1) 让这些请求一个一个来；
# 直连来源（JavBus/AVSOX/AVMOO）不受影响，照常并发。
_FLARESOLVERR_GATE = asyncio.Semaphore(1)
_FLARESOLVERR_SOURCES = ("javdb", "fc2")


async def enrich(items: list[dict], proxy: Optional[str] = None,
                 concurrency: int = 10, per_timeout: float = 9.0) -> list[dict]:
    """
    按需抓取详情。items 为待补全的条目（需含 url + source），
    返回与输入等长、顺序一致的详情列表（失败/超时项为 None）。

    并发策略：
      - 直连来源（JavBus/AVSOX/AVMOO）走 Semaphore(concurrency) 并发，单条快速超时。
        concurrency=10 是直连/代理源的甜点：明显快于 6，又不至于触发 AVSOX 等站限流。
      - 走 FlareSolverr 的来源（JavDB/FC2）走全局 Semaphore(1) 串行，单条放宽到 32s——
        FlareSolverr 单实例不支持并发，串行才能逐条成功而非集体超时（不受上面并发影响）。
    """
    sem = asyncio.Semaphore(concurrency)

    # 是否启用了 FlareSolverr（影响走 FlareSolverr 的来源 JavDB/FC2 的并发与超时）
    # 含「自动探测」：地址留空但已自动探到本机 FlareSolverr 时，同样按走 FS 处理（串行+放宽超时）
    flaresolverr_on = False
    try:
        from config_manager import load as load_config
        from ._fsgate import auto_endpoint as _fs_auto
        _cfg = load_config()
        flaresolverr_on = bool((_cfg.get("javdb_flaresolverr_url") or "").strip()
                               or (_cfg.get("fc2_flaresolverr_url") or "").strip()
                               or _fs_auto())
    except Exception:
        pass

    async def one(item):
        url = item.get("url", "")
        source = (item.get("source", "") or "").lower()
        mod = SOURCE_MODULES.get(source)
        if not mod or not url:
            return None
        use_fs = source in _FLARESOLVERR_SOURCES and flaresolverr_on
        # FlareSolverr 来源：全局串行闸 + 32s 超时；其余：并发信号量 + 快速超时
        gate = _FLARESOLVERR_GATE if use_fs else sem
        timeout = 32.0 if use_fs else per_timeout
        async with gate:
            await asyncio.sleep(0.05)
            try:
                return await asyncio.wait_for(mod.fetch_detail(url, proxy), timeout=timeout)
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
        tasks.append(_run_source(src, mod.get_latest(proxy, lim), _PER_SOURCE_TIMEOUT_LATEST))
        labels.append(src)

    paired = await asyncio.gather(*tasks, return_exceptions=True)
    collected = []
    for item in paired:
        if isinstance(item, tuple):
            label, res = item
            if isinstance(res, list) and res:
                collected.append((label, res))

    if not collected:
        return []
    if len(collected) == 1:
        out = collected[0][1]
        for it in out:
            it.setdefault("sources", [it.get("source", "")])
            it.setdefault("source_urls", {it.get("source", ""): it.get("url", "")})
        return out
    return _merge_lists(collected)


def detect_search_mode(query: str) -> str:
    """自动检测搜索模式"""
    query = query.strip()
    # FC2 番号：FC2-PPV-1234567 / FC2PPV1234567 / FC2-1234567（含中间数字，不被通用番号正则覆盖）
    if re.match(r'^(?i:fc2)[-\s]?(?i:ppv)?[-\s]?\d{5,7}$', query):
        return SEARCH_MODE_CODE
    if re.match(r'^[A-Za-z]{2,8}[-\s]?\d{2,6}$', query):
        return SEARCH_MODE_CODE
    if re.search(r'[぀-ヿ一-鿿]', query) and len(query) <= 10:
        return SEARCH_MODE_ACTOR
    return SEARCH_MODE_KEYWORD
