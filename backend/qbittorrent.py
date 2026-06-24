"""
qBittorrent WebUI 客户端（V1.4）
将磁力链 / .torrent 链接推送到群晖中部署的 qBittorrent。

WebUI API 文档：
https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-5.0)
鉴权：POST /api/v2/auth/login 取 SID Cookie；后续请求带上即可。
qB 会校验 Referer/Origin，需与 WebUI 地址同源，否则返回 403。
"""
from typing import Optional
from urllib.parse import quote
from pathlib import Path
import asyncio
import base64
import json
import os
import re
import time
import httpx


# 公共 BT tracker：JavDB 等站点的磁力链常是「只有 hash 无 tracker」的裸磁力，
# qB 仅靠 DHT 在群晖 NAT 下常找不到节点 → 一直卡在「下载元数据」。
# 推送前补上一批【当前可用】的公共 tracker，显著提升找到 peer/取到元数据的成功率。
#
# tracker 会随时间失效（用户实测有大半「未工作」）。故不再写死一份会腐烂的列表，而是：
#   优先抓取 ngosang/trackerslist 的「best」列表（按在线率自动维护、每日更新），
#   缓存到 /config/trackers_cache.json（TTL 1 天），抓取失败回退本地兜底列表，
#   用户也可在设置里自填覆盖。逻辑在 ensure_trackers_fresh / _current_trackers。
_FALLBACK_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://tracker-udp.gbitt.info:80/announce",
    "udp://explodie.org:6969/announce",
    "udp://opentracker.io:6969/announce",
    "udp://tracker.dler.org:6969/announce",
    "https://tracker.tamersunion.org:443/announce",
]

# best 列表的多个镜像，依次尝试（GitHub Pages / jsDelivr CDN / raw）
_REMOTE_TRACKER_SOURCES = [
    "https://ngosang.github.io/trackerslist/trackers_best.txt",
    "https://cdn.jsdelivr.net/gh/ngosang/trackerslist@master/trackers_best.txt",
    "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt",
]
_TRACKERS_TTL = 604800  # 缓存 7 天（tracker 大多稳定、无需频繁更新）
# 运行期内存态：source ∈ {fallback, file, remote, user}
_trackers_state = {"ts": 0.0, "list": list(_FALLBACK_TRACKERS), "source": "fallback"}


def _trackers_file() -> Path:
    return Path(os.getenv("CONFIG_DIR", "/config")) / "trackers_cache.json"


def _read_trackers_file():
    """读取上次成功抓取的缓存（ts, list）；无则 (0, [])。不判 TTL。"""
    try:
        p = _trackers_file()
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            lst = d.get("list") or []
            if lst:
                return float(d.get("ts", 0)), list(lst)
    except Exception:
        pass
    return 0.0, []


def _parse_trackers(text: str) -> list:
    """从文本/多行/逗号分隔里挑出 tracker URL，去重保序。"""
    out = []
    for line in re.split(r"[\r\n,]+", text or ""):
        s = line.strip()
        if s and "://" in s and s not in out:
            out.append(s)
    return out


def _current_trackers() -> list:
    return _trackers_state["list"] or list(_FALLBACK_TRACKERS)


async def _fetch_remote_trackers(proxy: Optional[str]):
    for url in _REMOTE_TRACKER_SOURCES:
        try:
            async with httpx.AsyncClient(proxy=proxy, timeout=15,
                                         follow_redirects=True) as c:
                r = await c.get(url)
            if r.status_code == 200:
                lst = _parse_trackers(r.text)
                if lst:
                    return lst, url
        except Exception:
            continue
    return [], ""


