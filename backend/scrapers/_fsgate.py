"""
全局 FlareSolverr 串行闸 + 地址智能适配（V1.4.3）。

一、串行闸（GATE）
FlareSolverr 是单浏览器实例、一次只能处理一个请求；客户端并发提交会在其内部排队、
长时间挂起直至 ReadTimeout（典型症状：后台刮削正占用 FlareSolverr 时，前台点「JavDB
连通测试」就超时）。所有走 FlareSolverr 的取页——JavDB/FC2 的搜索/详情/诊断/最新，以及
MissAV 过盾兜底——都通过本闸全局串行，一次只向 FlareSolverr 发一个请求，避免互相撞车。

放置原则：闸只加在「取页层」（即本模块的 flaresolverr_request 内）。上层（如 enrich）
**不得**在持有任何会与本闸重入的锁后再触发取页。

二、地址智能适配（flaresolverr_request）
不论用户按哪种 NAS/服务器习惯填地址、把 FlareSolverr 装在哪，后端都尽量自动连上：
  - 写法兼容：缺 http:// 自动补、缺端口默认 8191、带 /v1 或末尾斜杠自动清理。
  - Docker localhost 自动改写：容器内填 localhost 指向的是本服务自己（必然连不上），
    自动按命中概率尝试 host.docker.internal / 同网络容器名 flaresolverr / 网桥网关 172.17.0.1。
  - 快失败：连接超时压到 6s（死地址不卡 70s），读取给足让 FlareSolverr 渲染。
  - 连上即缓存该地址，后续请求直接命中、不再多探；连不上给出可读排错提示。

JavDB / FC2 / MissAV 三源统一调用 flaresolverr_request，行为一致。
"""
import os
import asyncio
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

# 进程级、跨模块共享：JavDB / FC2 / MissAV 的 FlareSolverr 请求都排这一个队
GATE = asyncio.Semaphore(1)

# FlareSolverr 默认端口
FS_DEFAULT_PORT = 8191
# 上一次探测成功的 endpoint 缓存：{用户填写的原始地址: 实际连通的 host:port}
_fs_endpoint_cache: dict = {}


# ──────────────────────────────────────────────
# Docker 探测 / 地址规整 / 候选生成
# ──────────────────────────────────────────────
def running_in_docker() -> bool:
    """
    判断后端自身是否跑在容器里（NAS/群晖/服务器最常见的部署方式）。
    只有在容器内，localhost 才需要改写成宿主机网关/容器名才能连到「另一个」容器。
    """
    try:
        if os.path.exists("/.dockerenv"):
            return True
        with open("/proc/1/cgroup", "r", encoding="utf-8", errors="ignore") as f:
            data = f.read()
        return "docker" in data or "containerd" in data or "kubepods" in data
    except Exception:
        return False


