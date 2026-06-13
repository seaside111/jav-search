"""
M-Team（馒头）PT 站 API 客户端（V1.5）

仅覆盖辅种/读取侧（发种 createOredit 放 Phase 4）：
  search / detail / files / genDlToken / 下载 .torrent / 连通诊断

事实依据（已验证）：
  - Base：可配 mteam_api_base，开发期用测试站 https://test2.m-team.cc/api
  - 鉴权：请求头 x-api-key: <密钥>
  - POST /torrent/search    JSON  {keyword, mode, pageNumber, pageSize}
  - POST /torrent/detail    form  {id}
  - POST /torrent/files     form  {id}
  - POST /torrent/genDlToken form {id} → {message:"SUCCESS", data:"<下载URL>"} → GET 取 .torrent

响应统一包在 Result 里：{code, message, data:{...}}。规范未细化 data 字段，
故以下解析全部走防御式（缺字段不报错），辅种最终以下载器重校验为准。
"""
from typing import Optional
import httpx

import logbus

# AV 番号检索默认走成人区
DEFAULT_MODE = "adult"
_UA = "Mozilla/5.0 (compatible; jav-search/1.5; m-team-client)"


def _base(config: dict) -> str:
    return (config.get("mteam_api_base") or "https://test2.m-team.cc/api").rstrip("/")


def _api_key(config: dict) -> str:
    return (config.get("mteam_api_key") or "").strip()


def _headers(config: dict, extra: Optional[dict] = None) -> dict:
    h = {
        "x-api-key": _api_key(config),
        "User-Agent": _UA,
        "Accept": "application/json, */*",
    }
    if extra:
        h.update(extra)
    return h


def _proxy(config: dict) -> Optional[str]:
    # M-Team 一般直连可达；如需出网代理可复用主代理。默认不走代理（站点常封代理 IP）。
    return None


def _is_ok(payload: dict) -> bool:
    """判断 Result 是否成功。message=SUCCESS 或 code in (0,'0','200')。"""
    if not isinstance(payload, dict):
        return False
    msg = str(payload.get("message", "")).upper()
    code = str(payload.get("code", ""))
    return msg == "SUCCESS" or code in ("0", "200")


def _looks_mediatype_err(status: int, text: str) -> bool:
    """判断是否为 Content-Type/参数不匹配类错误（用于自动换类型重试）。"""
    if status == 415:
        return True
    t = text or ""
    return any(kw in t for kw in (
        "媒體類型", "媒体类型", "media type", "Media Type", "Content-Type",
        "参数错误", "參數錯誤", "Unsupported"))