async def ensure_trackers_fresh(config: dict, force: bool = False) -> dict:
    """
    确保内存里的公共 tracker 列表是新的（_augment_magnet 用 _current_trackers 读它）。
    优先级：用户自填覆盖 > 远程 best 列表（缓存 TTL 内复用文件/内存）> 本地兜底。
    每次加种前调用，TTL 内零成本直接返回，不会每次都抓网络。
    """
    config = config or {}
    # 1) 用户自填覆盖（最高优先；清空则回到自动逻辑）
    override = _parse_trackers(config.get("public_trackers", ""))
    if override:
        _trackers_state.update(ts=time.time(), list=override, source="user")
        return _trackers_state

    now = time.time()
    # 2) TTL 内且已是远程/文件来源 → 直接复用
    if (not force and _trackers_state["source"] in ("remote", "file")
            and now - _trackers_state["ts"] < _TRACKERS_TTL):
        return _trackers_state

    # 关闭自动更新 → 用兜底
    if not config.get("public_trackers_auto_update", True):
        _trackers_state.update(list=list(_FALLBACK_TRACKERS), source="fallback")
        return _trackers_state

    # 3) 文件缓存仍在 TTL 内 → 直接用，免抓
    if not force:
        fts, flst = _read_trackers_file()
        if flst and now - fts < _TRACKERS_TTL:
            _trackers_state.update(ts=fts, list=flst, source="file")
            return _trackers_state

    # 4) 远程抓取 best 列表
    lst, src = await _fetch_remote_trackers(config.get("proxy") or None)
    if lst:
        _trackers_state.update(ts=now, list=lst, source="remote")
        try:
            p = _trackers_file()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"ts": now, "list": lst, "src": src},
                                    ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        return _trackers_state

    # 5) 远程全失败（抓取服务都不通）：用任何留存的旧缓存（过期也一直可用），
    #    再不行才用内置兜底——保证「有几个能用的就能跑」，绝不因更新失败而无 tracker。
    fts, flst = _read_trackers_file()
    if flst:
        _trackers_state.update(ts=fts, list=flst, source="file")
    elif not _trackers_state["list"] or _trackers_state["source"] == "remote":
        _trackers_state.update(list=list(_FALLBACK_TRACKERS), source="fallback")
    return _trackers_state


async def refresh_trackers(config: dict) -> dict:
    """手动刷新（设置页按钮用）。返回 {ok, count, source, trackers}。"""
    st = await ensure_trackers_fresh(config, force=True)
    src_label = {"remote": "在线 best 列表", "file": "本地缓存",
                 "user": "用户自填", "fallback": "内置兜底"}.get(st["source"], st["source"])
    return {"ok": True, "count": len(st["list"]), "source": st["source"],
            "source_label": src_label, "trackers": st["list"]}


def _augment_magnet(magnet: str) -> str:
    """给磁力链补上公共 tracker（已存在的不重复添加）。非磁力链原样返回。"""
    if not magnet or not magnet.lower().startswith("magnet:"):
        return magnet
    low = magnet.lower()
    parts = []
    for tr in _current_trackers():
        enc = quote(tr, safe="")
        if enc.lower() in low or tr.lower() in low:
            continue
        parts.append("&tr=" + enc)
    return magnet + "".join(parts)


def _infohash_from_magnet(url: str) -> str:
    """从磁力链解析 infohash（40位十六进制原样小写；32位 base32 转十六进制）。非磁力返回空。"""
    m = re.search(r"xt=urn:btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})", url or "")
    if not m:
        return ""
    val = m.group(1)
    if len(val) == 40:
        return val.lower()
    try:
        return base64.b32decode(val.upper()).hex()
    except Exception:
        return ""


async def _post_upload_limit(client, qb_url: str, infohash: str, limit_bytes: int) -> bool:
    """调用 setUploadLimit 设单种上传限速（qB 用字节/秒）。返回接口是否回 200。
    注意：qB 对【不存在/未注册的 hash 也会回 200】，因此不能只看这里的返回判定成功，
    必须再用 _get_upload_limit 回查确认限速真正落到了种子句柄上。"""
    if not infohash or limit_bytes <= 0:
        return False
    try:
        resp = await client.post(
            f"{_base(qb_url)}/api/v2/torrents/setUploadLimit",
            data={"hashes": infohash.lower(), "limit": str(int(limit_bytes))},
            headers=_headers(qb_url),
        )
        return resp.status_code == 200
    except Exception:
        return False


async def _get_upload_limit(client, qb_url: str, infohash: str) -> int:
    """回查指定种子当前的单种上传限速（字节/秒）。未知 hash 返回 -1（区别于 0=不限）。"""
    if not infohash:
        return -1
    try:
        resp = await client.post(
            f"{_base(qb_url)}/api/v2/torrents/uploadLimit",
            data={"hashes": infohash.lower()},
            headers=_headers(qb_url),
        )
        if resp.status_code != 200:
            return -1
        data = resp.json()
        if isinstance(data, dict):
            # 返回 {hash: limit_bytes}；hash 不存在时该键缺失
            val = data.get(infohash.lower())
            return int(val) if val is not None else -1
    except Exception:
        pass
    return -1


