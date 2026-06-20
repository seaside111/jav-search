"""
FC2 官方成人卖场「最新发现源」(adult.contents.fc2.com, V1.5.0-beta23 起)

为什么加：sukebei 依赖「有人发种」，卖家上架到有种之间有数小时空窗，且不是每部都
有人发种 → 漏号、滞后。FC2 官方卖场是源头：每部 FC2-PPV 一上架即在此，号最新、最全。

两种模式（V1.5.0-beta24）：
  - **免登录首页**（默认，配置 fc2_market_cookie 留空）：抓 https://adult.contents.fc2.com/?sort=date
    直连、纯 nginx、无 Cloudflare，带年龄 cookie 即放行。缺点：首页是「新着+排行+推荐」
    多板块混排、号非严格倒序，故收齐后统一【编号降序+截断】，旧排行号自然沉底被丢。
  - **登录 Cookie**（填 fc2_market_cookie）：改走 /search/?sort=date&order=desc 干净倒序、
    可深翻页，更全更准。Cookie 失效（被重定向到 id.fc2.com 登录）则自动回退免登录首页。

封面/标题（V1.5.0-beta24）：列表卡自带缩略图（//contents-thumbnail2.fc2.com/wNNN/...，
无防盗链）——直接当封面，并把宽度升到 w480。**这修掉了用 fourhoi 确定性封面对最新番号
404（MissAV 尚未收录）导致最新卡无封面的问题。** 卡片标题取卖场商品标题；干净标题/样品图
仍由 MissAV 在列表/详情按需补全。下载仍走 sukebei/Jackett 按番号检索。卡片结构与 sukebei
卡同构，可被 fc2._merge_latest 无缝合并去重（同番号优先保留先到的较丰富卡）。
"""
import re
import math
from typing import Optional
import httpx
from bs4 import BeautifulSoup

from . import _missav  # fourhoi 兜底封面
from . import _sukebei  # 复用「无码」标题判定 _is_uncensored_title

MARKET_BASE = "https://adult.contents.fc2.com"
SOURCE = "FC2"
LABEL = "FC2"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,zh-CN;q=0.9,en;q=0.7",
}
# 年龄确认 cookie：免登录访问成人卖场（缺则被年龄门拦/重定向至确认页）
_AGE_COOKIES = {"contents_locale": "ja", "fc2_adult": "1", "is_adult": "1"}

# 商品链接 /article/<id>/ 里的 id 即 FC2-PPV 番号
_ART_RE = re.compile(r"/article/(\d{5,8})/")
# 缩略图懒加载兜底字段
_IMG_ATTRS = ("src", "data-src", "data-original", "data-lazy")


def _cfg(key, default):
    try:
        from config_manager import load as load_config
        return load_config().get(key, default)
    except Exception:
        return default


def _proxy() -> Optional[str]:
    return (_cfg("proxy", "") or "").strip() or None


def _market_cookie() -> str:
    return (_cfg("fc2_market_cookie", "") or "").strip()


