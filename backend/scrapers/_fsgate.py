"""
全局 FlareSolverr 取页闸（重写：填 URL 即用 + 防过载死机）。

定位：FlareSolverr 不绑进安装包，由用户自行部署后，在设置页填一个可达 URL
（如 http://192.168.1.100:8191 或绑定的外网域名）即用。本模块只负责把
JavDB / FC2 / MissAV 三源的取页请求安全地送到那个 URL，并防止把它打死。

为什么需要这把闸：FlareSolverr 是单浏览器实例，一次只能处理一个请求。
之前的事故是——后台刮削、首页最新、搜索、连通测试同时往它怼，请求在它内部
排队、互相挤、越堆越多，最终整个实例卡死（典型表现：所有请求一起 ReadTimeout，
被误判成「IP 失效」）。本模块用三层保护根治：

  1. 串行闸（GATE）：进程级 Semaphore(1)，一次只向 FlareSolverr 发一个请求。
  2. 背压（排队上限）：排队等待的请求超过 _MAX_WAITERS 就立刻快失败，
     不再无限堆积——堆积正是压垮单实例的根因。
  3. 熔断（circuit breaker）：连续多次「连不上 / 读超时」判定实例已不健康，
     开闸冷却一段时间，期间直接快失败，不再继续往一个半死的实例上怼，
     给它自我恢复的机会，也让前台立刻拿到可读错误而非长时间挂起。

另含地址规整（normalize_fs_url / fs_candidates）：缺协议自动补、缺端口默认 8191、
带 /v1 或末尾斜杠自动清理；容器内误填 localhost 时按命中概率兜底几个常见地址。
"""
import os
import time
import socket
import asyncio
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

# ── 三层保护的全局状态（进程级，跨模块共享）──
GATE = asyncio.Semaphore(1)        # 串行：一次只发一个请求给 FlareSolverr
_MAX_WAITERS = 16                  # 排队上限：超过直接快失败，杜绝无限堆积压垮实例
_waiters = 0                       # 当前正在排队 + 执行中的请求数
_MIN_INTERVAL = 0.4                # 两次请求之间的最小间隔（秒），给单浏览器留回收时间
_last_done_at = 0.0                # 上一次请求结束的时刻（time.monotonic）

# 熔断：连续「连不上 / 读超时」达到阈值即开闸冷却，期间快失败不再打实例
_CB_FAIL_THRESHOLD = 4             # 连续失败多少次触发熔断
_CB_COOLDOWN = 45.0                # 熔断冷却时长（秒）
_consec_fails = 0                  # 当前连续失败计数（成功即清零）
_cb_open_until = 0.0               # 熔断打开至此刻之前都快失败（time.monotonic）

# FlareSolverr 默认端口
FS_DEFAULT_PORT = 8191
# 上一次探测成功的 endpoint 缓存：{用户填写的原始地址: 实际连通的 host:port}
_fs_endpoint_cache: dict = {}


# ──────────────────────────────────────────────
# Docker 探测 / 地址规整 / 候选生成
# ──────────────────────────────────────────────
def running_in_docker() -> bool:
    """判断后端自身是否跑在容器里——只有容器内填 localhost 才需要改写成宿主机网关/容器名。"""
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
      - 缺端口：http://192.168.1.5    -> http://192.168.1.5:8191
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
    生成 FlareSolverr 候选地址（按尝试优先级）：用户填的规整地址永远最优先；
    仅当填的是 localhost/127.0.0.1 且后端在容器内（最常见误配——此时 localhost 指向
    本服务自己，连不上）才追加 host.docker.internal / 容器名 flaresolverr / 网桥网关兜底。
    局域网 IP / 域名一律不改写，非 Docker 不扩展。去重保序返回。
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