async def _ensure_upload_limit(client, qb_url: str, infohash: str, kbps: int,
                               tries: int = 6, delay: float = 1.0) -> bool:
    """设单种上传限速并【回查确认真正生效】。
    磁力 add 时 upLimit 常被忽略（彼时无元数据），add 后立刻 setUploadLimit 也可能因
    种子尚未完全注册而落空——且 qB 对未知 hash 仍回 200，光看返回会误判成功。
    故这里反复「设置→读回比对」，直到读回值等于目标值或重试用尽。"""
    if not infohash or kbps <= 0:
        return False
    target = int(kbps) * 1024
    for _ in range(tries):
        await _post_upload_limit(client, qb_url, infohash, target)
        if await _get_upload_limit(client, qb_url, infohash) == target:
            return True
        await asyncio.sleep(delay)
    return False


async def _list_hashes(client, qb_url: str) -> set:
    """取 qB 当前全部种子的 infohash 集合（小写）。失败返回空集。"""
    try:
        resp = await client.get(f"{_base(qb_url)}/api/v2/torrents/info",
                                headers=_headers(qb_url))
        if resp.status_code == 200:
            data = resp.json()
            return {(t.get("hash") or "").lower() for t in data if isinstance(t, dict)}
    except Exception:
        pass
    return set()


async def _resolve_added_hash(client, qb_url: str, pre_hashes: set,
                              magnet_ih: str, tries: int = 8,
                              delay: float = 1.0) -> str:
    """取 qB 实际入库的新种子 infohash：用 add 前后列表差集（最权威，能扛 v2/混合种、
    base32 磁力导致的 btih 与实际 hash 不一致）。多个新增时优先与磁力 btih 吻合的那个；
    始终拿不到则兜底返回磁力 btih。"""
    magnet_ih = (magnet_ih or "").lower()
    # 磁力本就已在 qB（重复添加）：差集永远为空，无需空等，直接用其 btih。
    if magnet_ih and magnet_ih in pre_hashes:
        return magnet_ih
    for _ in range(tries):
        cur = await _list_hashes(client, qb_url)
        new = [h for h in cur if h not in pre_hashes]
        if new:
            if magnet_ih and magnet_ih in new:
                return magnet_ih
            return new[0]
        await asyncio.sleep(delay)
    return magnet_ih


async def _add_trackers(client, qb_url: str, infohash: str, trackers: list) -> None:
    """给已入库的种子补一批公共 tracker（addTrackers，urls 用换行分隔）。失败静默。"""
    if not infohash or not trackers:
        return
    try:
        await client.post(f"{_base(qb_url)}/api/v2/torrents/addTrackers",
                          data={"hash": infohash.lower(), "urls": "\n".join(trackers)},
                          headers=_headers(qb_url))
    except Exception:
        pass


async def _reannounce(client, qb_url: str, infohash: str) -> None:
    """强制该种子立即向 tracker 重新汇报（reannounce）。失败静默。"""
    if not infohash:
        return
    try:
        await client.post(f"{_base(qb_url)}/api/v2/torrents/reannounce",
                          data={"hashes": infohash.lower()},
                          headers=_headers(qb_url))
    except Exception:
        pass


# 持有后台 reannounce 任务的强引用，避免任务被 GC 提前回收（asyncio 已知坑）。
_BG_TASKS: set = set()


def _spawn_reannounce(qb_url: str, username: str, password: str, infohash: str,
                      delays=(5, 20)) -> None:
    """后台延迟多次强制 reannounce：新加种子 qB 有时迟迟不 announce，私有站会卡在
    「tracker 工作中却连不到 peer」，须手动强制汇报后 peer 才回来。这里替用户在加种后
    隔几秒自动汇报几次（用独立短连接，不阻塞 add 的响应）。"""
    if not infohash:
        return
    async def _runner():
        for d in delays:
            await asyncio.sleep(d)
            try:
                async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
                    ok, _ = await _login(c, qb_url, username, password)
                    if ok:
                        await _reannounce(c, qb_url, infohash)
            except Exception:
                pass
    try:
        task = asyncio.get_event_loop().create_task(_runner())
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)
    except Exception:
        pass


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
    # 登录成功的几种返回：
    #  - 老版本：200 + 响应体 "Ok."
    #  - 新版本 / 开启「对本地主机跳过身份验证」：204 无内容，仅下发 QBT_SID Cookie
    #  - 部分配置：200 空体
    # httpx 会自动把 Set-Cookie 存进 client.cookies，后续请求自动带上，故此处直接放行。
    if resp.status_code in (200, 204):
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


