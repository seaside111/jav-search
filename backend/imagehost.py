"""
图床上传（V1.5）

M-Team 官方 API 无图片上传端点（OpenAPI 仅 /subtitle/upload 与 createOredit），
发种简介里的截图必须传第三方图床、再把 BBCode 塞进 descr。

支持多图床「按序兜底」：按配置 image_host(优先图床) 排序，依次尝试，第一个成功即用；
每个图床内再「代理优先、直连兜底 + 重试」。已接入：
  - catbox.moe：POST https://catbox.moe/user/api.php，multipart reqtype=fileupload + fileToUpload，
    返回纯文本直链。免 key、对代理友好（默认首选）。
  - pixhost：POST https://api.pixhost.to/images，返回 JSON {show_url, th_url}，
    原图直链由缩略图推导（t##→img##、thumbs→images），必要时抓 show 页 #image 兜底。
    JAV 属成人内容，content_type 固定 1。
扩展新图床：实现 _upload_xxx(content, filename, proxy) 后登记到 _HOST_UPLOADERS / ALL_HOSTS。
"""
import asyncio
import base64
import re
from pathlib import Path
from typing import Optional
import httpx

PIXHOST_API = "https://api.pixhost.to/images"
# 用真实浏览器 UA：catbox.moe 会按 UA 拦截非浏览器请求（返回 412 Invalid uploader），
# 自定义 UA(如 jav-search/1.5) 会被拒；pixhost 也对浏览器 UA 更友好。
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# pixhost 真实大图直链格式：https://img<N>.pixhost.to/images/<相册>/<编号>_<文件名>
# （由 nikukyugamer/pixhost-downloader 的 cypress 用例固化确认）
_PIX_REAL_RE = re.compile(r"https://img\d+\.pixhost\.to/images/", re.I)


def _derive_pixhost_img(th_url: str) -> str:
    """由缩略图 URL 推导原图直链：t##.pixhost.to/thumbs/ → img##.pixhost.to/images/
    （缩略图与原图同相册同文件名、仅子域 t→img、路径 thumbs→images）。"""
    return th_url.replace("//t", "//img", 1).replace("/thumbs/", "/images/")


async def _pixhost_real_img(show_url: str, th_url: str, proxy: Optional[str]) -> str:
    """
    取 pixhost 原图直链。
      1) 先按缩略图推导（免额外请求，最快）；命中已知格式即用。
      2) 推导不符合已知格式（pixhost 改了规则）→ 抓 show 页解析 <img id="image"> 的真实 src
         （nikukyugamer/pixhost-downloader 的权威办法），仍失败则退回推导值。
    """
    derived = _derive_pixhost_img(th_url)
    if _PIX_REAL_RE.match(derived):
        return derived
    if show_url:
        try:
            async with httpx.AsyncClient(proxy=proxy, timeout=30,
                                         follow_redirects=True) as client:
                r = await client.get(show_url, headers={"User-Agent": _UA})
            m = re.search(r'id=["\']image["\'][^>]*\ssrc=["\']([^"\']+)["\']',
                          r.text or "", re.I)
            if m:
                return m.group(1).strip()
        except Exception:
            pass
    return derived


async def _upload_pixhost(content: bytes, filename: str, proxy: Optional[str],
                          th_size: int = 350, timeout: int = 60) -> dict:
    data = {"content_type": "1", "max_th_size": str(th_size)}
    files = {"img": (filename, content, "application/octet-stream")}
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=timeout,
                                     follow_redirects=True) as client:
            resp = await client.post(PIXHOST_API, data=data, files=files,
                                     headers={"User-Agent": _UA, "Accept": "application/json"})
    except Exception as e:
        return {"ok": False, "error": f"pixhost 上传失败: {e}"}
    if resp.status_code not in (200, 201):
        return {"ok": False, "error": f"pixhost HTTP {resp.status_code}: {resp.text[:120]}"}
    try:
        j = resp.json()
    except Exception:
        return {"ok": False, "error": f"pixhost 响应非 JSON: {resp.text[:120]}"}
    show_url = j.get("show_url") or ""
    th_url = j.get("th_url") or ""
    if not th_url:
        return {"ok": False, "error": f"pixhost 未返回图片地址: {j}"}
    # 取原图直链（推导为主、show 页解析为兜底），帖子里直接显示原图，免点开图床 show 页。
    img_url = await _pixhost_real_img(show_url, th_url, proxy)
    return {"ok": True, "show_url": show_url, "th_url": th_url, "img_url": img_url,
            "bbcode": f"[img]{img_url}[/img]", "error": ""}