def _parse_cookie_str(s: str) -> dict:
    """把浏览器导出的 Cookie 串解析成 dict（与年龄 cookie 合并使用）。"""
    out = {}
    for part in (s or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip()
            if k:
                out[k] = v
    return out


def _bigger(thumb: str) -> str:
    """卖场缩略图按宽度参数缩放；列表给 w360/w276，统一升到 w480 当卡片封面更清晰。"""
    if not thumb:
        return ""
    u = ("https:" + thumb) if thumb.startswith("//") else thumb
    return re.sub(r"/w\d+(h\d+)?/", "/w480/", u, count=1)


def _card(num: str, cover: str, title: str) -> dict:
    """与 _sukebei._card 同构，但封面优先用卖场自带缩略图（fourhoi 仅作兜底）。"""
    code = f"FC2-PPV-{num}"
    return {
        "code": code,
        "title": (title or "").strip() or code,
        "cover": cover or _missav.cover_url(num),   # 卖场真封面优先，无则 fourhoi 兜底
        "url": f"https://fc2ppvdb.com/articles/{num}",  # 点开→详情(MissAV 补全)
        "source": SOURCE,
        "release_date": "", "duration": "", "director": "", "studio": "",
        "label": LABEL, "series": "", "score": "", "score_count": "",
        "has_magnet": False, "actors": [], "tags": [], "samples": [],
        "magnets": [], "description": "", "detail_loaded": False,
        # 「无码」弱标记：商品标题含卖家自填无码字样即 True（仅参考，未命中≠有码）
        "uncensored_hint": _sukebei._is_uncensored_title(title),
    }


def _parse_cards(html: str) -> list[dict]:
    """从卖场列表页抽出 (番号, 封面缩略图, 标题)。同一商品常有多个 /article/ 链接
    （图片链 + 标题链）：按番号聚合，封面取带 <img> 的、标题取带文本的。"""
    soup = BeautifulSoup(html, "html.parser")
    data, order = {}, []
    for a in soup.select("a[href*='/article/']"):
        m = _ART_RE.search(a.get("href", ""))
        if not m:
            continue
        num = m.group(1)
        d = data.get(num)
        if d is None:
            d = {"cover": "", "title": ""}
            data[num] = d
            order.append(num)
        if not d["cover"]:
            img = a.find("img") or (a.find_parent().find("img") if a.find_parent() else None)
            if img:
                for k in _IMG_ATTRS:
                    v = img.get(k)
                    if v and "blank" not in v and "spacer" not in v:
                        d["cover"] = v
                        break
        if not d["title"]:
            t = (a.get("title") or a.get_text(" ", strip=True)).strip()
            if t:
                d["title"] = t
    return [_card(n, _bigger(data[n]["cover"]), data[n]["title"]) for n in order]


async def _fetch_mode(client: httpx.AsyncClient, url_fn, pages: int):
    """按某模式(免登录首页/登录搜索)逐页抓取并解析。
    返回卡片列表；若首页即被重定向到登录(id.fc2.com)则返回 None（信号：该模式不可用）。"""
    cards, seen = [], set()
    for page in range(1, pages + 1):
        try:
            resp = await client.get(url_fn(page))
        except Exception as e:
            print(f"[fc2market] 抓取异常 (page {page}): {type(e).__name__}: {e}")
            break
        if "id.fc2.com" in str(resp.url):           # 被弹登录页
            return cards if cards else None
        if resp.status_code != 200 or not resp.text:
            print(f"[fc2market] HTTP {resp.status_code} (page {page})")
            break
        added = 0
        for card in _parse_cards(resp.text):
            if card["code"] in seen:
                continue
            seen.add(card["code"])
            cards.append(card)
            added += 1
        if added == 0:                              # 该页无新番号 → 停翻
            break
    return cards


async def fetch_fc2_latest(proxy: Optional[str] = None, limit: int = 60,
                           max_pages: int = 3) -> list[dict]:
    """
    取 FC2 官方卖场最新番号（带封面/标题）。直连、不过盾、快。
    有登录 Cookie → 走 /search 干净倒序深翻页；无/失效 → 免登录首页 ?sort=date。
    收齐后按番号降序再截断，确保最新不被页序丢弃。失败返回已得部分（绝不整体消失）。
    """
    if proxy is None:
        proxy = _proxy()
    cookie = _market_cookie()
    cookies = {**_AGE_COOKIES, **(_parse_cookie_str(cookie) if cookie else {})}
    # 翻页策略按模式区分：
    #  - 免登录首页(?sort=date)：是「新着+排行+推荐」混排，唯一番号实测饱和在 ~40，翻再多
    #    页也是重复 → 封顶 3 页即可（够拿最新一批，且快，不空跑）。
    #  - 登录搜索(/search)：真正的倒序分页，按 limit 深翻（每页约 18 个），封顶 10 页。
    if cookie:
        pages = min(10, max(max_pages, math.ceil(max(1, limit) / 18)))
    else:
        pages = max(max_pages, 3)

    def search_url(p):
        return f"{MARKET_BASE}/search/?sort=date&order=desc" + ("" if p == 1 else f"&page={p}")

    def home_url(p):
        return f"{MARKET_BASE}/?sort=date" + ("" if p == 1 else f"&page={p}")

    cards = None
    try:
        async with httpx.AsyncClient(headers=_HEADERS, cookies=cookies,
                                     proxy=proxy or None, timeout=15,
                                     follow_redirects=True) as client:
            if cookie:
                cards = await _fetch_mode(client, search_url, pages)
                if cards is None:
                    print("[fc2market] 登录 Cookie 无效/过期，回退免登录首页模式")
            if not cards:                            # None(被弹) 或 [](空) → 免登录首页
                cards = await _fetch_mode(client, home_url, pages) or []
    except Exception as e:
        print(f"[fc2market] 取最新失败: {type(e).__name__}: {e}")
        cards = cards or []

    # 按番号降序再截断：避免最新号因混排页序靠后被 limit 砍掉
    cards.sort(key=lambda c: int(re.sub(r"\D", "", c["code"]) or 0), reverse=True)
    return cards[: max(1, limit)]


# 商品页主封面：<meta property="og:image"> 的卖家上传全图（storage*.contents.fc2.com）
_OG_IMG_RE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', re.I)

# 缩略图 CDN 前缀（卖场列表卡同款）：把 storage 原图包成它，无防盗链、有尺寸、易取
_THUMB_CDN = "https://contents-thumbnail2.fc2.com/w480/"


def thumbify(url: str) -> str:
    """把 storage*.contents.fc2.com 原图 URL 转成缩略图 CDN 格式（已是缩略图则原样返回）。
    原图常有防盗链/体量大，缩略图 CDN 与列表卡封面同源、实测无防盗链、项目机易抓。"""
    if not url:
        return url
    if "contents-thumbnail" in url:
        return url
    if "contents.fc2.com" in url and "/file/" in url:
        return _THUMB_CDN + re.sub(r"^https?://", "", url)
    return url


# 商品已下架/不存在时卖场页的日文提示文案（用于「精准识别下架」，区别于地区拦截/网络抖动）。
# 命中其一 + HTTP 200 即判定该号确已下架，停止后续重抓官图。
_GONE_MARKERS = ("販売を終了", "存在しないか", "見つかりません", "削除された",
                 "商品が見つかりません", "ページが見つかりません")

# 需登录才能查看的商品（付费/限定，ref=payarticle）：页面是 meta-refresh 跳 fc2.com/login.php 的空壳，
# 匿名取不到 og:image。填了卖场登录 Cookie(fc2_market_cookie) 后 FC2 会直接出商品页、即可取官图。
_LOGIN_REDIRECT_RE = re.compile(r'http-equiv=["\']?refresh["\']?[^>]*login\.php', re.I)


async def fetch_cover_ex(num: str, proxy: Optional[str] = None) -> tuple:
    """抓单个 FC2 商品页封面，并**精准区分「下架/不存在」与「临时失败」**。
    返回 (cover_url, gone)：
      - (url, False) ：取到官图（contents.fc2.com 缩略图 CDN）。
      - ("",  True)  ：服务器可达且明确指向「下架/不存在」（HTTP 404，或 200 + 下架提示文案）
                       → 上层据此锁定、不再重抓官图。
      - ("",  False) ：临时失败（被弹登录/年龄门、403/5xx、超时、网络错误、改版无提示）
                       → 仍按退避重试，避免地区拦截被误判成下架。"""
    if not num:
        return "", False
    if proxy is None:
        proxy = _proxy()
    cookie = _market_cookie()
    cookies = {**_AGE_COOKIES, **(_parse_cookie_str(cookie) if cookie else {})}
    url = f"{MARKET_BASE}/article/{num}/"
    try:
        async with httpx.AsyncClient(headers=_HEADERS, cookies=cookies,
                                     proxy=proxy or None, timeout=12,
                                     follow_redirects=True) as client:
            r = await client.get(url)
    except Exception:
        return "", False                      # 网络错误/超时 → 临时失败
    # 被弹登录/年龄确认页 → 访问受限（地区/Cookie），不是下架
    if "id.fc2.com" in str(r.url):
        return "", False
    if r.status_code == 404:
        return "", True                       # 明确 404 → 下架/不存在
    if r.status_code != 200 or not r.text:
        return "", False                      # 403/5xx 等 → 临时失败
    # 需登录商品（meta-refresh 跳 login.php）：匿名取不到封面。
    #   无登录 Cookie → 匿名永远取不到 → 当"取不到"收手(gone)，不再每轮白抓；
    #   有 Cookie 仍被弹 → Cookie 失效 → 临时失败可重试（修好 Cookie 即自愈）。
    if _LOGIN_REDIRECT_RE.search(r.text):
        return "", (not cookie)
    m = _OG_IMG_RE.search(r.text)
    if m:
        u = m.group(1).strip()
        if u.startswith("//"):
            u = "https:" + u
        # 真商品封面在 storage*.contents.fc2.com/file/...；排除站点 logo 等占位
        if "contents.fc2.com" in u and "/file/" in u:
            return thumbify(u), False         # 官图：转缩略图 CDN（无防盗链、与列表卡同源）
        return "", False                      # 有 og:image 但非商品图 → 当临时失败，别误判下架
    # 200 且无商品 og:image：命中下架提示文案才判下架，否则临时失败（防地区/改版误杀）
    if any(k in r.text for k in _GONE_MARKERS):
        return "", True
    return "", False


async def fetch_cover(num: str, proxy: Optional[str] = None) -> str:
    """兼容旧接口：只取封面 URL（不关心下架信号）。失败/下架/非商品图均返回 ''。"""
    cover, _gone = await fetch_cover_ex(num, proxy)
    return cover