async def list_torrents_ex(qb_url: str, username: str, password: str,
                           timeout: int = 15) -> dict:
    """
    列出 qB 所有种子，并【明确区分「下载器不可达」与「确实没有种子」】。
    返回 {"reachable": bool, "torrents": [...], "error": str}。
      - reachable=True  且 torrents=[]  → 下载器在线、确实空（可据此判断「种子已被删除」）；
      - reachable=False                  → 登录失败/超时/HTTP 异常（qB 假死/重启/网络），
        此时 torrents 必为 []，调用方【绝不能】据此判定种子消失或删除成功。
    这是抗「qB 假死」的基石：以前无论哪种情况都回 []，无法分辨，
    导致回查删除/做种消失检测把「连不上」误判成「没了」。
    """
    if not qb_url:
        return {"reachable": False, "torrents": [], "error": "未配置 qBittorrent 地址"}
    base = _base(qb_url)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            ok, msg = await _login(client, qb_url, username, password)
            if not ok:
                return {"reachable": False, "torrents": [], "error": msg}
            resp = await client.get(f"{base}/api/v2/torrents/info",
                                    headers=_headers(qb_url))
            if resp.status_code != 200:
                return {"reachable": False, "torrents": [],
                        "error": f"列种子 HTTP {resp.status_code}"}
            data = resp.json()
            if not isinstance(data, list):
                return {"reachable": False, "torrents": [], "error": "列种子返回非预期格式"}
            # 统一字段（与 transmission.list_torrents 对齐，供辅种比对/种子管理共用）
            torrents = [{
                "hash": (t.get("hash") or "").lower(),
                "name": t.get("name", "") or "",
                "content_path": t.get("content_path", "") or "",
                "save_path": t.get("save_path", "") or "",
                "ratio": float(t.get("ratio") or 0.0),
                "seeding_time": int(t.get("seeding_time") or 0),
                "progress": float(t.get("progress") or 0.0),
                "state": t.get("state", "") or "",
                "category": t.get("category", "") or "",
                "upspeed": int(t.get("upspeed") or 0),       # 上传速度 字节/s
                "uploaded": int(t.get("uploaded") or 0),     # 累计上传 字节
                "dlspeed": int(t.get("dlspeed") or 0),
                "size": int(t.get("size") or 0),
            } for t in data if isinstance(t, dict)]
            return {"reachable": True, "torrents": torrents, "error": ""}
    except Exception as e:
        print(f"[qB] 列种子失败: {e}", flush=True)
        return {"reachable": False, "torrents": [], "error": str(e)}


async def list_torrents(qb_url: str, username: str, password: str,
                        timeout: int = 15) -> list:
    """（兼容旧接口）只取种子列表；无法区分「不可达」与「空」时一律回 []。
    需要区分可达性的场景请改用 list_torrents_ex。"""
    res = await list_torrents_ex(qb_url, username, password, timeout)
    return res["torrents"]