async def _send(config: dict, url: str, mode: str, body, timeout: int):
    """按 mode（json/form）发一次。返回 httpx.Response。"""
    if mode == "form":
        headers = _headers(config, {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
        kwargs = {"data": body if body is not None else {}}
    else:
        headers = _headers(config)
        kwargs = {"json": body if body is not None else {}}
    async with httpx.AsyncClient(proxy=_proxy(config), timeout=timeout,
                                 follow_redirects=True) as client:
        return await client.post(url, headers=headers, **kwargs)


async def _post(config: dict, path: str, *, json_body: Optional[dict] = None,
                form: Optional[dict] = None, timeout: int = 20) -> dict:
    """
    统一 POST。返回 {ok, payload, error}。
    自动兜底：若首选 Content-Type 被判「媒体类型/参数不匹配」，自动换另一种重试一次
    （各端点要的类型不一，避免逐个硬编码猜错）。
    """
    if not _api_key(config):
        return {"ok": False, "error": "未配置 M-Team API 密钥"}
    url = _base(config) + path
    primary = "form" if form is not None else "json"
    body = form if form is not None else json_body
    order = [primary, "json" if primary == "form" else "form"]
    logbus.debug("M-Team", f"POST {path} [{primary}] body={body}")

    last_err = ""
    for i, mode in enumerate(order):
        try:
            resp = await _send(config, url, mode, body, timeout)
        except Exception as e:
            logbus.debug("M-Team", f"POST {path} [{mode}] 异常: {type(e).__name__}: {e}")
            return {"ok": False, "error": f"请求失败: {type(e).__name__}: {e}"}
        logbus.debug("M-Team", f"POST {path} [{mode}] → HTTP {resp.status_code}")

        if resp.status_code in (401, 403):
            return {"ok": False, "error": f"鉴权失败 HTTP {resp.status_code}（API 密钥无效或权限不足）"}
        if resp.status_code != 200:
            last_err = f"HTTP {resp.status_code}: {resp.text[:160]}"
            if i == 0 and _looks_mediatype_err(resp.status_code, resp.text):
                logbus.debug("M-Team", f"POST {path} 媒体类型不匹配，改用另一种类型重试")
                continue
            return {"ok": False, "error": last_err}

        try:
            payload = resp.json()
        except Exception:
            return {"ok": False, "error": f"响应非 JSON: {resp.text[:160]}"}
        if not _is_ok(payload):
            msg = str(payload.get("message", payload))
            # 业务层 200 但提示媒体类型/参数错误 → 换类型再试一次
            if i == 0 and _looks_mediatype_err(200, msg):
                logbus.debug("M-Team", f"POST {path} 返回「{msg}」，改用另一种类型重试")
                last_err = f"接口返回失败: {msg}"
                continue
            return {"ok": False, "error": f"接口返回失败: {msg}", "payload": payload}
        return {"ok": True, "payload": payload}

    return {"ok": False, "error": last_err or "请求失败"}


def _norm_item(t: dict) -> dict:
    """把一条种子记录规整成统一字段（缺字段安全）。"""
    if not isinstance(t, dict):
        return {}
    status = t.get("status") or {}
    try:
        size = int(t.get("size") or 0)
    except (ValueError, TypeError):
        size = 0
    return {
        "id": str(t.get("id") or ""),
        "name": t.get("name") or "",
        "small_descr": t.get("smallDescr") or "",
        "size": size,
        "category": str(t.get("category") or ""),
        "seeders": str(status.get("seeders") or t.get("seeders") or ""),
        "leechers": str(status.get("leechers") or t.get("leechers") or ""),
        "discount": status.get("discount") or "",
        "created_date": t.get("createdDate") or "",
        "imdb": t.get("imdb") or "",
        "raw": t,
    }


async def search(config: dict, keyword: str, mode: str = DEFAULT_MODE,
                 page_number: int = 1, page_size: int = 100) -> dict:
    """搜种。返回 {ok, items:[规整记录], total, error}。"""
    res = await _post(config, "/torrent/search", json_body={
        "keyword": keyword,
        "mode": mode or DEFAULT_MODE,
        "pageNumber": page_number,
        "pageSize": page_size,
    })
    if not res["ok"]:
        return {"ok": False, "items": [], "total": 0, "error": res["error"]}
    data = (res["payload"] or {}).get("data") or {}
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        rows = []
    items = [_norm_item(t) for t in rows]
    try:
        total = int(data.get("total") or len(items)) if isinstance(data, dict) else len(items)
    except (ValueError, TypeError):
        total = len(items)
    return {"ok": True, "items": items, "total": total, "error": ""}


async def detail(config: dict, torrent_id: str) -> dict:
    """取种子详情。返回 {ok, item, error}。"""
    res = await _post(config, "/torrent/detail", form={"id": str(torrent_id)})
    if not res["ok"]:
        return {"ok": False, "item": {}, "error": res["error"]}
    data = (res["payload"] or {}).get("data") or {}
    return {"ok": True, "item": _norm_item(data), "error": ""}


async def files(config: dict, torrent_id: str) -> dict:
    """列种子内文件。返回 {ok, files:[{name,size}], error}（字段为最佳努力解析）。"""
    res = await _post(config, "/torrent/files", form={"id": str(torrent_id)})
    if not res["ok"]:
        return {"ok": False, "files": [], "error": res["error"]}
    data = (res["payload"] or {}).get("data") or []
    rows = data if isinstance(data, list) else (data.get("data") if isinstance(data, dict) else [])
    out = []
    for f in rows or []:
        if not isinstance(f, dict):
            continue
        try:
            sz = int(f.get("size") or 0)
        except (ValueError, TypeError):
            sz = 0
        out.append({"name": f.get("name") or f.get("fileName") or "", "size": sz})
    return {"ok": True, "files": out, "error": ""}


async def gen_dl_token(config: dict, torrent_id: str) -> dict:
    """取下载 URL。返回 {ok, url, error}。"""
    res = await _post(config, "/torrent/genDlToken",
                      form={"id": str(torrent_id)}, timeout=20)
    if not res["ok"]:
        return {"ok": False, "url": "", "error": res["error"]}
    url = (res["payload"] or {}).get("data") or ""
    if not url or not isinstance(url, str):
        return {"ok": False, "url": "", "error": "未取得下载链接"}
    return {"ok": True, "url": url, "error": ""}


async def fetch_torrent(config: dict, torrent_id: str) -> dict:
    """genDlToken → GET 下载 .torrent 字节。返回 {ok, content, url, error}。"""
    tok = await gen_dl_token(config, torrent_id)
    if not tok["ok"]:
        return {"ok": False, "content": None, "url": "", "error": tok["error"]}
    url = tok["url"]
    try:
        async with httpx.AsyncClient(proxy=_proxy(config), timeout=30,
                                     follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": _UA})
    except Exception as e:
        return {"ok": False, "content": None, "url": url, "error": f"下载种子失败: {e}"}
    if resp.status_code != 200 or not resp.content:
        return {"ok": False, "content": None, "url": url,
                "error": f"下载种子失败 HTTP {resp.status_code}"}
    return {"ok": True, "content": resp.content, "url": url, "error": ""}


def _uid(config: dict) -> str:
    return str(config.get("mteam_uid") or "").strip()


async def member_profile(config: dict) -> dict:
    """会员资料（含上传/下载/分享率等）。返回 {ok, data, error}。"""
    uid = _uid(config)
    if not uid:
        return {"ok": False, "data": {}, "error": "未配置 M-Team uid"}
    res = await _post(config, "/member/profile", form={"uid": uid}, timeout=20)
    if not res["ok"]:
        return {"ok": False, "data": {}, "error": res["error"]}
    return {"ok": True, "data": (res["payload"] or {}).get("data") or {}, "error": ""}


async def peer_statistics(config: dict) -> dict:
    """做种/吸血统计。返回 {ok, data, error}。"""
    uid = _uid(config)
    if not uid:
        return {"ok": False, "data": {}, "error": "未配置 M-Team uid"}
    res = await _post(config, "/tracker/myPeerStatistics", form={"uid": uid}, timeout=20)
    if not res["ok"]:
        return {"ok": False, "data": {}, "error": res["error"]}
    return {"ok": True, "data": (res["payload"] or {}).get("data") or {}, "error": ""}


async def mybonus(config: dict) -> dict:
    """魔力值/积分。返回 {ok, data, error}。"""
    uid = _uid(config)
    if not uid:
        return {"ok": False, "data": {}, "error": "未配置 M-Team uid"}
    res = await _post(config, "/tracker/mybonus", form={"uid": uid}, timeout=20)
    if not res["ok"]:
        return {"ok": False, "data": {}, "error": res["error"]}
    return {"ok": True, "data": (res["payload"] or {}).get("data") or {}, "error": ""}


async def user_torrent_list(config: dict, page_size: int = 200) -> dict:
    """
    我发布/做种的种子（含审核状态）。返回 {ok, items:[规整记录], error}。
    body 结构未文档化，按常见 UserTorrentSearch 形态试探（带 uid/分页）。
    """
    # 接口必填 userid + type（type 为列表类型，做种监控取 SEEDING）
    body = {"userid": _uid(config), "type": "SEEDING",
            "pageNumber": 1, "pageSize": page_size}
    res = await _post(config, "/member/getUserTorrentList", json_body=body, timeout=25)
    if not res["ok"]:
        return {"ok": False, "items": [], "error": res["error"]}
    data = (res["payload"] or {}).get("data") or {}
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        rows = []
    items = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        # 兼容两种形态：直接是种子记录，或 {torrent:{...}, status/审核...}
        tor = r.get("torrent") if isinstance(r.get("torrent"), dict) else r
        items.append({
            "id": str(tor.get("id") or r.get("id") or ""),
            "name": tor.get("name") or "",
            "status": tor.get("status") or r.get("status") or "",
            "visible": tor.get("visible") if tor.get("visible") is not None else r.get("visible"),
            "raw": r,
        })
    return {"ok": True, "items": items, "error": ""}


# 发种所需的枚举列表端点（POST 无参；/system/getConf 是取站点配置的、不是这个）
_LIST_ENDPOINTS = {
    "category": "/torrent/categoryList",
    "standard": "/torrent/standardList",
    "videoCodec": "/torrent/videoCodecList",
    "audioCodec": "/torrent/audioCodecList",
    "source": "/torrent/sourceList",
    "medium": "/torrent/mediumList",
    "country": "/system/countryList",
}
_ENUM_NAME_KEYS = ("nameChs", "cname", "nameChi", "name", "label", "title")


def _norm_enum(raw) -> list:
    """把任意形态的枚举返回规整成 [{id, name}]。"""
    if isinstance(raw, dict):
        if isinstance(raw.get("data"), list):
            raw = raw["data"]
        elif isinstance(raw.get("list"), list):
            raw = raw["list"]
        else:
            return [{"id": str(k), "name": str(v)} for k, v in raw.items()
                    if isinstance(v, (str, int))]
    out = []
    if isinstance(raw, list):
        for it in raw:
            if not isinstance(it, dict):
                continue
            iid = it.get("id", it.get("value"))
            name = next((it[k] for k in _ENUM_NAME_KEYS if it.get(k)), "")
            if iid is not None:
                out.append({"id": str(iid), "name": str(name)})
    return out


async def get_enum_list(config: dict, name: str) -> dict:
    """拉取单个枚举列表（category/standard/videoCodec/audioCodec/source/medium）。"""
    path = _LIST_ENDPOINTS.get(name)
    if not path:
        return {"ok": False, "items": [], "error": f"未知枚举 {name}"}
    res = await _post(config, path, timeout=20)
    if not res["ok"]:
        return {"ok": False, "items": [], "error": res["error"]}
    return {"ok": True, "items": _norm_enum((res["payload"] or {}).get("data")), "error": ""}


async def get_conf(config: dict) -> dict:
    """
    拉取发种所需的全部枚举列表，合并成
    {category, standard, videoCodec, audioCodec, source, medium}（每项 [{id,name}]）。
    """
    out, errs = {}, []
    for name in _LIST_ENDPOINTS:
        r = await get_enum_list(config, name)
        if r["ok"]:
            out[name] = r["items"]
        else:
            errs.append(f"{name}: {r['error']}")
    if not out:
        return {"ok": False, "data": {}, "error": "；".join(errs) or "枚举拉取失败"}
    return {"ok": True, "data": out, "error": "；".join(errs)}


async def create_torrent(config: dict, fields: dict,
                         torrent_bytes: bytes, nfo_bytes: Optional[bytes] = None,
                         timeout: int = 120) -> dict:
    """
    发种：POST /torrent/createOredit（multipart）。
    fields 含 name/descr/category/anonymous 等文本字段；torrent_bytes 为 .torrent 二进制。
    返回 {ok, id, error}。
    """
    if not _api_key(config):
        return {"ok": False, "id": "", "error": "未配置 M-Team API 密钥"}
    url = _base(config) + "/torrent/createOredit"
    # 文本字段统一转 str；布尔转 "true"/"false"
    data = {}
    for k, v in (fields or {}).items():
        if v is None:
            continue
        if isinstance(v, bool):
            data[k] = "true" if v else "false"
        else:
            data[k] = str(v)
    files = {"file": ("upload.torrent", torrent_bytes, "application/x-bittorrent")}
    if nfo_bytes:
        files["nfo"] = ("info.nfo", nfo_bytes, "application/octet-stream")
    try:
        async with httpx.AsyncClient(proxy=_proxy(config), timeout=timeout,
                                     follow_redirects=True) as client:
            resp = await client.post(url, data=data, files=files,
                                     headers=_headers(config))
    except Exception as e:
        return {"ok": False, "id": "", "error": f"发种请求失败: {e}"}
    if resp.status_code in (401, 403):
        return {"ok": False, "id": "", "error": f"鉴权失败 HTTP {resp.status_code}"}
    if resp.status_code != 200:
        return {"ok": False, "id": "", "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    try:
        payload = resp.json()
    except Exception:
        return {"ok": False, "id": "", "error": f"响应非 JSON: {resp.text[:200]}"}
    if not _is_ok(payload):
        return {"ok": False, "id": "", "error": f"发种失败: {payload.get('message', payload)}"}
    data_field = payload.get("data")
    # data 可能是新种子 id（字符串/数字）或对象
    tid = ""
    if isinstance(data_field, (str, int)):
        tid = str(data_field)
    elif isinstance(data_field, dict):
        tid = str(data_field.get("id") or "")
    return {"ok": True, "id": tid, "error": ""}


async def diagnose(config: dict) -> dict:
    """
    连通诊断（仿 javdb/fc2 诊断风格）。
    用一次轻量 search 验证 base 可达 + 密钥有效。
    """
    base = _base(config)
    if not _api_key(config):
        return {"reachable": False, "authed": False, "base": base,
                "message": "未配置 API 密钥"}
    res = await _post(config, "/torrent/search", json_body={
        "keyword": "", "mode": DEFAULT_MODE, "pageNumber": 1, "pageSize": 1,
    }, timeout=15)
    if res["ok"]:
        return {"reachable": True, "authed": True, "base": base,
                "message": "连接正常，密钥有效"}
    err = res.get("error", "")
    authed = "鉴权失败" not in err
    return {"reachable": ("HTTP" in err or "鉴权" in err), "authed": authed,
            "base": base, "message": err}
