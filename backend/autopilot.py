"""
FC2 全自动发种调度器（V1.5.1·蓄种池模型）

后台定时跑「发现 → 动态补池 → PT 站查重 → 等磁力 → 顺序发种」一条龙，入队后的发布/做种
全部交给现有发种流水线（publish.py，已在 v1.5.0-beta39 做过容错加固）。本模块只负责：
  ① 定时抓 FC2 最新番号（scrapers.fc2.get_latest，sukebei+官方卖场双源，最新最全），
     用「发现窗口」(autopilot_discover_count，约一两百条)动态【补充/刷新】蓄种池；
  ② 逐号 PT 站查重(M-Team)：站点已有的登记进 done_pt 跳过，查重通过的留在蓄种池；
  ③ 蓄种池里「查重通过」的番号按号顺序逐个搜磁力（资源源 sukebei/jackett，严格番号匹配）；
  ④ 搜到磁力者经名额节流（下载中/未成熟做种数 + 每轮上限）陆续入队，避免突增打爆低配 VPS；
  ⑤ 去重靠「登记册」而非游标：凡建过发种任务的番号(任意状态，publish.has_any_task)一律
     不再抓取入池；PT 站已有的记 done_pt。故番号不按数字大小被「游标」跳过——小号磁力
     即便晚出，只要还在发现窗口内就会被重新发现、入池、等磁力出现再发种。

为何废弃旧「游标(cursor)+跳号」：磁力不一定按番号顺序出现，小号常在大号之后才有人发布
磁力。旧模型游标一旦推过某小号、它又滑出发现窗口，就永久漏发。蓄种池模型不设单调游标，
只要发现窗口能再次扫到该号（且未发种、未在 PT），它就会回到池里继续等磁力。

状态持久化在 /config/autopilot_state.json：pool（蓄种池）/ done_pt（PT已有登记）/ stats。
"""
import asyncio
import json
import re
import time
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

import config_manager
from config_manager import load as load_config
import logbus
import publish
import downloader
import mteam
from scrapers import fc2 as fc2_scraper
from scrapers._sukebei import search_sukebei
from jackett import search_jackett

router = APIRouter(prefix="/api/autopilot")

_STATE_PATH = config_manager.CONFIG_PATH.parent / "autopilot_state.json"
_worker_task: Optional[asyncio.Task] = None
_BUSY = False   # 防止 worker tick 与「立即跑一轮」并发重入

# 运行期状态（落盘）
#
# pool（蓄种池）：持久候选池。发现窗口里、号 >= 起点下限、且【未建过任务、未在 done_pt】的
#   番号都补进来，携带元数据与状态：
#     checked=False → 待 PT 查重；checked=True → 查重通过、留池等磁力（蓄种中）。
#   离开池的三种情形：入队发种成功 / 查重命中 PT(转 done_pt) / 蓄种超 keep_days 天剔除。
# done_pt（PT 已有登记）：查重命中 PT 站、但本地没建过任务的番号。仅用于后续刷新时跳过，
#   免得每轮对同一个「站点已有」番号反复查重/搜磁力。我方已发种的番号不进这里——它们由
#   publish 任务表(has_any_task)登记，是另一套去重。
# 不再有 cursor / pending 字段（旧「游标+跳号」模型已废弃）。
_state = {
    "pool": {},             # {code: {"num","title","cover","url","source","uncensored","checked","tries","added"}}
    "done_pt": {},          # {code: {"num","ts"}}
    "stats": {
        "last_run": 0.0, "last_added": 0, "last_error": "",
        "total_added": 0, "total_on_pt": 0, "total_evicted": 0,
        "latest_seen": 0,
    },
}


def _log(msg: str):
    logbus.info("全自动", msg)