def normalize_fs_url(raw: str) -> str:
    """
    规整用户填写的 FlareSolverr 地址，兼容各种习惯写法，统一成 scheme://host:port：
      - 缺协议：192.168.1.5:8191      -> http://192.168.1.5:8191
      - 缺端口：http://192.168.1.5    -> http://192.168.1.5:8191（FlareSolverr 默认 8191）
      - 带 /v1 或末尾斜杠            -> 去掉，调用时再统一补 /v1
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    try:
        p = urlsplit(raw)
        scheme = p.scheme or "http"
        host = p.hostname or ""
        if not host:
            return ""
        port = p.port or FS_DEFAULT_PORT
        return urlunsplit((scheme, f"{host}:{port}", "", "", ""))
    except Exception:
        return raw.rstrip("/")


def fs_candidates(raw: str) -> list:
    """
    依据常见 NAS/服务器装法，生成 FlareSolverr 候选地址（按尝试优先级排序）：
      1. 用户填写的规整地址（最优先，尊重用户意图）
      2. 若 host 是 localhost/127.0.0.1 且后端在容器内 —— 这是最常见的误配：
         FlareSolverr 多半是「另一个」容器，localhost 指向的是本服务自己，必然连不上。
         按命中概率自动补：
         - host.docker.internal:port  → FlareSolverr 容器把端口发布到宿主机时
         - flaresolverr:port / :8191  → 与本服务在同一 Docker 网络、用约定容器名时（内部端口固定 8191）
         - 172.17.0.1:port            → Docker 默认网桥网关，host.docker.internal 不可用时兜底
    局域网 IP / 域名不会被改写（只对 localhost 生效），非 Docker 部署也不扩展。去重保序后返回。
    """
    base = normalize_fs_url(raw)
    if not base:
        return []
    cands = [base]
    try:
        p = urlsplit(base)
        host = (p.hostname or "").lower()
        port = p.port or FS_DEFAULT_PORT
        if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") and running_in_docker():
            cands.append(f"http://host.docker.internal:{port}")
            cands.append(f"http://flaresolverr:{port}")
            if port != FS_DEFAULT_PORT:
                cands.append(f"http://flaresolverr:{FS_DEFAULT_PORT}")
            cands.append(f"http://172.17.0.1:{port}")
    except Exception:
        pass
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def resolved_endpoint(raw: str) -> str:
    """返回某个原始地址当前缓存的、已验证可连的 endpoint（供诊断回显）；未缓存返回空串。"""
    return _fs_endpoint_cache.get((raw or "").strip(), "")


def split_proxy_auth(proxy: str) -> tuple:
    """
    拆分代理 URL 中的账号密码：
      http://user:pass@host:port  ->  (http://host:port, user, pass)
    Chrome（FlareSolverr 用）命令行不支持内联账密，须拆开单独传。
    """
    if not proxy:
        return "", "", ""
    try:
        p = urlsplit(proxy)
        user = p.username or ""
        pwd = p.password or ""
        host = p.hostname or ""
        netloc = host + (f":{p.port}" if p.port else "")
        clean = urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))
        return clean, user, pwd
    except Exception:
        return proxy, "", ""


# ──────────────────────────────────────────────
# 单地址请求（快失败 + 区分是否连上）
# ──────────────────────────────────────────────
async def _request_one(endpoint: str, url: str, proxy: Optional[str],
                       cookies: Optional[dict], max_timeout: int,
                       read_timeout: float) -> tuple:
    """
    向「单个」FlareSolverr endpoint 发请求（已在 GATE 内串行）。返回 (html, status, error, connected)。
      connected=False：连 FlareSolverr 都没连上（ConnectTimeout/ConnectError）—— 地址多半填错，可换下一个候选。
      connected=True ：已连上（哪怕 FlareSolverr 自身报错或读超时）—— 地址是对的，不必再换。
    """
    ep = endpoint.rstrip("/")
    if not ep.endswith("/v1"):
        ep += "/v1"
    payload = {"cmd": "request.get", "url": url, "maxTimeout": max_timeout}
    if proxy:
        purl, puser, ppass = split_proxy_auth(proxy)
        pobj = {"url": purl}
        if puser:
            pobj["username"] = puser
        if ppass:
            pobj["password"] = ppass
        payload["proxy"] = pobj
    # 仅传 name/value（不带 domain）：部分 FlareSolverr 版本对带 domain 的 cookie 会报 500。
    if cookies:
        payload["cookies"] = [{"name": k, "value": v} for k, v in cookies.items()]
    # 连接超时设短（候选地址快速失败、不卡住），读取/写入给足（FlareSolverr 渲染+过盾较慢）。
    timeout = httpx.Timeout(connect=6.0, read=read_timeout, write=read_timeout, pool=read_timeout)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(ep, json=payload)
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        return "", 0, f"flaresolverr 异常: {type(e).__name__}: {e}", False
    except Exception as e:
        return "", 0, f"flaresolverr 异常: {type(e).__name__}: {e}", True
    fs_status, fs_msg, html, status = "", "", "", 0
    try:
        data = resp.json()
        fs_status = data.get("status", "") or ""
        fs_msg = data.get("message", "") or ""
        sol = data.get("solution") or {}
        html = sol.get("response", "") or ""
        status = int(sol.get("status", 0) or 0)
    except Exception:
        status = resp.status_code
    if resp.status_code != 200 or fs_status.lower() == "error":
        detail = fs_msg or f"HTTP {resp.status_code}"
        return html, status or resp.status_code, f"flaresolverr: {detail}", True
    return html, status or 200, "", True


# ──────────────────────────────────────────────
# 共享取页入口：智能多候选 + 全局串行 + 缓存
# ──────────────────────────────────────────────
async def flaresolverr_request(url: str, flaresolverr_url: str, proxy: Optional[str] = None,
                               cookies: Optional[dict] = None, max_timeout: int = 40000,
                               read_timeout: float = 70.0) -> tuple:
    """
    通过 FlareSolverr 取过盾后的 HTML（智能地址适配 + 全局串行）。返回 (html, status, error)。
    按 fs_candidates 生成候选地址依次尝试：连不上就换下一个，连上即用并缓存；
    全部连不上时附带「已尝试地址 + Docker 误配提示」。整体在 GATE 内串行。
    """
    candidates = fs_candidates(flaresolverr_url)
    if not candidates:
        return "", 0, "flaresolverr 异常: 地址为空"
    cache_key = (flaresolverr_url or "").strip()
    cached = _fs_endpoint_cache.get(cache_key)
    if cached and cached in candidates:
        candidates = [cached] + [c for c in candidates if c != cached]

    last_html, last_status, last_err = "", 0, ""
    # 全局串行：整轮候选尝试在同一个闸内，避免与其它路径撞车
    async with GATE:
        for ep in candidates:
            html, status, err, connected = await _request_one(
                ep, url, proxy, cookies, max_timeout, read_timeout)
            if connected:
                _fs_endpoint_cache[cache_key] = ep   # 记住可连地址
                return html, status, err
            last_html, last_status, last_err = html, status, err  # 连不上，换下一个

    # 所有候选都连不上：清掉缓存，回传最后错误并附排错提示
    _fs_endpoint_cache.pop(cache_key, None)
    hint = last_err
    if len(candidates) > 1:
        hint = (f"{last_err}（已自动尝试 {' / '.join(candidates)} 均连不上 FlareSolverr）。"
                "若 FlareSolverr 与本服务都在 Docker：请把地址填成 http://host.docker.internal:8191，"
                "或与本服务置于同一 Docker 网络后用容器名 http://flaresolverr:8191，不要用 localhost。")
    elif "ConnectTimeout" in last_err or "ConnectError" in last_err:
        hint = (f"{last_err}（连不上 {candidates[0]}）。"
                "请确认 FlareSolverr 已启动、地址/端口正确且本服务能访问到它。")
    return last_html, last_status, hint
