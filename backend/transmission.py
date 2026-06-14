"""
Transmission RPC 客户端（V1.5）
与 qbittorrent.py 平级，作为可选下载器后端，由 downloader.py 统一调度。

RPC 文档：
https://github.com/transmission/transmission/blob/main/docs/rpc-spec.md
要点：
  - 入口固定为 {base}/transmission/rpc，POST JSON：{method, arguments}
  - 鉴权用 HTTP Basic（用户名/密码，可空）
  - 防 CSRF：首次请求返回 409 并在响应头给出 X-Transmission-Session-Id，
    需带上该头重发；该 id 在会话内复用，过期会再次 409，自动重取即可。
  - 「分类」概念用 labels（字符串数组，RPC v16+）；保存目录用 download-dir。
"""
import base64
from typing import Optional
import httpx

# 复用 qbittorrent 里那套公共 tracker 补全逻辑，避免裸磁力卡在「下载元数据」。
from qbittorrent import _augment_magnet, _fetch_torrent_bytes

_SESSION_HEADER = "X-Transmission-Session-Id"


def _rpc_url(tr_url: str) -> str:
    """把用户填的地址规整成 RPC 端点。
    填 http://host:9091 → 自动补 /transmission/rpc；已含 /rpc 则原样用。"""
    base = (tr_url or "").rstrip("/")
    if not base:
        return ""
    if base.endswith("/rpc") or base.endswith("/transmission/rpc"):
        return base
    return base + "/transmission/rpc"


async def _rpc(
    client: httpx.AsyncClient,
    url: str,
    auth: Optional[tuple],
    session_id: str,
    method: str,
    arguments: dict,
) -> tuple[Optional[dict], str, str]:
    """
    发一次 RPC 调用。返回 (响应json, 最新session_id, 错误信息)。
    自动处理 409 重取 session id 后重发一次。
    """
    body = {"method": method, "arguments": arguments}
    headers = {_SESSION_HEADER: session_id} if session_id else {}
    for _attempt in range(2):
        try:
            resp = await client.post(url, json=body, headers=headers, auth=auth)
        except Exception as e:
            return None, session_id, f"无法连接 Transmission: {e}"
        if resp.status_code == 409:
            # 取新的 session id 后重试
            session_id = resp.headers.get(_SESSION_HEADER, "")
            headers = {_SESSION_HEADER: session_id} if session_id else {}
            continue
        if resp.status_code == 401:
            return None, session_id, "鉴权失败：用户名或密码错误"
        if resp.status_code != 200:
            return None, session_id, f"RPC HTTP {resp.status_code}"
        try:
            return resp.json(), session_id, ""
        except Exception as e:
            return None, session_id, f"RPC 响应解析失败: {e}"
    return None, session_id, "RPC 多次 409，无法取得 session id"


def _auth(username: str, password: str) -> Optional[tuple]:
    if username or password:
        return (username or "", password or "")
    return None


async def get_version(tr_url: str, username: str, password: str,
                      timeout: int = 10) -> dict:
    """检测连通性与登录，返回 Transmission 版本。"""
    url = _rpc_url(tr_url)
    if not url:
        return {"online": False, "message": "未配置 Transmission 地址"}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        data, _sid, err = await _rpc(client, url, _auth(username, password), "",
                                     "session-get", {})
        if err:
            return {"online": False, "message": err}
        if (data or {}).get("result") == "success":
            ver = (data.get("arguments") or {}).get("version", "")
            return {"online": True, "version": ver,
                    "message": f"连接正常 · Transmission {ver}"}
        return {"online": False, "message": f"RPC 返回：{(data or {}).get('result', '未知')}"}


async def list_torrents(tr_url: str, username: str, password: str,
                        timeout: int = 15) -> list:
    """
    列出全部种子的关键信息（供辅种比对 / 种子管理使用）。
    返回 [{hash, name, content_path, save_path, ratio, seeding_time, progress, state, category}]。
    """
    url = _rpc_url(tr_url)
    if not url:
        return []
    fields = ["hashString", "name", "downloadDir", "uploadRatio",
              "secondsSeeding", "percentDone", "status", "labels",
              "rateUpload", "uploadedEver", "rateDownload", "totalSize"]
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        data, _sid, err = await _rpc(client, url, _auth(username, password), "",
                                     "torrent-get", {"fields": fields})
        if err or not data:
            if err:
                print(f"[TR] 列种子失败: {err}", flush=True)
            return []
        out = []
        for t in (data.get("arguments") or {}).get("torrents", []):
            if not isinstance(t, dict):
                continue
            labels = t.get("labels") or []
            out.append({
                "hash": (t.get("hashString") or "").lower(),
                "name": t.get("name", "") or "",
                "content_path": "",
                "save_path": t.get("downloadDir", "") or "",
                "ratio": float(t.get("uploadRatio") or 0.0),
                "seeding_time": int(t.get("secondsSeeding") or 0),
                "progress": float(t.get("percentDone") or 0.0),
                "state": t.get("status"),
                "category": labels[0] if labels else "",
                "upspeed": int(t.get("rateUpload") or 0),
                "uploaded": int(t.get("uploadedEver") or 0),
                "dlspeed": int(t.get("rateDownload") or 0),
                "size": int(t.get("totalSize") or 0),
            })
        return out


