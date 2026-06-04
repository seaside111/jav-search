"""
qBittorrent WebUI 客户端（V1.4）
将磁力链 / .torrent 链接推送到群晖中部署的 qBittorrent。

WebUI API 文档：
https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-5.0)
鉴权：POST /api/v2/auth/login 取 SID Cookie；后续请求带上即可。
qB 会校验 Referer/Origin，需与 WebUI 地址同源，否则返回 403。
"""
from typing import Optional
import httpx


def _base(qb_url: str) -> str:
    return (qb_url or "").rstrip("/")


def _headers(qb_url: str) -> dict:
    base = _base(qb_url)
    return {
        "Referer": base,
        "Origin": base,
        "User-Agent": "Mozilla/5.0 (compatible; jav-search/1.4)",
    }


async def _fetch_torrent_bytes(url: str, timeout: int = 20) -> Optional[bytes]:
    """
    后端代取 .torrent 文件内容。
    用途：当 Jackett/索引器返回的种子直链是 http://localhost:9117/... 这类
    「仅 jav-search 后端可达」的地址时，直接把 URL 交给 qB 会因 qB 端解析不到
    localhost 而失败。改由后端（与 Jackett 同网/同机）取回字节再上传给 qB。
    """
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code == 200 and resp.content:
                return resp.content
    except Exception as e:
        print(f"[qB] 代取种子文件失败 {url[:80]}: {e}", flush=True)
    return None


async def _login(client: httpx.AsyncClient, qb_url: str,
                 username: str, password: str) -> tuple[bool, str]:
    """登录获取 SID Cookie。无密码（局域网白名单）时也允许直接通过。"""
    base = _base(qb_url)
    try:
        resp = await client.post(
            f"{base}/api/v2/auth/login",
            data={"username": username or "", "password": password or ""},
            headers=_headers(qb_url),
        )
    except Exception as e:
        return False, f"无法连接 qBittorrent: {e}"

    text = (resp.text or "").strip()
    if resp.status_code == 200 and text.lower() == "ok.":
        return True, "ok"
    if resp.status_code == 403:
        return False, "登录失败：IP 可能被 qB 临时封禁（多次密码错误），或未放行"
    if text.lower() == "fails.":
        return False, "登录失败：用户名或密码错误"
    # 部分配置开启了「对本地主机跳过身份验证」，登录接口可能直接放行
    if resp.status_code == 200:
        return True, "ok"
    return False, f"登录失败：HTTP {resp.status_code} {text[:80]}"


async def get_version(qb_url: str, username: str, password: str,
                      timeout: int = 10) -> dict:
    """检测连通性与登录，返回 qB 版本。"""
    if not qb_url:
        return {"online": False, "message": "未配置 qBittorrent 地址"}
    base = _base(qb_url)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        ok, msg = await _login(client, qb_url, username, password)
        if not ok:
            return {"online": False, "message": msg}
        try:
            resp = await client.get(f"{base}/api/v2/app/version",
                                    headers=_headers(qb_url))
            if resp.status_code == 200:
                return {"online": True, "version": resp.text.strip(),
                        "message": f"连接正常 · {resp.text.strip()}"}
            return {"online": False, "message": f"取版本失败 HTTP {resp.status_code}"}
        except Exception as e:
            return {"online": False, "message": str(e)}


async def add_torrent(
    qb_url: str,
    username: str,
    password: str,
    download_url: str,
    save_path: str = "",
    category: str = "",
    paused: bool = False,
    timeout: int = 20,
) -> dict:
    """
    推送一个磁力链或 .torrent 链接到 qBittorrent。

    :param download_url: magnet:?xt=... 或 http(s) 指向 .torrent 的链接
    :param save_path:    保存目录（qB 主机视角）；非空时关闭自动管理(autoTMM)使其生效
    :param category:     任务分类
    :param paused:       是否暂停加入（true 则加入后不自动开始）
    """
    if not qb_url:
        return {"success": False, "error": "未配置 qBittorrent 地址"}
    if not download_url:
        return {"success": False, "error": "下载链接为空"}

    base = _base(qb_url)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        ok, msg = await _login(client, qb_url, username, password)
        if not ok:
            return {"success": False, "error": msg}

        # 关闭自动种子管理，savepath 才会生效
        data = {
            "paused": "true" if paused else "false",
            "autoTMM": "false",
        }
        if save_path:
            data["savepath"] = save_path
        if category:
            data["category"] = category

        # 磁力链直接交给 qB；http(s) 种子直链先由后端代取文件再上传，
        # 规避 qB 端无法解析 localhost / 内网地址导致的下载失败。
        files = None
        if download_url.lower().startswith("magnet:"):
            data["urls"] = download_url
        else:
            content = await _fetch_torrent_bytes(download_url, timeout)
            if content:
                files = {"torrents": ("download.torrent", content,
                                      "application/x-bittorrent")}
            else:
                # 代取失败则退回让 qB 自行抓取（地址若不可达可能仍会失败）
                data["urls"] = download_url

        try:
            resp = await client.post(
                f"{base}/api/v2/torrents/add",
                data=data,
                files=files,
                headers=_headers(qb_url),
            )
        except Exception as e:
            return {"success": False, "error": f"推送请求失败: {e}"}

        text = (resp.text or "").strip()
        if resp.status_code == 200 and text.lower() == "ok.":
            return {"success": True, "message": "已推送到 qBittorrent"}
        if resp.status_code == 200:
            # 某些版本成功也返回空体
            return {"success": True, "message": "已推送到 qBittorrent"}
        if resp.status_code == 415:
            return {"success": False, "error": "qB 拒绝该种子（链接无效或不是种子）"}
        return {"success": False, "error": f"推送失败 HTTP {resp.status_code} {text[:80]}"}
