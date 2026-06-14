"""
javu 平台 JSON API 客户端（AVSOX / AVMOO 2026 改版后共用）。

AVSOX、AVMOO 于 2026 年整站改成 Vue SPA：服务器只返回一个空壳 HTML
（<div id="javu-site-index">，由 javu.min.js 客户端渲染），旧的静态 HTML 网格
（movie-box）不复存在，原先复用 _javbus_base 的 HTML 解析自然抓不到任何数据。
数据改由 JSON 接口提供，两站现已共用同一套「javu」后端，故抽出本模块统一实现，
avsox.py / avmoo.py 仅传入各自域名与来源名。

接口要点（实测 2026-06）：
  - 端点：POST {BASE}/javu/data/api/<method>，请求体 JSON。
  - 鉴权/反爬：唯一硬性要求是带 Origin 头且与站点同源，否则 nginx WAF 直接
    403（标记 "VBU"）。无需 CSRF Token、无需 Cookie。请求体未签名/未加密。
  - method：
      getMovies  {page, pageSize, lang}  → 最新片源列表
      getMovie   {id(=movieId), lang}    → 单片详情（含解析好的 studio/genre/star/样品图）
      search     {search, lang}          → 关键词/番号搜索
  - 返回 {code:200, data:[...]|{...}}；图片字段 posterSmall/posterLarge 已是完整 URL，
    相对路径 ps/pl 则需拼接图片 CDN（IMG_BASE）兜底。
  - 详情页前端路由为 /<lang>/movies/<movieId>，故 url 以 movieId 结尾，
    fetch_detail 反向取末段作为 getMovie 的 id。
"""
import re
from typing import Optional
import httpx

LANG = "cn"
# 单次最多取多少条（安全上限，防止误填超大值打爆响应；服务端本身不封顶）。
MAX_PAGE_SIZE = 200
# 原始图片 CDN：posterSmall/posterLarge 缺失时，按相对 ps/pl 路径兜底拼接。
IMG_BASE = "https://file.netcdn.space/storage"


def _bases(base_or_list) -> list[str]:
    """把单域名或域名列表规整成去重后的候选列表（保序）。"""
    seq = [base_or_list] if isinstance(base_or_list, str) else list(base_or_list or [])
    out = []
    for b in seq:
        b = (b or "").rstrip("/")
        if b and b not in out:
            out.append(b)
    return out

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _headers(base: str) -> dict:
    # Origin 必须与站点同源，WAF 仅凭此放行（缺则 403 VBU）。
    return {
        "User-Agent": _UA,
        "Origin": base,
        "Referer": f"{base}/{LANG}",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }


async def _api(base: str, method: str, body, proxy: Optional[str],
               source: str, timeout: float = 15.0):
    """调一次 javu JSON 接口，成功返回 data 字段，失败返回 None。

    注意请求体格式：javu 客户端把方法的【位置参数】整体作为 JSON 数组发出
    （例如 search({search,lang}, pageSize, lang) → body=[{...}, 30, "cn"]）。
    用错成单个对象会被服务端忽略参数、返回默认数据（搜索表现为永远只回最新第一条），
    故 body 必须是与客户端一致的位置参数数组（仅含 File/Blob 时才转 FormData，这里用不到）。
    """
    url = f"{base}/javu/data/api/{method}"
    try:
        async with httpx.AsyncClient(proxy=proxy or None, timeout=timeout,
                                     follow_redirects=True) as client:
            resp = await client.post(url, json=body, headers=_headers(base))
        if resp.status_code != 200:
            print(f"[{source}] api {method} HTTP {resp.status_code}")
            return None
        data = resp.json()
        if not isinstance(data, dict) or data.get("code") != 200:
            code = data.get("code") if isinstance(data, dict) else "?"
            print(f"[{source}] api {method} code={code}")
            return None
        return data.get("data")
    except Exception as e:
        print(f"[{source}] api {method} error: {type(e).__name__}: {e!r}")
        return None


async def _call(bases: list[str], method: str, body, proxy: Optional[str],
                source: str):
    """按域名顺序做故障转移：依次试每个候选域名，第一个**请求成功**（_api 返回非
    None，包括返回空列表的合法结果）的即采用，返回 (data, base_used)。
    只在真正请求失败（HTTP 错误/异常/code≠200 → None）时才切下一个域名；
    搜索返回空列表属正常结果，不会触发无谓的备用域名重试。全失败返回 (None, 首个域名)。"""
    first = bases[0] if bases else ""
    for b in bases:
        data = await _api(b, method, body, proxy, source)
        if data is not None:
            if b != first:
                print(f"[{source}] 主域名不可用，已切备用域名 {b}", flush=True)
            return data, b
    return None, first


def _abs_img(val: str) -> str:
    """把图片字段规整成绝对 URL：已是 http(s) 原样返回，相对路径拼 CDN。"""
    if not val:
        return ""
    if val.startswith("http://") or val.startswith("https://"):
        return val
    return IMG_BASE + val if val.startswith("/") else f"{IMG_BASE}/{val}"