CATBOX_API = "https://catbox.moe/user/api.php"


async def _upload_catbox(content: bytes, filename: str, proxy: Optional[str],
                         timeout: int = 60) -> dict:
    """catbox.moe 匿名上传：multipart reqtype=fileupload + fileToUpload，
    返回纯文本直链（无缩略图/无 show 页）。免 key、对代理友好。"""
    data = {"reqtype": "fileupload"}
    files = {"fileToUpload": (filename, content, "application/octet-stream")}
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=timeout,
                                     follow_redirects=True) as client:
            resp = await client.post(CATBOX_API, data=data, files=files,
                                     headers={"User-Agent": _UA})
    except Exception as e:
        return {"ok": False, "error": f"catbox 上传失败: {e}"}
    if resp.status_code not in (200, 201):
        return {"ok": False, "error": f"catbox HTTP {resp.status_code}: {resp.text[:120]}"}
    url = (resp.text or "").strip()
    if not url.lower().startswith("http"):
        return {"ok": False, "error": f"catbox 返回异常: {url[:120]}"}
    return {"ok": True, "show_url": url, "th_url": url, "img_url": url,
            "bbcode": f"[img]{url}[/img]", "error": ""}


IMGBB_API = "https://api.imgbb.com/1/upload"


def _imgbb_key() -> str:
    try:
        from config_manager import load as _load
        return (_load().get("image_imgbb_key") or "").strip()
    except Exception:
        return ""


async def _upload_imgbb(content: bytes, filename: str, proxy: Optional[str],
                        timeout: int = 60) -> dict:
    """imgbb 上传：需免费 API Key。key 鉴权、不按 IP 过滤——机房/代理出口 IP 也能传，
    适合 catbox(封机房IP)/pixhost(代理不通) 都失败的环境。返回 JSON data.url 直链。"""
    key = _imgbb_key()
    if not key:
        return {"ok": False, "error": "imgbb 未配置 API Key（发种设置里填）"}
    data = {"key": key, "image": base64.b64encode(content).decode("ascii"),
            "name": Path(filename).stem}
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=timeout,
                                     follow_redirects=True) as client:
            resp = await client.post(IMGBB_API, data=data, headers={"User-Agent": _UA})
    except Exception as e:
        return {"ok": False, "error": f"imgbb 上传失败: {e}"}
    if resp.status_code not in (200, 201):
        return {"ok": False, "error": f"imgbb HTTP {resp.status_code}: {resp.text[:120]}"}
    try:
        j = resp.json()
    except Exception:
        return {"ok": False, "error": f"imgbb 响应非 JSON: {resp.text[:120]}"}
    url = ((j.get("data") or {}).get("url")) or ""
    if not url:
        return {"ok": False, "error": f"imgbb 未返回地址: {str(j)[:120]}"}
    return {"ok": True, "show_url": url, "th_url": url, "img_url": url,
            "bbcode": f"[img]{url}[/img]", "error": ""}


FREEIMAGE_API = "https://freeimage.host/api/1/upload"
# freeimage.host(Chevereto) 官方公开 API key（其 API 文档/社区通用），免注册即用；被限流可在设置
# 填自己的 key 覆盖。直链落在 iili.io（Chevereto CDN，Cloudflare 前置，国内直连命中率较高）。
FREEIMAGE_PUBLIC_KEY = "6d207e02198a847aa98d0a2a901485a5"


