"""
FC2 全自动发种调度器（V1.5.1）

后台定时跑「发现 → 资源搜索 → 严格番号匹配 → 自动入队」一条龙，入队后的发布/做种
全部交给现有发种流水线（publish.py，已在 v1.5.0-beta39 做过容错加固）。本模块只负责：
  ① 定时抓 FC2 最新番号（scrapers.fc2.get_latest，sukebei+官方卖场双源，最新最全）；
  ② 对游标(cursor)之后的新番号，按设置的资源源搜种（sukebei 默认 / jackett）；
  ③ 严格番号匹配——候选标题开头番号归一化后必须与目标完全一致（含数字边界），才入队；
  ④ 节流——按现有全自动任务的「下载中 / 未成熟做种」数与每轮上限控制本轮入队个数，
     避免刷新时突然涌入大量任务把低配 VPS / qB 打爆；
  ⑤ 发现的新番号全部进「持久待办池」(pending)逐号处理，从旧到新不跳号；无精确匹配的本轮
     留池后续继续找，超重试上限才放弃并推进游标——即便某号滑出发现窗口也不会被漏掉。

状态持久化在 /config/autopilot_state.json：cursor / pending / stats。
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
from scrapers import fc2 as fc2_scraper
from scrapers._sukebei import search_sukebei
from jackett import search_jackett

router = APIRouter(prefix="/api/autopilot")

_STATE_PATH = config_manager.CONFIG_PATH.parent / "autopilot_state.json"
_worker_task: Optional[asyncio.Task] = None
_BUSY = False   # 防止 worker tick 与「立即跑一轮」并发重入

# 运行期状态（落盘）
#
# pending（V1.5.1 修复跳号）：持久「待办池」。凡发现的、号 > cursor 的新番号都落进这里，
# 携带元数据(标题/封面/url/无码标记)与已重试轮数 tries；只有【入队成功 / 已有任务 /
# 重试超限放弃】才从池里移除。这样即便某号滑出 sukebei/卖场的发现窗口（滑动窗口只含最新
# 一两百条），它仍留在待办池里被逐号处理，不会被「窗口前移」吞掉而跳号。
# cursor 语义随之收紧为「池中最小未决号 - 1」：池非空时绝不越过最小待办号，保证从旧到新
# 逐号推进、不漏号；池空（全部处理完）才推进到当前最新。
_state = {
    "cursor": 0,            # 已处理到的番号数字（只往大走；池非空时＝最小待办号-1）
    "pending": {},          # {code: {"num","title","cover","url","source","uncensored","tries"}}
    "stats": {
        "last_run": 0.0, "last_added": 0, "last_error": "",
        "total_added": 0, "total_skipped_nomatch": 0, "total_given_up": 0,
        "latest_seen": 0,
    },
}


def _log(msg: str):
    logbus.info("全自动", msg)


# ── 状态持久化 ──
def _load_state():
    try:
        if _STATE_PATH.exists():
            d = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                _state["cursor"] = int(d.get("cursor", 0) or 0)
                pending = {}
                # 新版：完整待办池（带元数据）
                for code, meta in (d.get("pending") or {}).items():
                    if isinstance(meta, dict):
                        num = _number_of(str(code))
                        if num > 0:
                            pending[str(code)] = {
                                "num": num,
                                "title": str(meta.get("title", "")),
                                "cover": str(meta.get("cover", "")),
                                "url": str(meta.get("url", "")),
                                "source": str(meta.get("source", "fc2")) or "fc2",
                                "uncensored": bool(meta.get("uncensored")),
                                "tries": int(meta.get("tries", 0) or 0),
                            }
                # 兼容旧版 pending_retry（仅番号→轮数）：并入待办池，元数据留空待下轮发现回填
                for code, tries in (d.get("pending_retry") or {}).items():
                    code = str(code)
                    if code in pending:
                        continue
                    num = _number_of(code)
                    if num > 0 and str(tries).lstrip("-").isdigit():
                        pending[code] = {"num": num, "title": "", "cover": "", "url": "",
                                         "source": "fc2", "uncensored": False,
                                         "tries": int(tries)}
                _state["pending"] = pending
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


# ── 一轮 ──
async def _round(config: dict) -> dict:
    """跑一轮全自动发现+入队。返回本轮摘要 dict。"""
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
    start_num = int(config.get("autopilot_fc2_start_number", 0) or 0)

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

    # 番号→数字，排重，记录见到的最大号
    discovered = []
    for it in (latest or []):
        code = (it.get("code") or "").strip()
        num = _number_of(code)
        if num > 0:
            discovered.append((num, code, it))
    if discovered:
        _state["stats"]["latest_seen"] = max(n for n, _, _ in discovered)

    # 首轮 cursor=0 且 start_number=0：以当前最新为起点、不回补历史
    if _state["cursor"] <= 0:
        _state["cursor"] = max(start_num, _state["stats"]["latest_seen"]) if start_num <= 0 \
            else max(start_num - 1, 0)
        _log(f"初始化游标 = {_state['cursor']}（之后只发现更新的番号）")
        _save_state()

    cursor = max(_state["cursor"], (start_num - 1) if start_num > 0 else 0)
    pending = _state["pending"]

    # ① 把本轮发现的、号 > cursor 的新番号并入持久待办池（携带元数据）。
    #    已在池中的号：用本轮更优的元数据回填（旧 pending_retry 迁移来的元数据为空时尤其重要）。
    for num, code, it in discovered:
        if num <= cursor:
            continue
        meta = pending.get(code)
        if meta is None:
            pending[code] = {
                "num": num,
                "title": it.get("title", ""), "cover": it.get("cover", ""),
                "url": it.get("url", ""), "source": it.get("source", "fc2") or "fc2",
                "uncensored": bool(it.get("uncensored_hint")), "tries": 0,
            }
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

    # ② 处理待办池：严格从旧到新逐号推进（不跳号）。slots 仅由「成功入队」消耗，
    #    无精确匹配(nomatch)不占名额，故一个卡住的旧号不会挡住后面的号入队。
    retry_rounds = int(config.get("autopilot_retry_rounds", 5) or 0)
    cap = _capacity(config)
    slots = cap["slots"]
    added, nomatch, gaveup = 0, 0, 0

    for code in sorted(pending, key=lambda c: pending[c]["num"]):
        meta = pending[code]
        num = meta["num"]
        if slots <= 0:
            break
        # 已有未终止任务的番号：移出池（去重，避免重复入队）
        if publish.has_active_code(code):
            pending.pop(code, None)
            continue
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
                pending.pop(code, None)
            except Exception as e:
                _log(f"[{code}] 入队失败：{e}")
        else:
            # 无精确匹配：累计重试轮数，超上限才放弃移出池（推进游标越过它）
            meta["tries"] = int(meta.get("tries", 0)) + 1
            if retry_rounds > 0 and meta["tries"] > retry_rounds:
                pending.pop(code, None)
                gaveup += 1
                _log(f"[{code}] 超 {retry_rounds} 轮仍无精确匹配，放弃")
            else:
                nomatch += 1

    # ③ 推进游标：池非空时＝最小未决号 - 1（绝不越过仍待办的号，杜绝跳号）；
    #    池空（全部处理完）则推进到当前最新，之后只发现更新的号。
    if pending:
        _state["cursor"] = min(m["num"] for m in pending.values()) - 1
    else:
        _state["cursor"] = max(_state["cursor"], _state["stats"]["latest_seen"])

    _state["stats"].update({
        "last_run": time.time(), "last_added": added, "last_error": "",
        "total_added": _state["stats"]["total_added"] + added,
        "total_skipped_nomatch": _state["stats"]["total_skipped_nomatch"] + nomatch,
        "total_given_up": _state["stats"]["total_given_up"] + gaveup,
    })
    _save_state()
    summary = {"added": added, "nomatch": nomatch, "gaveup": gaveup,
               "cursor": _state["cursor"], "slots": cap["slots"],
               "downloading": cap["downloading"], "seeding_active": cap["seeding_active"],
               "pending": len(pending)}
    if added or nomatch or gaveup:
        _log(f"本轮：新增{added} 待重试{nomatch} 放弃{gaveup} 待办池{len(pending)} "
             f"游标→{_state['cursor']}（下载中{cap['downloading']}/做种{cap['seeding_active']}）")
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
    return {
        "success": True,
        "enabled": bool(config.get("autopilot_fc2_enabled")),
        "cursor": _state["cursor"],
        "latest_seen": _state["stats"].get("latest_seen", 0),
        "downloading": cap["downloading"],
        "seeding_active": cap["seeding_active"],
        "slots": cap["slots"],
        "pending": len(_state["pending"]),
        "pending_retry": len(_state["pending"]),   # 兼容旧前端字段名
        "interval_minutes": int(config.get("autopilot_fc2_interval_minutes", 30) or 30),
        "resource_source": config.get("autopilot_resource_source", "jackett"),
        "stats": _state["stats"],
    }


class ResetRequest(BaseModel):
    start_number: Optional[int] = None


@router.post("/reset")
async def api_reset(req: ResetRequest):
    """重置游标到指定起点（或配置里的起点）并清空待办池。下一轮从该起点之后重新发现。"""
    config = load_config()
    start = req.start_number if req.start_number is not None \
        else int(config.get("autopilot_fc2_start_number", 0) or 0)
    _state["cursor"] = max(0, int(start) - 1) if start > 0 else 0
    _state["pending"] = {}
    _save_state()
    _log(f"游标已重置：起点={start}（cursor={_state['cursor']}）")
    return {"success": True, "cursor": _state["cursor"]}


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