# ── 状态持久化 ──
def _load_state():
    try:
        if not _STATE_PATH.exists():
            return
        d = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            return
        now = time.time()
        pool = {}
        # 新版蓄种池
        for code, meta in (d.get("pool") or {}).items():
            if not isinstance(meta, dict):
                continue
            num = _number_of(str(code))
            if num <= 0:
                continue
            pool[str(code)] = {
                "num": num,
                "title": str(meta.get("title", "")),
                "cover": str(meta.get("cover", "")),
                "url": str(meta.get("url", "")),
                "source": str(meta.get("source", "fc2")) or "fc2",
                "uncensored": bool(meta.get("uncensored")),
                "checked": bool(meta.get("checked")),
                "tries": int(meta.get("tries", 0) or 0),
                "added": float(meta.get("added", now) or now),
            }
        # 兼容旧版「待办池」pending / pending_retry：并入蓄种池（待查重 checked=False）
        for code, meta in (d.get("pending") or {}).items():
            code = str(code)
            if code in pool or not isinstance(meta, dict):
                continue
            num = _number_of(code)
            if num > 0:
                pool[code] = {
                    "num": num, "title": str(meta.get("title", "")),
                    "cover": str(meta.get("cover", "")), "url": str(meta.get("url", "")),
                    "source": str(meta.get("source", "fc2")) or "fc2",
                    "uncensored": bool(meta.get("uncensored")),
                    "checked": False, "tries": int(meta.get("tries", 0) or 0), "added": now,
                }
        for code in (d.get("pending_retry") or {}):
            code = str(code)
            num = _number_of(code)
            if code not in pool and num > 0:
                pool[code] = {"num": num, "title": "", "cover": "", "url": "",
                              "source": "fc2", "uncensored": False,
                              "checked": False, "tries": 0, "added": now}
        _state["pool"] = pool
        done_pt = {}
        for code, meta in (d.get("done_pt") or {}).items():
            code = str(code)
            num = _number_of(code)
            if num > 0:
                m = meta if isinstance(meta, dict) else {}
                done_pt[code] = {"num": num, "ts": float(m.get("ts", now) or now)}
        _state["done_pt"] = done_pt
        st = d.get("stats") or {}
        _state["stats"].update({k: st[k] for k in _state["stats"] if k in st})
    except Exception as e:
        _log(f"状态加载失败（用默认）：{e}")