async def add_torrent(
    qb_url: str,
    username: str,
    password: str,
    download_url: str = "",
    torrent_bytes: Optional[bytes] = None,
    save_path: str = "",
    category: str = "",
    paused: bool = False,
    skip_checking: bool = False,
    upload_limit_kbps: int = 0,
    reannounce: bool = True,
    public_trackers: bool = True,
    timeout: int = 20,
) -> dict:
    """
    推送一个磁力链或 .torrent 到 qBittorrent。

    :param download_url:  magnet:?xt=... 或 http(s) 指向 .torrent 的链接
    :param torrent_bytes: 已取到的 .torrent 字节（辅种用，优先于 download_url）
    :param save_path:     保存目录（qB 主机视角）；非空时关闭自动管理(autoTMM)使其生效
    :param category:      任务分类
    :param paused:        是否暂停加入（true 则加入后不自动开始）
    :param skip_checking: 是否跳过哈希校验（辅种通常需 false 以触发校验确认数据吻合）
    :param public_trackers: 是否补公共 tracker（磁力补进 URL、种子加种后补）。
                            发种取回的官方 PT 种子须传 False，绝不混入公共 tracker。
    """
    if not qb_url:
        return {"success": False, "error": "未配置 qBittorrent 地址"}
    if not download_url and not torrent_bytes:
        return {"success": False, "error": "下载链接为空"}

    base = _base(qb_url)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        ok, msg = await _login(client, qb_url, username, password)
        if not ok:
            return {"success": False, "error": msg}

        # 加种前快照现有 hash，供 add 后用差集拿到 qB 实际入库的真实 hash
        # （限速回查、tracker 重新汇报、返回 hash 给上层都依赖它）。
        pre_hashes = await _list_hashes(client, qb_url)

        # 关闭自动种子管理，savepath 才会生效
        data = {
            "paused": "true" if paused else "false",
            "autoTMM": "false",
        }
        if save_path:
            data["savepath"] = save_path
        if category:
            data["category"] = category
        if skip_checking:
            data["skip_checking"] = "true"
        if upload_limit_kbps and upload_limit_kbps > 0:
            data["upLimit"] = str(int(upload_limit_kbps) * 1024)  # qB 用字节/秒

        # 优先用已取到的字节（辅种）；否则按链接类型处理：
        # 磁力链直接交给 qB；http(s) 种子直链先由后端代取文件再上传，
        # 规避 qB 端无法解析 localhost / 内网地址导致的下载失败。
        files = None
        if torrent_bytes:
            files = {"torrents": ("download.torrent", torrent_bytes,
                                  "application/x-bittorrent")}
        elif download_url.lower().startswith("magnet:"):
            # 裸磁力补 tracker，避免 qB 卡在「下载元数据」（官方 PT 种子 public_trackers=False 不补）
            data["urls"] = _augment_magnet(download_url) if public_trackers else download_url
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
        if resp.status_code == 415:
            return {"success": False, "error": "qB 拒绝该种子（链接无效或不是种子）"}
        if resp.status_code != 200:
            return {"success": False, "error": f"推送失败 HTTP {resp.status_code} {text[:80]}"}
        # 200（"Ok." 或个别版本空体）即视为成功。

        # 拿到 qB 实际入库的真实 hash（add 前后差集，扛 v2/base32 磁力的 btih 不一致）。
        # 限速回查、tracker 重新汇报、返回给上层都基于它。
        magnet_ih = _infohash_from_magnet(download_url) if download_url else ""
        real_ih = await _resolve_added_hash(client, qb_url, pre_hashes, magnet_ih)

        # 常规 .torrent（非磁力）下载也补公共 tracker：磁力已在 URL 里补过，
        # 种子则加种后用 addTrackers 接口补上，与磁力统一。官方 PT 种子 public_trackers=False 跳过。
        was_magnet = bool(download_url and download_url.lower().startswith("magnet:"))
        if public_trackers and real_ih and not was_magnet:
            await _add_trackers(client, qb_url, real_ih, _current_trackers())

        # 单种上传限速：add 时 upLimit 对磁力常被忽略（彼时无元数据），且 qB 对未知 hash
        # 也回 200——必须 add 后显式设置并【回查确认】真正落到种子句柄上才算数。
        if upload_limit_kbps and upload_limit_kbps > 0 and real_ih:
            ok_lim = await _ensure_upload_limit(client, qb_url, real_ih, upload_limit_kbps)
            if not ok_lim:
                print(f"[qB] 单种限速未能确认生效 hash={real_ih[:12]} "
                      f"目标={upload_limit_kbps}KB/s", flush=True)

        # 强制 tracker 立即汇报：先就地汇报一次，再后台延迟补汇报几次，
        # 规避新加种子卡在「工作中却无 peer」、须手动强制汇报才回来的问题。
        if reannounce and real_ih:
            await _reannounce(client, qb_url, real_ih)
            _spawn_reannounce(qb_url, username, password, real_ih)

        return {"success": True, "message": "已推送到 qBittorrent", "hash": real_ih}


async def delete_torrents(qb_url: str, username: str, password: str,
                          hashes: list, delete_files: bool = False,
                          timeout: int = 20) -> dict:
    """删除种子（可选连同数据）。供种子管理使用，与 transmission.delete_torrents 对齐。"""
    if not qb_url:
        return {"success": False, "error": "未配置 qBittorrent 地址"}
    if not hashes:
        return {"success": True, "message": "无种子可删"}
    base = _base(qb_url)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        ok, msg = await _login(client, qb_url, username, password)
        if not ok:
            return {"success": False, "error": msg}
        try:
            resp = await client.post(
                f"{base}/api/v2/torrents/delete",
                data={"hashes": "|".join(h.lower() for h in hashes),
                      "deleteFiles": "true" if delete_files else "false"},
                headers=_headers(qb_url),
            )
        except Exception as e:
            return {"success": False, "error": f"删除请求失败: {e}"}
        if resp.status_code == 200:
            return {"success": True, "message": f"已删除 {len(hashes)} 个种子"}
        return {"success": False, "error": f"删除失败 HTTP {resp.status_code}"}