def _movie_url(base: str, m: dict) -> str:
    return f"{base}/{LANG}/movies/{m.get('movieId', '')}"


def _title(m: dict) -> str:
    return (m.get("title") or m.get("title_cn") or m.get("title_ja")
            or m.get("title_en") or "").strip()


def _list_item(base: str, source: str, m: dict) -> dict:
    code = (m.get("movieFanHao") or "").strip()
    thumb = _abs_img(m.get("posterSmall") or m.get("ps") or "")
    cover = _abs_img(m.get("posterLarge") or m.get("pl") or "") or thumb
    return {
        "code": code,
        "title": _title(m) or code,
        "cover": cover,
        "cover_thumb": thumb,
        "url": _movie_url(base, m),
        "source": source,
        "release_date": (m.get("releaseDate") or "")[:10],
        "duration": f"{m.get('length')}分钟" if m.get("length") else "",
        "director": "", "studio": "", "label": "", "series": "",
        "score": "", "actors": [], "tags": [], "description": "",
        "detail_loaded": False,
    }


def _detail_item(base: str, source: str, m: dict) -> dict:
    code = (m.get("movieFanHao") or "").strip()
    thumb = _abs_img(m.get("posterSmall") or m.get("ps") or "")
    cover = _abs_img(m.get("posterLarge") or m.get("pl") or "") or thumb

    studio = ""
    s = m.get("studio")
    if isinstance(s, dict):
        studio = (s.get("studioName") or s.get("studioName_cn")
                  or s.get("studioName_ja") or "")

    actors = []
    for st in (m.get("star") or []):
        if not isinstance(st, dict):
            continue
        name = (st.get("starName") or st.get("starName_ja")
                or st.get("starName_cn") or "").strip()
        if name:
            actors.append({"name": name,
                           "avatar": st.get("avatarUrl") or _abs_img(st.get("avatar", ""))})

    tags = []
    for g in (m.get("genre") or []):
        if not isinstance(g, dict):
            continue
        t = (g.get("genreName") or g.get("genreName_cn")
             or g.get("genreName_ja") or "").strip()
        if t:
            tags.append(t)

    samples = [_abs_img(x) for x in (m.get("sampleLarge") or m.get("sampleSmall") or []) if x]
    desc = (m.get("description_cn") or m.get("description_ja")
            or m.get("description_en") or "").strip()

    return {
        "title": _title(m) or code,
        "cover": cover,
        "cover_thumb": thumb,
        "url": _movie_url(base, m),
        "source": source,
        "code": code,
        "release_date": (m.get("releaseDate") or "")[:10],
        "duration": f"{m.get('length')}分钟" if m.get("length") else "",
        "director": "", "studio": studio, "label": "", "series": "",
        "score": "", "actors": actors, "tags": tags,
        "samples": samples, "description": desc,
        "detail_loaded": True,
    }


# ── 对外：供 avsox.py / avmoo.py 调用 ──
# 各函数的 base 形参既可传单域名字符串，也可传【域名候选列表】（主→备用）做故障转移。
async def get_latest(base, source: str, proxy: Optional[str],
                     max_results: int) -> list[dict]:
    # getMovies(page, pageSize, lang)：服务端尊重 pageSize 但忽略 page（实测翻页无效），
    # 故一次性按 pageSize=max_results 取最新 N 条即可，无需翻页。
    size = max(1, min(int(max_results), MAX_PAGE_SIZE))
    data, used = await _call(_bases(base), "getMovies", [1, size, LANG], proxy, source)
    if not isinstance(data, list):
        return []
    out, seen = [], set()
    for m in data:
        if not isinstance(m, dict):
            continue
        it = _list_item(used, source, m)
        key = it["code"] or it["url"]
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out[:max_results]


async def search_list(base, source: str, query: str, mode: str,
                      proxy: Optional[str], max_results: int) -> list[dict]:
    # search({search, lang}, pageSize, lang)：位置参数数组，按番号(movieFanHao)匹配。
    size = max(int(max_results), 30)
    data, used = await _call(_bases(base), "search",
                             [{"search": query, "lang": LANG}, size, LANG],
                             proxy, source)
    if not isinstance(data, list) or not data:
        return []
    items = [_list_item(used, source, m) for m in data if isinstance(m, dict)]
    # 番号搜索：把番号精确吻合的排到最前
    if mode == "code":
        norm = re.sub(r"[^a-z0-9]", "", (query or "").lower())
        if norm:
            exact = [x for x in items
                     if re.sub(r"[^a-z0-9]", "", x["code"].lower()) == norm]
            if exact:
                rest = [x for x in items if x not in exact]
                items = exact + rest
    return items[:max_results]


async def fetch_detail(base, source: str, url: str,
                       proxy: Optional[str]) -> Optional[dict]:
    mid = (url or "").rstrip("/").split("/")[-1].strip()
    if not mid:
        return None
    m, used = await _call(_bases(base), "getMovie", [mid, LANG], proxy, source)
    if not isinstance(m, dict):
        return None
    return _detail_item(used, source, m)