def _save_state():
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(json.dumps(_state, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except Exception as e:
        _log(f"状态持久化失败：{e}")


def _dedup_keyword(code: str) -> str:
    """PT 站查重关键词：把番号破折号换空格并折叠空白（与 publish 查重一致，分词更易命中）。"""
    return re.sub(r"\s+", " ", (code or "").replace("-", " ")).strip()


# ── 番号工具 ──
def _strict_code_match(candidate_title: str, target_code: str) -> bool:
    """严格番号匹配：候选标题里【任意位置】须出现与目标完全一致的番号 token。

    番号未必在标题最开头——资源标题常带前缀噪声，如：
      「[HD 720p] FC2-PPV-4925502 ※本物アイドル…」「+++ FC2-PPV-4925502 【…】18歳…」。
    故不能简单「剥噪声后从开头比对」（那样 HD/720p 这种【字母数字】前缀会让开头错位、漏判）。

    做法：把目标番号拆成字母数字段（FC2-PPV-4925502 → FC2 / PPV / 4925502），
    段间允许任意非字母数字噪声（空格/破折号/方括号/全角符号/中文等），
    并要求 token 两端都是【非字母数字边界】：
      - 前边界 (?<![a-z0-9])：杜绝 xFC2.../720pFC2... 这种字母数字粘连的误配；
      - 后边界 (?![a-z0-9]) ：数字边界，杜绝 4925502 误配 49255021（多一位）、4925502abc。
    满足用户口径「排除符号噪声后，番号的字母+数字+顺序完全一致才确认」。
    """
    title = (candidate_title or "").lower()
    segs = re.findall(r"[a-z0-9]+", (target_code or "").lower())
    if not title or not segs:
        return False
    body = r"[^a-z0-9]*".join(re.escape(s) for s in segs)
    pattern = r"(?<![a-z0-9])" + body + r"(?![a-z0-9])"
    return re.search(pattern, title) is not None


def _number_of(code: str) -> int:
    """取番号里的数字主体（FC2-PPV-4925502 → 4925502）。取最长的一段数字。"""
    nums = re.findall(r"\d+", code or "")
    return max((int(n) for n in nums), default=0)


# ── 节流：成熟做种释放槽位 ──
def _capacity(config: dict) -> dict:
    """按现有全自动任务状态算本轮可新增名额。返回 {slots, downloading, seeding_active}。

    - downloading    = 全自动任务中处于 排队/查重/下载/处理 阶段的数量；
    - seeding_active  = 全自动任务中 做种且【未成熟】（做种时长 < settle 分钟）的数量；
      已成熟的做种不再占名额，使流水线能持续推进。
    名额 = min(maxDownloading-downloading, maxSeeding-seeding_active, maxNewPerRound)，下限 0。
    """
    max_dl = int(config.get("autopilot_max_downloading", 2) or 0)
    max_seed = int(config.get("autopilot_max_seeding", 3) or 0)
    settle_s = float(config.get("autopilot_seed_settle_minutes", 120) or 0) * 60
    max_new = int(config.get("autopilot_max_new_per_round", 3) or 0)
    now = time.time()
    downloading = seeding_active = 0
    for st, seed_started in publish.auto_task_states():
        if st in (publish.QUEUED, publish.CHECKING, publish.DOWNLOADING, publish.PROCESSING):
            downloading += 1
        elif st == publish.SEEDING:
            if settle_s <= 0 or (now - seed_started) < settle_s:
                seeding_active += 1
    slots = min(max_dl - downloading, max_seed - seeding_active, max_new)
    return {"slots": max(0, slots), "downloading": downloading,
            "seeding_active": seeding_active}


# ── 资源搜索（带严格匹配，默认选体积最大＝最清晰的版本）──
async def _find_magnet(code: str, config: dict) -> dict:
    """按设置的资源源搜种并严格匹配，返回 {magnet, title, source, size_bytes} 或 {} 未命中。
    源选择：autopilot_resource_source（sukebei|jackett）；开了 fallback 则另一源再搜一次。

    选种口径（autopilot_prefer_largest，默认 True）：同一番号常有多个版本（如 720p 小文件 /
    FHD 大文件），搜索结果默认按「做种数」排序，小文件未必清晰却因做种多排在最前——故这里
    在【全部严格匹配且有磁力】的候选里挑【体积最大】的那个（体积相同再看做种数），保证拿到
    最清晰版本。关掉该项则回退「取结果首个匹配」（即按源排序：做种多优先）。

    防「选中小文件」三道闸（修复 1.5.1 做种是小文件）：
      ① 体积下限 autopilot_min_size_mb：已知体积低于下限的种子直接排除（堵小样片/预览片）；
         体积【未知(解析为0)】的不受此限——避免把解析失败的大种子误杀；若全部低于下限则
         放宽兜底（绝不空手而归，宁可发小也别漏发，再靠日志暴露）。
      ② 退化告警：开了「选最大」但全部候选体积都未知(0) → max 退化成「按做种数选」，
         FC2 做种最多的恰恰常是被广泛转发的小文件——此时明确告警，暴露体积解析问题。
      ③ 全程日志：把每个候选(标题/体积/做种数)与最终选择都打出来，下次失手可立即定位。"""
    proxy = config.get("proxy") or None
    primary = (config.get("autopilot_resource_source") or "jackett").strip().lower()
    fallback = bool(config.get("autopilot_resource_fallback", True))
    prefer_largest = bool(config.get("autopilot_prefer_largest", True))
    min_bytes = int(config.get("autopilot_min_size_mb", 300) or 0) * 1024 * 1024
    # Jackett 是否可用（与前端「搜索资源」同一判定：有 url+key 即可用）。Jackett 经 Torznab
    # 返回数值字节 Size，体积判定最准——故默认优先用它选最大；未配置则自动退到 sukebei。
    jackett_ok = bool((config.get("jackett_url") or "").strip()
                      and (config.get("jackett_api_key") or "").strip())
    if primary == "jackett" and not jackett_ok:
        _log(f"[{code}] 资源源设为 Jackett 但未配置(缺 url/key)，自动改用 sukebei")
        primary = "sukebei"
    order = [primary] + ([("jackett" if primary == "sukebei" else "sukebei")] if fallback else [])
    order = [s for s in order if s != "jackett" or jackett_ok]   # 未配置则剔除 jackett，免空转

    def _gb(n: int) -> str:
        return f"{n / (1024**3):.2f}GB" if n > 0 else "体积未知"

    for src in order:
        try:
            if src == "jackett":
                ju = (config.get("jackett_url") or "").strip()
                jk = (config.get("jackett_api_key") or "").strip()
                if not (ju and jk):
                    continue
                results = await search_jackett(
                    query=code, jackett_url=ju, api_key=jk,
                    indexers=(config.get("jackett_indexers") or "all"),
                    proxy=None, timeout=int(config.get("jackett_timeout", 20) or 20))
            else:
                results = await search_sukebei(code, proxy=proxy)
        except Exception as e:
            _log(f"[{code}] {src} 搜索异常：{e}")
            continue
        # 收集本源【全部严格匹配且有磁力】的候选
        matches = []
        for r in (results or []):
            magnet = r.get("magnet") or r.get("link") or ""
            if not magnet:
                continue
            if _strict_code_match(r.get("title", ""), code):
                matches.append({
                    "magnet": magnet, "title": r.get("title", ""), "source": src,
                    "size_bytes": int(r.get("size_bytes") or 0),
                    "seeders": int(r.get("seeders") or 0),
                })
        if not matches:
            continue

        # ③ 候选明细日志（多候选时才打，单候选无需）——下次「选中小文件」可凭此立即定位
        if len(matches) > 1:
            detail = "；".join(
                f"{_gb(m['size_bytes'])}/做种{m['seeders']}·{(m['title'] or '')[:36]}"
                for m in sorted(matches, key=lambda m: -m["size_bytes"])[:6])
            _log(f"[{code}] {src} 严格匹配 {len(matches)} 个候选：{detail}")

        if prefer_largest:
            # ① 体积下限：先在「已知体积 ≥ 下限」里挑；为空（全未知或全偏小）则放宽到全部候选兜底
            pool = [m for m in matches if min_bytes <= 0 or m["size_bytes"] >= min_bytes]
            if not pool:
                pool = matches
                if min_bytes > 0:
                    _log(f"[{code}] {src} 无 ≥{min_bytes // (1024*1024)}MB 的候选，"
                         f"放宽兜底（可能确无大文件版，请留意）")
            # ② 退化告警：开了选最大却全员体积未知 → max 实际按做种数选，易中小文件
            if all(m["size_bytes"] <= 0 for m in pool):
                _log(f"[{code}] {src} 警告：候选体积全部解析失败，"
                     f"「选最大」已退化为「按做种数选」，可能选中小文件（请检查体积解析）")
            best = max(pool, key=lambda m: (m["size_bytes"], m["seeders"]))
            _log(f"[{code}] {src} 选定：{_gb(best['size_bytes'])}/做种{best['seeders']}·"
                 f"{(best['title'] or '')[:40]}")
            return best
        # 关掉「选最大」：取源排序首个（做种多优先）
        _log(f"[{code}] {src} 选定(首个匹配)：{_gb(matches[0]['size_bytes'])}/"
             f"做种{matches[0]['seeders']}·{(matches[0]['title'] or '')[:40]}")
        return matches[0]
    return {}


# ── PT 站查重 ──
async def _pt_check(code: str, config: dict) -> Optional[bool]:
    """查 PT 站(M-Team)是否已有该番号。返回 True=站点已有 / False=站点暂无 / None=查询失败。
    复用 publish 流水线同款关键词（破折号转空格，分词更易命中）。"""
    try:
        res = await mteam.search(config, keyword=_dedup_keyword(code), page_size=20)
    except Exception as e:
        _log(f"[{code}] PT 查重异常：{e}")
        return None
    if not res.get("ok"):
        _log(f"[{code}] PT 查重失败：{res.get('error', '')[:60]}")
        return None
    return bool(res.get("items"))


# ── 一轮 ──
async def _round(config: dict) -> dict:
    """跑一轮：动态补池 → PT 查重 → 蓄种等磁力 → 顺序发种。返回本轮摘要 dict。"""
    if not config.get("autopilot_fc2_enabled"):
        return {"skipped": "未启用"}

    # 下载器不可达则本轮直接跳过——不空转入队（与 publish 加固一致，宁可漏不可错）
    dl = await downloader.list_torrents_ex(config)
    if not dl["reachable"]:
        msg = f"下载器不可达，跳过本轮（{dl.get('error', '')[:50]}）"
        _state["stats"]["last_error"] = msg
        _save_state()
        _log(msg)
        return {"skipped": msg}

    proxy = config.get("proxy") or None
    floor = max(0, int(config.get("autopilot_fc2_start_number", 0) or 0))  # 起点下限，0=不限

    # 抓 FC2 最新（发现窗口条数可配，越大越不易在两轮间漏号；上限由 get_latest 收口到 200）
    discover_n = max(1, int(config.get("autopilot_discover_count", 200) or 200))
    try:
        latest = await fc2_scraper.get_latest(proxy, discover_n)
    except Exception as e:
        msg = f"抓 FC2 最新失败：{e}"
        _state["stats"]["last_error"] = msg
        _save_state()
        _log(msg)
        return {"error": msg}

    discovered = []
    for it in (latest or []):
        code = (it.get("code") or "").strip()
        num = _number_of(code)
        if num > 0:
            discovered.append((num, code, it))
    if discovered:
        _state["stats"]["latest_seen"] = max(n for n, _, _ in discovered)

    pool = _state["pool"]
    done_pt = _state["done_pt"]
    now = time.time()

    # ① 动态补池：把发现窗口里、号 >= 起点下限、且【未建过任务、未在 done_pt】的番号补进蓄种池。
    #    已在池中的号用本轮更优元数据回填；已建任务/已在 done_pt 的号顺手从池里清掉（去重收敛）。
    added_pool = 0
    for num, code, it in discovered:
        if num < floor:
            continue
        if code in done_pt:
            pool.pop(code, None)
            continue
        if publish.has_any_task(code):     # 登记在册（任意状态的发种任务）→ 不再入池
            pool.pop(code, None)
            continue
        meta = pool.get(code)
        if meta is None:
            pool[code] = {
                "num": num, "title": it.get("title", ""), "cover": it.get("cover", ""),
                "url": it.get("url", ""), "source": it.get("source", "fc2") or "fc2",
                "uncensored": bool(it.get("uncensored_hint")),
                "checked": False, "tries": 0, "added": now,
            }
            added_pool += 1
        else:
            meta["num"] = num
            if it.get("title") and not meta.get("title"):
                meta["title"] = it["title"]
            if it.get("cover") and not meta.get("cover"):
                meta["cover"] = it["cover"]
            if it.get("url") and not meta.get("url"):
                meta["url"] = it["url"]
            if it.get("uncensored_hint"):
                meta["uncensored"] = True

    # ② PT 查重：对池里待查重(checked=False)的号逐个查，按号从小到大，限每轮条数(控 M-Team 调用量)。
    #    站点已有→移出池记 done_pt；站点暂无→checked=True（进入蓄种等磁力）。
    #    M-Team 未配置则跳过查重、直接放行（交流水线 CHECKING 兜底）；查询失败则保留待下轮再查。
    mteam_ok = bool((config.get("mteam_api_key") or "").strip())
    check_cap = max(0, int(config.get("autopilot_check_per_round", 20) or 0))
    on_pt = 0
    unchecked = sorted((c for c in pool if not pool[c]["checked"]),
                       key=lambda c: pool[c]["num"])
    if not mteam_ok:
        for code in unchecked:
            pool[code]["checked"] = True
    else:
        for code in unchecked[:check_cap or len(unchecked)]:
            verdict = await _pt_check(code, config)
            if verdict is None:           # 查询失败：保留待下轮，避免误判
                continue
            if verdict:
                pool.pop(code, None)
                done_pt[code] = {"num": _number_of(code), "ts": now}
                on_pt += 1
            else:
                pool[code]["checked"] = True

    # ③ 蓄种超时剔除：查重通过但长期搜不到磁力的老号，留池超 keep_days 天即剔除（0=永不剔除）。
    keep_days = max(0, int(config.get("autopilot_pool_keep_days", 14) or 0))
    evicted = 0
    if keep_days > 0:
        ttl = keep_days * 86400
        for code in [c for c in pool
                     if pool[c]["checked"] and (now - pool[c]["added"]) > ttl]:
            pool.pop(code, None)
            evicted += 1
        if evicted:
            _log(f"蓄种池剔除 {evicted} 个超 {keep_days} 天仍无磁力的番号")

    # ④ 顺序发种：蓄种池里「查重通过」的号按号从小到大逐个搜磁力，限每轮搜索条数(控资源源调用量)；
    #    搜到磁力者受名额节流(slots)陆续入队。slots 仅由成功入队消耗，搜不到的不占名额。
    find_cap = max(0, int(config.get("autopilot_find_per_round", 60) or 0))
    cap = _capacity(config)
    slots = cap["slots"]
    added, nomatch = 0, 0
    ready = sorted((c for c in pool if pool[c]["checked"]),
                   key=lambda c: pool[c]["num"])
    searched = 0
    for code in ready:
        if slots <= 0:
            break
        if find_cap and searched >= find_cap:
            break
        meta = pool[code]
        if publish.has_any_task(code):     # 期间已建任务：移出池去重
            pool.pop(code, None)
            continue
        searched += 1
        found = await _find_magnet(code, config)
        if found.get("magnet"):
            try:
                publish.enqueue_auto(
                    code=code, download_url=found["magnet"],
                    title=meta.get("title", ""), cover=meta.get("cover", ""),
                    source=meta.get("source", "fc2"), detail_url=meta.get("url", ""),
                    uncensored=bool(meta.get("uncensored")),
                    auto_source=found.get("source", ""))
                added += 1
                slots -= 1
                pool.pop(code, None)       # 已发种登记在册(任务表)，离开蓄种池
            except Exception as e:
                _log(f"[{code}] 入队失败：{e}")
        else:
            meta["tries"] = int(meta.get("tries", 0)) + 1
            nomatch += 1

    pending_check = sum(1 for c in pool if not pool[c]["checked"])
    ready_cnt = len(pool) - pending_check
    _state["stats"].update({
        "last_run": time.time(), "last_added": added, "last_error": "",
        "total_added": _state["stats"]["total_added"] + added,
        "total_on_pt": _state["stats"]["total_on_pt"] + on_pt,
        "total_evicted": _state["stats"]["total_evicted"] + evicted,
    })
    _save_state()
    summary = {"added": added, "nomatch": nomatch, "on_pt": on_pt, "evicted": evicted,
               "pool": len(pool), "pending_check": pending_check, "ready": ready_cnt,
               "done_pt": len(done_pt), "slots": cap["slots"],
               "downloading": cap["downloading"], "seeding_active": cap["seeding_active"]}
    if added_pool or added or on_pt or evicted:
        _log(f"本轮：补池{added_pool} 发种{added} 站点已有{on_pt} 剔除{evicted} ｜ "
             f"蓄种池{len(pool)}(待查重{pending_check}/待发种{ready_cnt}) "
             f"PT已有库{len(done_pt)}（下载中{cap['downloading']}/做种{cap['seeding_active']}）")
    return summary


# ── 后台 worker ──
async def _worker_loop():
    _log("全自动发种 worker 启动")
    while True:
        config = load_config()
        try:
            if config.get("autopilot_fc2_enabled"):
                global _BUSY
                if not _BUSY:
                    _BUSY = True
                    try:
                        await _round(config)
                    finally:
                        _BUSY = False
        except Exception as e:
            _log(f"worker tick 异常：{e}")
        # 间隔（分钟），下限 1 分钟防误填 0 空转
        interval = max(1, int(config.get("autopilot_fc2_interval_minutes", 30) or 30))
        await asyncio.sleep(interval * 60)


def start_worker():
    global _worker_task
    _load_state()
    if _worker_task and not _worker_task.done():
        return
    try:
        loop = asyncio.get_event_loop()
        _worker_task = loop.create_task(_worker_loop())
    except Exception as e:
        _log(f"worker 启动失败：{e}")


# ── API ──
@router.get("/status")
async def api_status():
    config = load_config()
    cap = _capacity(config)
    pool = _state["pool"]
    pending_check = sum(1 for c in pool if not pool[c]["checked"])
    return {
        "success": True,
        "enabled": bool(config.get("autopilot_fc2_enabled")),
        "latest_seen": _state["stats"].get("latest_seen", 0),
        "downloading": cap["downloading"],
        "seeding_active": cap["seeding_active"],
        "slots": cap["slots"],
        "pool": len(pool),
        "pending_check": pending_check,           # 蓄种池里待 PT 查重的号数
        "ready": len(pool) - pending_check,        # 查重通过、等磁力发种的号数
        "done_pt": len(_state["done_pt"]),         # PT 已有、跳过的号数
        "interval_minutes": int(config.get("autopilot_fc2_interval_minutes", 30) or 30),
        "resource_source": config.get("autopilot_resource_source", "jackett"),
        "stats": _state["stats"],
    }


class ResetRequest(BaseModel):
    start_number: Optional[int] = None


@router.post("/reset")
async def api_reset(req: ResetRequest):
    """清空蓄种池与 PT 已有登记，并把起点下限写回配置（仅影响下一轮：号 < 起点的不入池）。
    下一轮从当前发现窗口重新补池、重新查重。"""
    config = load_config()
    start = req.start_number if req.start_number is not None \
        else int(config.get("autopilot_fc2_start_number", 0) or 0)
    start = max(0, int(start))
    _state["pool"] = {}
    _state["done_pt"] = {}
    _save_state()
    _log(f"已重置：清空蓄种池与 PT 已有登记，起点下限={start}")
    return {"success": True, "start_number": start, "pool": 0}


@router.post("/run-once")
async def api_run_once():
    """立即手动跑一轮（不等定时间隔）。受同样的节流与严格匹配约束。"""
    global _BUSY
    if _BUSY:
        return {"success": False, "error": "已有一轮正在执行，请稍后"}
    config = load_config()
    if not config.get("autopilot_fc2_enabled"):
        return {"success": False, "error": "全自动发种未启用（先在发种设置里开启并保存）"}
    _BUSY = True
    try:
        summary = await _round(config)
    finally:
        _BUSY = False
    return {"success": True, "summary": summary}