def _freeimage_key() -> str:
    try:
        from config_manager import load as _load
        return (_load().get("image_freeimage_key") or "").strip() or FREEIMAGE_PUBLIC_KEY
    except Exception:
        return FREEIMAGE_PUBLIC_KEY


async def _upload_freeimage(content: bytes, filename: str, proxy: Optional[str],
                            timeout: int = 60) -> dict:
    """freeimage.host(Chevereto) 上传：API 与 imgbb 同形（key + base64 source）。免注册用公开 key，
    成人内容较宽松；直链 iili.io 走 Cloudflare，国内直连命中率较高。返回 image.url 直链。"""
    data = {"key": _freeimage_key(),
            "source": base64.b64encode(content).decode("ascii"),
            "format": "json"}
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=timeout,
                                     follow_redirects=True) as client:
            resp = await client.post(FREEIMAGE_API, data=data, headers={"User-Agent": _UA})
    except Exception as e:
        return {"ok": False, "error": f"freeimage 上传失败: {e}"}
    if resp.status_code not in (200, 201):
        return {"ok": False, "error": f"freeimage HTTP {resp.status_code}: {resp.text[:120]}"}
    try:
        j = resp.json()
    except Exception:
        return {"ok": False, "error": f"freeimage 响应非 JSON: {resp.text[:120]}"}
    url = ((j.get("image") or {}).get("url")) or ""
    if not url:
        return {"ok": False, "error": f"freeimage 未返回地址: {str(j)[:120]}"}
    return {"ok": True, "show_url": url, "th_url": url, "img_url": url,
            "bbcode": f"[img]{url}[/img]", "error": ""}


IMGCHEST_API = "https://api.imgchest.com/v1/post"


def _imgchest_token() -> str:
    try:
        from config_manager import load as _load
        return (_load().get("image_imgchest_token") or "").strip()
    except Exception:
        return ""


async def _upload_imgchest(content: bytes, filename: str, proxy: Optional[str],
                           timeout: int = 60) -> dict:
    """imgchest 上传：需个人 access token（imgchest.com → 账号 → API 生成）。明确允许 NSFW
    （nsfw=true 标记）、不压缩（AV 截图画质好），直链 cdn.imgchest.com 走 Cloudflare。
    返回 data.images[0].link 直链。"""
    token = _imgchest_token()
    if not token:
        return {"ok": False, "error": "imgchest 未配置 access token（发种设置里填；imgchest.com 账号→API 取）"}
    data = {"nsfw": "true"}
    files = {"images[]": (filename, content, "application/octet-stream")}
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=timeout,
                                     follow_redirects=True) as client:
            resp = await client.post(IMGCHEST_API, data=data, files=files,
                                     headers={"User-Agent": _UA, "Authorization": f"Bearer {token}"})
    except Exception as e:
        return {"ok": False, "error": f"imgchest 上传失败: {e}"}
    if resp.status_code not in (200, 201):
        return {"ok": False, "error": f"imgchest HTTP {resp.status_code}: {resp.text[:120]}"}
    try:
        j = resp.json()
    except Exception:
        return {"ok": False, "error": f"imgchest 响应非 JSON: {resp.text[:120]}"}
    imgs = ((j.get("data") or {}).get("images")) or []
    url = (imgs[0].get("link") if imgs else "") or ""
    if not url:
        return {"ok": False, "error": f"imgchest 未返回地址: {str(j)[:160]}"}
    return {"ok": True, "show_url": url, "th_url": url, "img_url": url,
            "bbcode": f"[img]{url}[/img]", "error": ""}


# 图床注册表 + 默认顺序。preferred 优先、其余兜底。
#   imgbb：需免费 key，key 鉴权不按 IP 过滤，机房/代理环境最稳；
#   imgchest：需 token，明确允许 NSFW、不压缩，直链走 Cloudflare（国内直连命中率较高）；
#   freeimage：免注册(公开key)，成人较宽松，直链 iili.io 走 Cloudflare；
#   catbox：免key，但封机房/非住宅 IP（代理出口多为机房 IP→412 Invalid uploader）；
#   pixhost：免key，部分代理/时段不通（站点可能临时关闭），保留作兜底。
# 注：postimage 已移除——其 key 版接口被官方停用（version not supported）。
_HOST_UPLOADERS = {"imgbb": _upload_imgbb, "imgchest": _upload_imgchest,
                   "freeimage": _upload_freeimage,
                   "catbox": _upload_catbox, "pixhost": _upload_pixhost}