# ──────────────────────────────────────────────
# 自动探测：设置页 FlareSolverr 地址留空时，自动找出本机/同宿主机可达的 FlareSolverr
# ──────────────────────────────────────────────
# 设计目标（用户要求）：装了 FlareSolverr 就「自然就通」、零填写；探不到也不要一直死循环重扫。
# 做法：按命中概率探常见地址，再扫本容器所在 /24 网段的 8191（能自动找到 172.17.0.x 这种 sibling
# 容器 IP——默认 bridge 无容器名 DNS、且主机防火墙常挡「网桥→宿主机网关」，唯有容器对容器直连最稳）。
# 探到就正缓存复用、探不到就负缓存一段时间，期间不再重扫（即「不通则不再尝试」，避免每次请求都全网段扫）。
_AUTO_TTL = 300.0                 # 探到地址的缓存时长（秒）
_AUTO_NEG_TTL = 300.0             # 没探到的负缓存时长（秒）——这段时间内不再全网段重扫
_FS_PROBE_TIMEOUT = 1.2          # 单个候选的探测超时（秒）
_auto_state = {"endpoint": "", "ts": 0.0}   # ts=0 表示从未探测
_auto_lock = asyncio.Lock()       # 单飞：避免多个请求同时触发全网段重扫


def _own_ipv4() -> str:
    """取本进程对外的 IPv4（容器内即其网桥 IP，如 172.17.0.3）；失败返回空串。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))     # 不真正发包，只为让内核选出口网卡
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return ""


def _default_gateway_ipv4() -> str:
    """读 /proc/net/route 取默认网关（容器内 = docker 网桥网关，通常 172.17.0.1）；失败返回空串。"""
    try:
        with open("/proc/net/route", "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        for line in lines[1:]:
            parts = line.split()
            if len(parts) >= 3 and parts[1] == "00000000" and parts[2] != "00000000":
                gw_hex = parts[2]   # 小端十六进制
                octets = [int(gw_hex[i:i + 2], 16) for i in (6, 4, 2, 0)]
                return ".".join(str(o) for o in octets)
    except Exception:
        pass
    return ""


async def _probe_is_fs(host_port: str, timeout: float = _FS_PROBE_TIMEOUT) -> bool:
    """探一个 host:port 是不是 FlareSolverr：GET / 看响应是否含 'FlareSolverr'。不走 GATE（轻量健康检查）。"""
    try:
        t = httpx.Timeout(connect=timeout, read=timeout, write=timeout, pool=timeout)
        async with httpx.AsyncClient(timeout=t) as client:
            resp = await client.get(f"http://{host_port}/")
        return resp.status_code == 200 and "FlareSolverr" in (resp.text or "")
    except Exception:
        return False


async def discover_auto(force: bool = False) -> str:
    """
    自动探测可达的 FlareSolverr，返回规整 URL（如 http://172.17.0.3:8191）；探不到返回 ''。
    带正/负缓存 + 单飞。顺序：容器名 flaresolverr → host.docker.internal → 网桥网关 → 127.0.0.1
    → 扫描本容器 /24 网段的 8191。force=True 跳过缓存强制重探（用于「测试连通」按钮）。
    """
    st = _auto_state
    now = time.monotonic()
    ttl = _AUTO_TTL if st["endpoint"] else _AUTO_NEG_TTL
    if not force and st["ts"] and (now - st["ts"] < ttl):
        return st["endpoint"]

    async with _auto_lock:
        # 二次确认：可能在等锁期间已被其它协程刷新
        now = time.monotonic()
        ttl = _AUTO_TTL if st["endpoint"] else _AUTO_NEG_TTL
        if not force and st["ts"] and (now - st["ts"] < ttl):
            return st["endpoint"]

        port = FS_DEFAULT_PORT
        gw = _default_gateway_ipv4() or "172.17.0.1"
        # 1) 常见地址，一把并发探（命中概率高、最快）
        common = [f"flaresolverr:{port}", f"host.docker.internal:{port}",
                  f"{gw}:{port}", f"127.0.0.1:{port}"]
        results = await asyncio.gather(*[_probe_is_fs(hp) for hp in common])
        for hp, ok in zip(common, results):
            if ok:
                ep = f"http://{hp}"
                _auto_state.update({"endpoint": ep, "ts": time.monotonic()})
                print(f"[fsgate] 自动探测到 FlareSolverr：{ep}")
                return ep

        # 2) 扫描本容器所在 /24 网段（找 sibling 容器 IP，如 172.17.0.3）
        own = _own_ipv4()
        found = ""
        if own.count(".") == 3:
            base = own.rsplit(".", 1)[0]
            skip = {own, gw}
            hosts = [f"{base}.{i}" for i in range(1, 255) if f"{base}.{i}" not in skip]
            sem = asyncio.Semaphore(64)

            async def _scan(h: str) -> str:
                async with sem:
                    return h if await _probe_is_fs(f"{h}:{port}") else ""

            hits = [h for h in await asyncio.gather(*[_scan(h) for h in hosts]) if h]
            if hits:
                hits.sort(key=lambda x: int(x.rsplit(".", 1)[1]))  # 取末位最小，结果稳定
                found = f"http://{hits[0]}:{port}"

        _auto_state.update({"endpoint": found, "ts": time.monotonic()})
        if found:
            print(f"[fsgate] 自动探测（网段扫描）到 FlareSolverr：{found}")
        else:
            print(f"[fsgate] 自动探测未发现 FlareSolverr，{int(_AUTO_NEG_TTL)}s 内不再重扫（回退直连）")
        return found


def auto_endpoint() -> str:
    """返回当前缓存的自动探测结果（未过期才返回，否则 ''）。供 enrich 等同步路径快速判断是否走 FS。"""
    st = _auto_state
    if st["endpoint"] and (time.monotonic() - st["ts"] < _AUTO_TTL):
        return st["endpoint"]
    return ""


def split_proxy_auth(proxy: str) -> tuple:
    """
    拆分代理 URL 中的账号密码：http://user:pass@host:port -> (http://host:port, user, pass)。
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
# 单地址请求（快失败 + 区分「连没连上」「实例健不健康」）
# ──────────────────────────────────────────────
async def _request_one(endpoint: str, url: str, proxy: Optional[str],
                       cookies: Optional[dict], max_timeout: int,
                       read_timeout: float) -> tuple:
    """
    向「单个」FlareSolverr endpoint 发请求（已在 GATE 内串行）。
    返回 (html, status, error, connected, healthy)：
      connected=False：连 FlareSolverr 都没连上（ConnectTimeout/ConnectError）—— 地址多半填错，换下一个候选。
      connected=True ：连上了（哪怕它报错或读超时）—— 地址是对的，不必再换。
      healthy=True   ：实例正常应答了一个 JSON（即使站点 403/被盾，那是站点的事，实例本身是活的）。
      healthy=False  ：连不上或读超时/写超时—— 实例可能已卡死，计入熔断。
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
        # 没连上：地址问题，可换候选；实例健康度未知 → 计为不健康（连不上本就该熔断）
        return "", 0, f"flaresolverr 异常: {type(e).__name__}: {e}", False, False
    except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as e:
        # 连上了但读/写超时：地址对，但实例多半卡住了 → 不健康，计入熔断
        return "", 0, f"flaresolverr 异常: {type(e).__name__}: {e}", True, False
    except Exception as e:
        return "", 0, f"flaresolverr 异常: {type(e).__name__}: {e}", True, False
    # 走到这里说明实例应答了一个 HTTP 响应 → 实例是活的（healthy=True），
    # 哪怕里面是 FlareSolverr 报错或站点 403，也属于「站点 / 配置」问题而非实例卡死。
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
        return html, status or resp.status_code, f"flaresolverr: {detail}", True, True
    return html, status or 200, "", True, True


# ──────────────────────────────────────────────
# 共享取页入口：背压 + 熔断 + 全局串行 + 智能多候选 + 缓存
# ──────────────────────────────────────────────
async def flaresolverr_request(url: str, flaresolverr_url: str, proxy: Optional[str] = None,
                               cookies: Optional[dict] = None, max_timeout: int = 40000,
                               read_timeout: float = 70.0) -> tuple:
    """
    通过 FlareSolverr 取过盾后的 HTML。返回 (html, status, error)。
    依次经过：熔断检查 → 背压检查 → 全局串行闸 → 最小间隔 → 多候选尝试。
    任何一层快失败都返回可读错误，绝不长时间挂起或继续压垮实例。
    """
    global _waiters, _last_done_at, _consec_fails, _cb_open_until

    candidates = fs_candidates(flaresolverr_url)
    if not candidates:
        return "", 0, "flaresolverr 异常: 地址为空"

    now = time.monotonic()
    # 1) 熔断：实例近期连续失败，冷却期内直接快失败，不再去怼它
    if now < _cb_open_until:
        wait = int(_cb_open_until - now) + 1
        return "", 0, (f"flaresolverr 异常: 实例连续多次无响应，已暂停请求 {wait}s 让其恢复"
                       "（避免大量任务把单实例继续压垮）。请确认 FlareSolverr 还活着、负载没爆。")

    # 2) 背压：排队已满直接快失败，杜绝无限堆积（堆积正是压垮单实例的根因）
    if _waiters >= _MAX_WAITERS:
        return "", 0, (f"flaresolverr 异常: 排队请求已达上限（{_MAX_WAITERS}），实例处理不过来，"
                       "已快速放弃本次以保护实例。请稍后再试，或降低并发/最新片源条数。")

    _waiters += 1
    try:
        # 3) 全局串行：整轮候选尝试在同一把闸内，一次只向 FlareSolverr 发一个请求
        async with GATE:
            # 4) 最小间隔：给单浏览器实例留出回收上一次会话的时间
            gap = _MIN_INTERVAL - (time.monotonic() - _last_done_at)
            if gap > 0:
                await asyncio.sleep(gap)

            cache_key = (flaresolverr_url or "").strip()
            cached = _fs_endpoint_cache.get(cache_key)
            ordered = candidates
            if cached and cached in candidates:
                ordered = [cached] + [c for c in candidates if c != cached]

            last_html, last_status, last_err = "", 0, ""
            any_healthy = False
            try:
                for ep in ordered:
                    html, status, err, connected, healthy = await _request_one(
                        ep, url, proxy, cookies, max_timeout, read_timeout)
                    any_healthy = any_healthy or healthy
                    if connected:
                        _fs_endpoint_cache[cache_key] = ep   # 记住可连地址
                        return html, status, err
                    last_html, last_status, last_err = html, status, err  # 连不上，换下一个
            finally:
                _last_done_at = time.monotonic()
                # 熔断计数：本轮实例是否健康（连得上且有应答）。健康即清零，不健康才累加。
                if any_healthy:
                    _consec_fails = 0
                    _cb_open_until = 0.0
                else:
                    _consec_fails += 1
                    if _consec_fails >= _CB_FAIL_THRESHOLD:
                        _cb_open_until = time.monotonic() + _CB_COOLDOWN
                        print(f"[fsgate] FlareSolverr 连续 {_consec_fails} 次无响应，"
                              f"熔断冷却 {int(_CB_COOLDOWN)}s")

        # 所有候选都连不上：清掉缓存，回传最后错误并附排错提示
        _fs_endpoint_cache.pop(cache_key, None)
        hint = last_err
        if len(ordered) > 1:
            hint = (f"{last_err}（已自动尝试 {' / '.join(ordered)} 均连不上 FlareSolverr）。"
                    "若 FlareSolverr 与本服务都在 Docker：请把地址填成 http://host.docker.internal:8191，"
                    "或与本服务置于同一 Docker 网络后用容器名 http://flaresolverr:8191，不要用 localhost。")
        elif "ConnectTimeout" in last_err or "ConnectError" in last_err:
            hint = (f"{last_err}（连不上 {ordered[0]}）。"
                    "请确认 FlareSolverr 已启动、地址/端口正确且本服务能访问到它。")
        return last_html, last_status, hint
    finally:
        _waiters -= 1