async def add_torrent(
    tr_url: str,
    username: str,
    password: str,
    download_url: str = "",
    torrent_bytes: Optional[bytes] = None,
    save_path: str = "",
    category: str = "",
    paused: bool = False,
    skip_checking: bool = False,  # Transmission 不支持跳过校验，留参保持接口一致
    upload_limit_kbps: int = 0,
    reannounce: bool = True,
    timeout: int = 30,
) -> dict:
    """
    添加一个种子到 Transmission。
    可传 download_url（磁力/种子直链）或 torrent_bytes（已取到的 .torrent 字节，辅种用）。
    """
    url = _rpc_url(tr_url)
    if not url:
        return {"success": False, "error": "未配置 Transmission 地址"}

    args: dict = {"paused": bool(paused)}
    if save_path:
        args["download-dir"] = save_path
    if category:
        args["labels"] = [category]
    if upload_limit_kbps and upload_limit_kbps > 0:
        args["uploadLimited"] = True
        args["uploadLimit"] = int(upload_limit_kbps)  # TR 用 KB/s

    # 决定用 metainfo（字节）还是 filename（链接）
    if torrent_bytes:
        args["metainfo"] = base64.b64encode(torrent_bytes).decode("ascii")
    elif download_url and download_url.lower().startswith("magnet:"):
        args["filename"] = _augment_magnet(download_url)
    elif download_url:
        # http(s) 种子直链：先后端代取字节，规避 TR 端无法解析 localhost/内网地址
        content = await _fetch_torrent_bytes(download_url, timeout)
        if content:
            args["metainfo"] = base64.b64encode(content).decode("ascii")
        else:
            args["filename"] = download_url
    else:
        return {"success": False, "error": "下载链接为空"}

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        data, sid, err = await _rpc(client, url, _auth(username, password), "",
                                    "torrent-add", args)
        if err:
            return {"success": False, "error": err}
        result = (data or {}).get("result", "")
        if result != "success":
            return {"success": False, "error": f"添加失败：{result}"}
        a = (data.get("arguments") or {})
        if "torrent-duplicate" in a:
            dup = a["torrent-duplicate"]
            ih = (dup.get("hashString") or "").lower()
            if reannounce and ih:
                await _rpc(client, url, _auth(username, password), sid,
                           "torrent-reannounce", {"ids": [ih]})
            return {"success": True, "message": "种子已存在于 Transmission（重复）",
                    "hash": ih, "duplicate": True}
        added = a.get("torrent-added") or {}
        ih = (added.get("hashString") or "").lower()
        # 强制 tracker 立即汇报，规避新加种子卡在「工作中却无 peer」。
        if reannounce and ih:
            await _rpc(client, url, _auth(username, password), sid,
                       "torrent-reannounce", {"ids": [ih]})
        return {"success": True, "message": "已添加到 Transmission", "hash": ih}


async def delete_torrents(tr_url: str, username: str, password: str,
                          hashes: list, delete_files: bool = False,
                          timeout: int = 20) -> dict:
    """删除种子（可选连同数据）。供种子管理使用。"""
    url = _rpc_url(tr_url)
    if not url:
        return {"success": False, "error": "未配置 Transmission 地址"}
    if not hashes:
        return {"success": True, "message": "无种子可删"}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        data, _sid, err = await _rpc(
            client, url, _auth(username, password), "", "torrent-remove",
            {"ids": [h.lower() for h in hashes], "delete-local-data": bool(delete_files)},
        )
        if err:
            return {"success": False, "error": err}
        if (data or {}).get("result") == "success":
            return {"success": True, "message": f"已删除 {len(hashes)} 个种子"}
        return {"success": False, "error": f"删除失败：{(data or {}).get('result', '未知')}"}
