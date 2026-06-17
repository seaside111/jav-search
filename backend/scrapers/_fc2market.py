"""
FC2 官方成人卖场「最新发现源」(adult.contents.fc2.com, V1.5.0-beta23)

为什么加：sukebei 依赖「有人发种」，卖家上架到有种之间有数小时空窗，且不是每部都
有人发种 → 漏号、滞后（实测官方已到 4922905，而 sukebei 侧才 4921859）。FC2 官方
卖场是源头：每部 FC2-PPV 一上架即在此，号最新、最全。

可达性（实测 2026-06）：
  - 首页 https://adult.contents.fc2.com/?sort=date **直连、纯 nginx、无 Cloudflare**，
    带年龄确认 cookie 即放行，无需登录。
  - 干净倒序的 /search/?sort=date 现需登录（弹 id.fc2.com），故走免登录首页。
  - 首页是「新着 + 排行 + 推荐」多板块混排，号非严格倒序、还混入热门旧号。本模块只
    负责「薅出全部商品番号」，交由上层 fc2.get_latest 统一【编号降序 + 截断】，旧排行
    号自然沉底被丢——无需精确定位新着 DOM 块，抗版面改动。

只负责发现番号（+ fourhoi 确定性封面），标题/样品图仍由 MissAV 在列表/详情按需补全，
下载仍走 sukebei/Jackett 按番号检索。卡片结构直接复用 _sukebei._card，保证与 sukebei
卡完全同构，可被 fc2._merge_latest 无缝合并去重（同番号优先保留 sukebei 的种子标题卡）。
"""
import re
from typing import Optional
import httpx

from . import _sukebei  # 复用同构卡片结构(_card)

MARKET_BASE = "https://adult.contents.fc2.com"
SOURCE = "FC2"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,zh-CN;q=0.9,en;q=0.7",
}
# 年龄确认 cookie：免登录访问成人卖场（缺则被年龄门拦/重定向至确认页）
_COOKIES = {"contents_locale": "ja", "fc2_adult": "1", "is_adult": "1"}

# 商品链接 /article/<id>/ 里的 id 即 FC2-PPV 番号
_ART_RE = re.compile(r"/article/(\d{5,8})/")


def _proxy() -> Optional[str]:
    try:
        from config_manager import load as load_config
        return (load_config().get("proxy") or "").strip() or None
    except Exception:
        return None


def _parse(html: str) -> list[str]:
    """薅出该页全部商品番号（去重保序）。"""
    out, seen = [], set()
    for m in _ART_RE.finditer(html or ""):
        num = m.group(1)
        if num not in seen:
            seen.add(num)
            out.append(num)
    return out


async def fetch_fc2_latest(proxy: Optional[str] = None, limit: int = 60,
                           max_pages: int = 3) -> list[dict]:
    """
    从 FC2 官方卖场首页(?sort=date)取最新番号。直连、不过盾、快。
    多板块混排 → 跨页累积全部番号后【按番号降序】再截断，确保真正最新的不被页序丢弃；
    上层 get_latest 会再次统一排序/截断。失败/异常返回已得部分（绝不整体消失）。
    """
    if proxy is None:
        proxy = _proxy()
    nums, seen = [], set()
    try:
        async with httpx.AsyncClient(headers=_HEADERS, cookies=_COOKIES,
                                     proxy=proxy or None, timeout=15,
                                     follow_redirects=True) as client:
            for page in range(1, max(1, max_pages) + 1):
                url = f"{MARKET_BASE}/?sort=date" + ("" if page == 1 else f"&page={page}")
                resp = await client.get(url)
                if resp.status_code != 200 or not resp.text:
                    print(f"[fc2market] HTTP {resp.status_code} (page {page})")
                    break
                added = 0
                for num in _parse(resp.text):
                    if num in seen:
                        continue
                    seen.add(num)
                    nums.append(num)
                    added += 1
                if added == 0:                       # 该页无新番号 → 后续页多为重复，停翻
                    break
    except Exception as e:
        print(f"[fc2market] 取最新失败: {type(e).__name__}: {e}")
    # 先按番号降序再截断：避免最新号因混排页序靠后而被 limit 砍掉
    nums.sort(key=int, reverse=True)
    return [_sukebei._card(num, "") for num in nums[: max(1, limit)]]