ALL_HOSTS = ["imgbb", "imgchest", "freeimage", "catbox", "pixhost"]


def order_hosts(preferred: Optional[str]) -> list:
    """按"优先图床"排出尝试顺序：preferred 在前、其余兜底（去重、过滤未知）。"""
    pref = (preferred or "").strip().lower()
    order = ([pref] if pref in _HOST_UPLOADERS else []) + \
            [h for h in ALL_HOSTS if h != pref]
    seen, out = set(), []
    for h in order:
        if h in _HOST_UPLOADERS and h not in seen:
            seen.add(h); out.append(h)
    return out or list(ALL_HOSTS)


async def _try_one_host(uploader, content: bytes, filename: str,
                        proxy: Optional[str], retries: int) -> dict:
    """单个图床：代理优先、直连兜底，各重试 retries 次；报错优先取代理通道的（更有诊断价值）。"""
    channels = [("代理", proxy), ("直连", None)] if proxy else [("直连", None)]
    proxy_err, last_err = "", ""
    for label, pxy in channels:
        for attempt in range(max(1, retries)):
            r = await uploader(content, filename, pxy)
            if r.get("ok"):
                return r
            last_err = f"{label}:{r.get('error', '')}"
            if label == "代理" and not proxy_err:
                proxy_err = last_err
            if attempt + 1 < retries:
                await asyncio.sleep(1.0)
    return {"ok": False, "error": proxy_err or last_err or "上传失败"}


async def upload_image(path: str, proxy: Optional[str] = None,
                       hosts: Optional[list] = None, retries: int = 2) -> dict:
    """上传单张图片：按 hosts 顺序依次尝试，第一个成功即返回（带 host 标记）。
    hosts 为图床名有序列表（默认 catbox→pixhost）。返回 {ok, img_url, bbcode, host, error}。"""
    p = Path(path)
    if not p.exists():
        return {"ok": False, "error": f"图片不存在: {path}"}
    try:
        content = await asyncio.to_thread(p.read_bytes)
    except Exception as e:
        return {"ok": False, "error": f"读取图片失败: {e}"}
    hosts = hosts or list(ALL_HOSTS)
    errs = []
    for host in hosts:
        up = _HOST_UPLOADERS.get(host)
        if not up:
            continue
        r = await _try_one_host(up, content, p.name, proxy, retries)
        if r.get("ok"):
            r["host"] = host
            return r
        errs.append(f"{host}:{r.get('error', '')}")
    return {"ok": False, "error": "；".join(errs) or "无可用图床"}


async def upload_images(paths: list, proxy: Optional[str] = None,
                        hosts: Optional[list] = None) -> dict:
    """批量上传（串行，避免被图床限速封 IP）。返回 {ok, results, bbcodes, error}。"""
    results, bbcodes = [], []
    for path in paths:
        r = await upload_image(path, proxy=proxy, hosts=hosts)
        results.append(r)
        if r.get("ok"):
            bbcodes.append(r["bbcode"])
    ok = len(bbcodes) > 0
    return {"ok": ok, "results": results, "bbcodes": bbcodes,
            "error": "" if ok else "全部图片上传失败"}


def build_gallery_bbcode(bbcodes: list, per_row: int = 3) -> str:
    """把多张图的 BBCode 拼成画廊文本（简单换行分组）。"""
    if not bbcodes:
        return ""
    lines, row = [], []
    for i, bb in enumerate(bbcodes, 1):
        row.append(bb)
        if i % max(1, per_row) == 0:
            lines.append("".join(row)); row = []
    if row:
        lines.append("".join(row))
    return "\n".join(lines)
