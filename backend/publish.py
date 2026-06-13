"""
发种流水线（V1.5 重心功能）

每个发种任务在后台 worker 里按状态机推进：
  QUEUED → CHECKING(查重) → DOWNLOADING(Jackett磁力) → PROCESSING(停种/刮削规整/制种/删原种/复查)
        → READY(待发布, 人工确认或 publish_auto) → UPLOADING(截图→图床→createOredit)
        → RESEED(取回官方种子做种) → SEEDING → (分享率/时长达标) STOPPED →(可选)删种/删文件
  任意步失败 → FAILED(可重试)；查重命中 → ABORTED_EXISTS；复查被抢发 → ABORTED_TAKEN

并发：publish_max_active 控制「占用流水线槽位」的任务数（从离开 QUEUED 到 STOPPED/终止）。
做种任务一直占槽直到停止条件达成，从而实现「达做种上限即排队」。

路径（V1.5 重构：seed-in-place + 硬链接归档）：
  发种工作目录＝下载目录，有两套视角——
    publish_work_dir       本项目容器视角（我们读写/规整文件用）
    publish_work_dir_host  下载器容器视角（做种 save_path 用）
  二者指向同一块物理盘的同一目录，只是两个容器各自的挂载名不同。
  规整（建番号文件夹/改名/写NFO）就在下载目录【原地】完成；做种始终留原地，
  save_path 直接用该种子下载时下载器自报的 save_path（最可靠，无需换算）。
  归档目录 publish_archive_dir 只给 EMBY 用，通过【硬链接】复制一份（跨卷自动退化为复制），
  下载器从头到尾不碰归档目录——因此归档目录不需要"下载器视角"配置。
"""
import asyncio
import json
import os
import re
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import config_manager
from config_manager import load as load_config
import downloader
import mteam
import mteam_enums
import logbus
import mediainfo as mediainfo_mod
import screenshot as screenshot_mod
import torrentmaker
import imagehost
from scrapers import search as scraper_search, SEARCH_MODE_CODE
from library import (
    VIDEO_EXTS, _safe_name, _download_image, _cover_referer, _build_nfo,
    _strip_code_prefix, _compose_title,
)

router = APIRouter(prefix="/api/publish")

# ── 状态常量 ──
QUEUED = "queued"
CHECKING = "checking"
DOWNLOADING = "downloading"
PROCESSING = "processing"
READY = "ready"            # 待人工确认发布
UPLOADING = "uploading"
SEEDING = "seeding"
STOPPED = "stopped"
ABORTED_EXISTS = "aborted_exists"
ABORTED_TAKEN = "aborted_taken"
FAILED = "failed"
CANCELLED = "cancelled"

_STATE_LABELS = {
    QUEUED: "排队中", CHECKING: "查重中", DOWNLOADING: "下载中",
    PROCESSING: "刮削制种中", READY: "待发布(点确认)", UPLOADING: "发布中",
    SEEDING: "做种中", STOPPED: "已停止", ABORTED_EXISTS: "站点已有·终止",
    ABORTED_TAKEN: "已被抢发·终止", FAILED: "失败", CANCELLED: "已取消",
}
# 前端分类用：每个状态归到一个大类
_STATE_GROUP = {
    QUEUED: "active", CHECKING: "active", DOWNLOADING: "active",
    PROCESSING: "active", UPLOADING: "active",
    READY: "ready", SEEDING: "seeding",
    STOPPED: "done", ABORTED_EXISTS: "done", ABORTED_TAKEN: "done", CANCELLED: "done",
    FAILED: "failed",
}
# 占用并发槽位的状态（活跃流水线）
_ACTIVE_STATES = {CHECKING, DOWNLOADING, PROCESSING, READY, UPLOADING, SEEDING}
# 终态
_TERMINAL = {STOPPED, ABORTED_EXISTS, ABORTED_TAKEN, CANCELLED}

# qB/TR 报告下载"完成"(progress≈1)后可能仍在 Moving/校验，文件尚未落到刮削目录。
# 此时若立刻刮削会误判"没有视频文件"。完成后先确认文件已落地，最多等这么多个 tick
# （每 tick≈publish_poll_interval 秒）再放行处理；超时仍无则进处理、由 _step_process 出带诊断的失败。
_DL_SETTLE_TICKS = 6


def _log(msg: str):
    logbus.info("发种", msg)


# ── 任务存储（内存 + /config/publish_tasks.json 持久化）──
_TASKS: dict = {}
_BUSY: set = set()          # 正在执行长步骤的任务 id，避免重入
_TASKS_PATH = config_manager.CONFIG_PATH.parent / "publish_tasks.json"
_worker_task: Optional[asyncio.Task] = None


def _save_tasks():
    try:
        _TASKS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_TASKS_PATH, "w", encoding="utf-8") as f:
            json.dump(list(_TASKS.values()), f, ensure_ascii=False, indent=2)
    except Exception as e:
        _log(f"任务持久化失败: {e}")


def _load_tasks():
    try:
        if _TASKS_PATH.exists():
            with open(_TASKS_PATH, "r", encoding="utf-8") as f:
                for t in json.load(f):
                    _TASKS[t["id"]] = t
            # 重启后把「中间态」任务标记为失败，避免卡死（用户可重试）
            for t in _TASKS.values():
                if t["state"] in (CHECKING, DOWNLOADING, PROCESSING, UPLOADING):
                    t["state"] = FAILED
                    t["error"] = "服务重启中断，请重试"
    except Exception as e:
        _log(f"任务加载失败: {e}")


def _new_task(code: str, download_url: str, title: str,
              item_meta: dict = None) -> dict:
    tid = uuid.uuid4().hex[:12]
    t = {
        "id": tid, "code": code, "download_url": download_url,
        "title": title or "", "title_jp": "", "state": QUEUED, "error": "",
        "infohash": "", "content_path": "", "torrent_path": "",
        "mteam_id": "", "confirmed": False, "created": time.time(), "updated": time.time(),
        "log": [],
        # 点击条目自带的元数据（封面/来源/该条目详情页URL），供 _scrape_meta 直接采用
        "item_meta": item_meta or {},
    }
    _TASKS[tid] = t
    _save_tasks()
    return t


def _set(t: dict, state: str = None, error: str = None, note: str = None, **kw):
    code = t.get("code", "")
    if state and state != t.get("state"):
        t["state"] = state
        logbus.info("发种", f"[{code}] → {_STATE_LABELS.get(state, state)}")  # 主要动作：状态流转
    elif state:
        t["state"] = state
    if error is not None:
        t["error"] = error
        if error:
            logbus.info("发种", f"[{code}] 错误：{error}")               # 主要动作：错误
    if note:
        t.setdefault("log", []).append(f"{time.strftime('%H:%M:%S')} {note}")
        t["log"] = t["log"][-30:]
        logbus.debug("发种", f"[{code}] {note}")                        # 细节：每一步
    for k, v in kw.items():
        t[k] = v
    t["updated"] = time.time()
    _save_tasks()


def _needs_file_protection(t: dict) -> bool:
    """该任务的原地做种文件是否仍需监控保护（不可被移动/归档）。
    - 未终止状态（排队→做种全程）：一律保护。
    - 终态特例「已停止 STOPPED 且做种种子未从下载器删除」：分享率/时长达标只是停止
      『占用流水线槽位』，但 publish_delete_after_stop=False（默认）时官方种子仍留在
      下载器里【继续做种同一批文件】——此时监控若把文件归档搬走，qB 就会卡在 99.9%/
      『等待』、PT 上掉种。故只要做种种子还在，就必须继续保护。
    - 其余终态（站点已有/被抢发/已取消、或已删做种种子的已停止）：文件不再被做种，
      可由监控正常归档。"""
    st = t.get("state")
    if st not in _TERMINAL:
        return True
    if st == STOPPED and not t.get("seed_torrent_removed"):
        return True
    return False


def active_codes() -> set:
    """供刮削监控排除：文件仍需保护的发种任务番号集合（归一化：仅字母数字小写）。
    这些番号的文件正被发种流水线管理/原地做种，监控绝不能移动或删除它们。
    收录规则见 _needs_file_protection（含「已停止但种子仍在做种」的特例）。"""
    out = set()
    for t in _TASKS.values():
        if not _needs_file_protection(t):
            continue
        c = re.sub(r"[^a-z0-9]", "", (t.get("code") or "").lower())
        if c:
            out.add(c)
    return out


def _work_root(config: dict) -> str:
    """全局下载/工作目录(本项目容器视角)，统一去尾斜杠/反斜杠。
    V1.5 统一：与刮削监控共用全局 scrape_watch_dir（发种在此原地规整/做种、监控扫同一处）；
    兼容旧配置回退 publish_work_dir。"""
    d = config.get("scrape_watch_dir") or config.get("publish_work_dir") or ""
    return d.replace("\\", "/").rstrip("/")


def _content_path_in_workdir(t: dict, config: dict) -> Optional[Path]:
    """把发种任务的下载内容映射到【刮削目录(本项目容器视角)】下的实际路径。

    单一来源：_step_process 定位下载数据、active_paths 计算占用路径都走这里，
    保证「发种读哪儿」与「监控保护哪儿」永远是同一处（两层保护互验的基础）。
    规则：取下载内容相对下载器自报 save_path 的相对路径，拼到刮削目录下；
    取不到相对路径时退化为 basename。未配置刮削目录返回 None。
    """
    our_root = _work_root(config)
    if not our_root:
        return None
    dl_save = (t.get("dl_save_path") or "").replace("\\", "/").rstrip("/")
    content = (t.get("content_path") or "").replace("\\", "/").rstrip("/")
    name = t.get("dl_name") or ""
    if not content:
        content = (dl_save + "/" + name) if (dl_save and name) else ""
    if dl_save and content.startswith(dl_save):
        rel = content[len(dl_save):].lstrip("/")
    else:
        rel = content.rsplit("/", 1)[-1] if content else ""
    return (Path(our_root) / rel) if rel else Path(our_root)


def active_paths(config: dict) -> set:
    """供刮削监控占用保护（第二层，与 active_codes 互补）：活跃发种任务在刮削目录下
    占用的实际路径集合（已 resolve）。监控扫到的视频只要落在其中任一路径（或其子树）下就跳过。

    收录两类路径：
      ① 番号做种文件夹  our_root/<safe(code)>  —— 规整后视频所在、原地做种处（code 一定有）；
      ② 原始下载内容路径 our_root/<rel>        —— 下载完成、规整前的磁力内容（拿到具体子路径才加，
         不收整个根目录，避免任务尚无 content_path 时误圈全盘、挡住正常归档）。

    与 active_codes 互验：番号识别失败但路径对得上 → 仍保护；路径未知但番号对得上 → 仍保护。
    """
    out = set()
    our_root = _work_root(config)
    if not our_root:
        return out
    root = Path(our_root)
    for t in _TASKS.values():
        if not _needs_file_protection(t):
            continue
        code = t.get("code") or ""
        if code:
            try:
                out.add((root / _safe_name(code)).resolve())
            except Exception:
                pass
        cp = _content_path_in_workdir(t, config)
        if cp is not None and cp != root:   # rel 为空时返回根目录本身，不收（见 ② 说明）
            try:
                out.add(cp.resolve())
            except Exception:
                pass
    return out


def tasks_for_monitor() -> list:
    """供监控页：含 infohash_new/mteam_id 的精简任务列表。"""
    return [{
        "id": t["id"], "code": t.get("code", ""), "title_jp": t.get("title_jp", ""),
        "mteam_id": t.get("mteam_id", ""), "infohash_new": t.get("infohash_new", ""),
        "state": t.get("state", ""), "state_label": _STATE_LABELS.get(t.get("state"), t.get("state", "")),
    } for t in _TASKS.values()]


def _public(t: dict) -> dict:
    return {**{k: t.get(k) for k in (
        "id", "code", "title", "title_jp", "state", "error", "mteam_id",
        "infohash", "infohash_new", "confirmed", "created", "updated", "log",
        "check_result", "scrape_result", "media_summary", "archive_path")},
        "state_label": _STATE_LABELS.get(t["state"], t["state"]),
        "group": _STATE_GROUP.get(t["state"], "active")}


# ── 路径映射 ──
# V1.5：读/刮削/归档统一基于 publish_work_dir（本项目容器视角的 /data 下载目录），
# 文件定位在 _step_process 第 1 步内联完成（用下载器自报的 save_path 取相对路径再拼到 /data 根）。
# 做种 save_path 直接用下载器自报的 save_path（下载器视角），不再需要"容器↔下载器"映射函数。


def _archive_for_emby(pub_folder: Path, code: str, config: dict) -> dict:
    """
    把规整好的「番号文件夹」（视频 + 封面 + NFO）复制一份到归档目录给 EMBY，
    与做种解耦——原文件夹始终留在下载目录做种，这里只是额外造一份。
    返回 {info, error}。
      - 未启用归档 / 未配置归档目录：跳过（EMBY 可直接扫描下载目录里的番号文件夹）。
      - 同一文件系统：逐文件【硬链接】（不占额外空间）。
      - 跨文件系统(EXDEV) 或显式选 copy：退化为【复制】。
      - publish_archive_by_month=True：归档落到 归档目录/YYYYMM/番号/，否则 归档目录/番号/。
    """
    # 归档总开关（全局，监控 & 发种共用）；兼容旧 publish_archive_enabled
    if not config.get("archive_enabled", config.get("publish_archive_enabled", True)):
        return {"info": "未启用归档（可让 EMBY 直接扫描下载目录）", "error": ""}
    # V1.5 统一：归档目录/模式/按年月全取全局键（与刮削监控共用），兼容旧 publish_* 回退。
    arch_root = (config.get("scrape_output_dir") or config.get("publish_archive_dir") or "").strip()
    if not arch_root:
        return {"info": "未配置归档目录，跳过归档", "error": ""}
    # 发种文件须原地做种 → 归档恒为硬链接/复制；全局选了 move 也降级为 hardlink（绝不移走做种数据）
    mode = (config.get("archive_mode") or config.get("publish_archive_mode") or "hardlink").lower()
    force_copy = mode == "copy"
    by_month = config.get("archive_by_month", config.get("publish_archive_by_month", True))
    try:
        dest = Path(arch_root)
        if by_month:
            dest = dest / datetime.now().strftime("%Y%m")
        dest = dest / _safe_name(code)
        dest.mkdir(parents=True, exist_ok=True)
        how = "复制" if force_copy else "硬链接"
        for f in pub_folder.iterdir():
            if not f.is_file():
                continue
            tgt = dest / f.name
            if tgt.exists():
                tgt.unlink()
            if force_copy:
                shutil.copy2(str(f), str(tgt))
            else:
                try:
                    os.link(str(f), str(tgt))
                except OSError:
                    shutil.copy2(str(f), str(tgt)); how = "复制(跨卷无法硬链)"
        return {"info": f"{how} → {dest}", "error": ""}
    except Exception as e:
        return {"info": "", "error": str(e)}


async def _delete_and_verify(t: dict, config: dict, infohash: str,
                             delete_files: bool = True) -> tuple[bool, str]:
    """删除种子并【回查确认】消失，最多重试 3 次。
    qB 的删除接口对不存在的 hash 也回 200（静默忽略），不能只看返回值，必须回查。"""
    ih = (infohash or "").strip().lower()
    if not ih:
        return False, "infohash 未知"
    last_err = ""
    for _ in range(3):
        res = await downloader.delete_torrents(config, [ih], delete_files=delete_files)
        if not res.get("success"):
            last_err = res.get("error", "") or last_err
        await asyncio.sleep(1.0)
        if not await _find_torrent(config, ih):
            return True, ""
    return False, last_err or "删除后种子仍在下载器列表中"


def _infohash_from_magnet(url: str) -> str:
    m = re.search(r"xt=urn:btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})", url or "")
    if not m:
        return ""
    val = m.group(1)
    if len(val) == 40:
        return val.lower()
    try:
        import base64
        return base64.b32decode(val.upper()).hex()
    except Exception:
        return ""


# ── 流水线各步 ──
async def _step_check(t: dict, config: dict) -> bool:
    """查重：站点已有该番号则终止。返回是否继续。"""
    _set(t, state=CHECKING, note="查重中")
    res = await mteam.search(config, keyword=t["code"], page_size=20)
    if not res["ok"]:
        _set(t, state=FAILED, error=f"查重失败：{res['error']}")
        return False
    # 存查重情况供详情页展示
    check_result = {
        "phase": "pre", "found": bool(res["items"]), "count": len(res["items"]),
        "items": [{"id": x.get("id", ""), "name": (x.get("name") or "")[:80]}
                  for x in res["items"][:5]],
    }
    if res["items"]:
        _set(t, check_result=check_result,
             state=ABORTED_EXISTS, note=f"站点已有 {len(res['items'])} 条，终止")
        return False
    _set(t, check_result=check_result, note="查重通过：站点暂无")
    return True


async def _step_download_start(t: dict, config: dict) -> bool:
    """推送磁力到下载器并记录 infohash。"""
    before = {x["hash"] for x in await downloader.list_torrents(config)}
    cat = (config.get("crossseed_category") or "mteam").strip()
    res = await downloader.add_torrent(config, download_url=t["download_url"],
                                       category=cat, paused=False)
    if not res.get("success"):
        _set(t, state=FAILED, error=f"推送下载失败：{res.get('error', '')}")
        return False
    # 可靠捕获下载器【实际入库】的 infohash：
    #   - Transmission 的 add 直接返回真实 hash；
    #   - qB 的 add 不返回 hash，必须用列表前后差集拿它实际算出的 hash（权威）。
    # 磁力链里解析出的 btih 仅作消歧/最后兜底——v2/混合种或 base32 磁力时 btih 可能
    # 与 qB 实际 hash 不一致，若直接拿它做删除会打到"幽灵 hash"（删除接口仍回 200）。
    magnet_ih = (_infohash_from_magnet(t["download_url"]) or "").lower()
    ih = (res.get("hash") or "").lower()
    if not ih:
        for _ in range(8):
            await asyncio.sleep(2)
            after = await downloader.list_torrents(config)
            new = [x for x in after if x["hash"] not in before]
            if new:
                # 新增多个时优先选与磁力 btih 吻合的那个
                ih = next((x["hash"] for x in new if x["hash"] == magnet_ih), new[0]["hash"])
                break
    if not ih:
        ih = magnet_ih
    # 重置落地等待计数：本次是全新下载，旧的残留计数不能让闸门提前放行
    _set(t, state=DOWNLOADING, infohash=ih, settle_ticks=0,
         note=f"已推送下载 infohash={ih[:12]}")
    return True


async def _find_torrent(config: dict, infohash: str) -> Optional[dict]:
    for x in await downloader.list_torrents(config):
        if x["hash"] == infohash:
            return x
    return None


async def _step_download_poll(t: dict, config: dict):
    """轮询下载进度；完成且文件已落地才进入 PROCESSING。"""
    tor = await _find_torrent(config, t["infohash"])
    if not tor:
        return  # 可能还没出现，下个 tick 再看
    if tor.get("progress", 0) < 0.999:
        return
    # 记录下载器自报路径（完成后才稳定，供刮削目录定位与做种 save_path 用）
    t["content_path"] = tor.get("content_path") or t.get("content_path") or ""
    t["dl_save_path"] = tor.get("save_path") or t.get("dl_save_path") or ""
    t["dl_name"] = tor.get("name") or t.get("dl_name") or ""
    # 完成后可能仍在 Moving/校验 → 确认视频已真正落到刮削目录再处理，避免"没有视频文件"误判。
    _cp, vids = _locate_download(t, config)
    if not vids:
        n = int(t.get("settle_ticks", 0)) + 1
        if n < _DL_SETTLE_TICKS:
            _set(t, settle_ticks=n,
                 note=f"下载完成，等待文件落地再处理（{n}/{_DL_SETTLE_TICKS}）")
            return
        # 超时仍无：放行进处理，由 _step_process 给出带诊断信息的失败
        _set(t, settle_ticks=n, note="等待文件落地超时，进入处理（若仍无视频将带诊断失败）")
    _set(t, note="下载完成，准备停种+刮削", content_path=t["content_path"],
         dl_save_path=t["dl_save_path"], dl_name=t["dl_name"])
    await _step_process(t, config)


def _pick_main_videos(container_path: Path, ratio: float = 0.3) -> list:
    """
    挑出所有「主视频」——既要剔除小广告/样板视频，又要保留多个大视频
    （分段 CD1/CD2、多场景等都是需要的）。
    规则：以最大视频为基准，保留 大小 >= 最大*ratio 的全部视频；至少返回最大的一个。
      - 单片：返回该片；
      - CD1(4G)+CD2(4G)+广告(50M)：阈值=1.2G，两段都保留、广告剔除；
      - 主片(4G)+广告(300M)：广告 < 1.2G，剔除。
    ratio 默认 0.3：真正的分段/多场景通常彼此体量相近，广告则远小于主片。
    """
    if container_path.is_file():
        return [container_path] if container_path.suffix.lower() in VIDEO_EXTS else []
    if not container_path.is_dir():
        return []
    vids = [p for p in container_path.rglob("*")
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
    if not vids:
        return []
    sizes = {}
    for p in vids:
        try:
            sizes[p] = p.stat().st_size
        except OSError:
            sizes[p] = 0
    largest = max(sizes.values()) if sizes else 0
    threshold = largest * ratio
    keep = [p for p in vids if sizes[p] > 0 and sizes[p] >= threshold]
    keep.sort(key=lambda p: p.name.lower())   # 稳定顺序，便于 cd1/cd2 命名
    return keep or [max(sizes, key=sizes.get)]


def _find_videos_by_code(root: Path, code: str):
    """在刮削目录下按番号兜底定位下载内容：找出 _recognize_code 命中该番号的视频，
    返回 (公共容器目录, 命中视频列表)。用于 content_path 映射与实际落盘有偏差时兜底，
    番号识别与监控同源（library._recognize_code），保证两边认的是同一份文件。
    未命中返回 (None, [])。"""
    norm = re.sub(r"[^a-z0-9]", "", (code or "").lower())
    if not norm or root is None or not root.exists():
        return None, []
    from library import _recognize_code as _rc
    hits = []
    try:
        for p in root.rglob("*"):
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                g = _rc(p, str(root))
                if g and re.sub(r"[^a-z0-9]", "", g.lower()) == norm:
                    hits.append(p)
    except Exception:
        return None, []
    if not hits:
        return None, []
    parents = {h.parent for h in hits}
    container = next(iter(parents)) if len(parents) == 1 else root
    return container, hits


def _locate_download(t: dict, config: dict):
    """定位下载内容并挑出主视频（单一来源：下载轮询闸门与 _step_process 都用它）。
    先按 content_path 映射的 cpath；命中不到再按番号在刮削目录兜底搜索。
    返回 (容器目录, 主视频列表)；都拿不到则 (cpath, [])。"""
    cpath = _content_path_in_workdir(t, config)
    if cpath is not None and cpath.exists():
        vids = _pick_main_videos(cpath)
        if vids:
            return cpath, vids
    root = Path(_work_root(config) or "")
    alt, alt_hits = _find_videos_by_code(root, t.get("code") or "")
    if alt is not None and alt_hits:
        vids = _pick_main_videos(alt)
        if vids:
            return alt, vids
    return cpath, []


def _paths_overlap(a: Path, b: Path) -> bool:
    """两个路径是否相等或互为祖先（同一棵子树）。用于判断"原下载数据"与"番号文件夹"是否重叠。"""
    try:
        a = a.resolve()
        b = b.resolve()
    except Exception:
        return False
    return a == b or a in b.parents or b in a.parents


def _clean_pub_folder(pub_folder: Path, keep_names: set) -> list:
    """
    清掉番号文件夹里除 keep_names 之外的所有文件/子目录（广告、样板、采样图等无用文件）。
    用于"下载文件夹已是番号名、广告与主视频同处一个文件夹"的情形——规整后这些无用文件
    会留在做种/制种文件夹里，必须主动剔除，否则会被打进种子、也无法靠删原种清掉。
    返回被删除的名称列表，便于记录。
    """
    removed = []
    try:
        for f in list(pub_folder.iterdir()):
            if f.name in keep_names:
                continue
            try:
                if f.is_dir():
                    shutil.rmtree(str(f))
                else:
                    f.unlink()
                removed.append(f.name)
            except Exception:
                pass
    except Exception:
        pass
    return removed


async def _scrape_meta(t: dict, config: dict) -> dict:
    """
    抓番号元数据（日文原名/演员/封面）填进任务，供详情页【提前展示】。
    入队后即可后台预抓，不必等下载完成；_step_process 会复用，避免二次抓取。
    幂等：已抓过（meta_scraped）直接返回缓存。返回 movie dict（未命中为 {}）。
    """
    if t.get("meta_scraped"):
        return t.get("movie_meta") or {}
    proxy = config.get("proxy") or None

    # ── 优先：直接采用「当前点击条目」自带的元数据 ──
    # 列表/详情上看到的封面、标题、来源、该条目详情页URL都已是用户实际选中的那一条，
    # 直接拿来用，绝不再按番号/标题二次搜索——避免错乱（尤其 FC2 番号在 javbus/javdb 查不到，
    # 二次搜索会命中无关条目或落空）。演员等需开详情页的字段，留到 _step_process 用
    # 本条目自己的 detail_url（item 自身的 url）enrich 一次补全，仍是同一条目、不会串。
    seed = t.get("item_meta") or {}
    if seed.get("cover") or seed.get("detail_url"):
        raw_title = seed.get("title", "") or t.get("title", "")
        title_jp = _strip_code_prefix(raw_title, t["code"]) if raw_title else ""
        movie = {
            "code": t["code"],
            "title": raw_title,
            "cover": seed.get("cover", ""),
            "source": seed.get("source", ""),
            "url": seed.get("detail_url", ""),
            "actors": [],
        }
        scrape_result = {
            "found": True, "title": title_jp,
            "cover": movie["cover"], "actors": [],
            "source": movie["source"],
        }
        _set(t, title_jp=title_jp, scrape_result=scrape_result,
             movie_meta=movie, meta_scraped=True,
             note=f"采用点击条目元数据[{movie['source'] or '?'}]：{title_jp[:40] or '(无标题,处理时按详情页补)'}")
        return movie

    # ── 兜底：无条目元数据（旧任务/直接调 API）才按番号搜 javbus/javdb ──
    try:
        results = await scraper_search(query=t["code"], mode=SEARCH_MODE_CODE,
                                       proxy=proxy, sources=["javbus", "javdb"])
    except Exception as e:
        _set(t, note=f"预抓元数据失败（不影响发种，稍后会重试）：{e}")
        return {}
    movie = results[0] if results else {}
    # 预抓只取列表级（标题/封面/列表自带演员），不打开详情页、不碰 FlareSolverr——
    # 详情早出更快；演员等需开详情页补全的，留到正式处理阶段(_step_process)再 enrich 一次。
    raw_title = movie.get("title", "") if movie else ""
    title_jp = _strip_code_prefix(raw_title, movie.get("code", "") or t["code"]) if movie else ""
    # 演员存名字字符串（来源解析为 {name,avatar} 对象列表，前端按字符串展示）
    actor_names = [(a.get("name", "") if isinstance(a, dict) else str(a))
                   for a in (movie.get("actors") or [])]
    actor_names = [n for n in actor_names if n][:10]
    scrape_result = {
        "found": bool(movie), "title": title_jp,
        "cover": movie.get("cover", "") if movie else "",
        "actors": actor_names,
        "source": movie.get("source", "") if movie else "",
    }
    _set(t, title_jp=title_jp, scrape_result=scrape_result,
         movie_meta=movie, meta_scraped=bool(movie),
         note=f"预抓元数据：{title_jp[:40] or '(未命中，处理时再试)'}")
    return movie


async def _step_process(t: dict, config: dict):
    """停种 → 刮削规整(番号+日文原名/封面/NFO) → 制种 → 删原磁力种 → 复查 → READY。"""
    _set(t, state=PROCESSING, note="开始刮削制种")
    proxy = config.get("proxy") or None

    # 1) 定位下载数据：读/刮削/归档全部基于【本项目容器视角的刮削目录 publish_work_dir(/data/...)】，
    #    不依赖"下载器视角"映射根——下载器的下载目录只在 ①首次下磁力 ②最后重新做种 时用，
    #    且沿用全局下载器设置(qb_save_path 等)，发种这里不再单独设。
    #    规则：刮削目录 ≡ 下载器实际保存目录（同一块物理盘的两个容器挂载名）。
    #    定位逻辑抽到 _content_path_in_workdir，与监控占用保护(active_paths)共用同一来源。
    our_root = _work_root(config)
    if not our_root:
        _set(t, state=FAILED, error="未配置下载/工作目录(全局设置 → 刮削 & 归档 → 下载/工作目录)")
        return
    dl_save = (t.get("dl_save_path") or "").replace("\\", "/").rstrip("/")   # 下载器自报 save_path（后续做种 save_path 用）
    # 定位下载内容（content_path 映射 + 番号兜底，单一来源 _locate_download，与下载轮询闸门一致）
    cpath, videos = _locate_download(t, config)
    if not videos:
        # 带诊断的失败：列出刮削目录下实际存在的视频，便于区分「路径偏差」还是「真没视频」
        try:
            listing = [str(p.relative_to(our_root)) for p in Path(our_root).rglob("*")
                       if p.is_file() and p.suffix.lower() in VIDEO_EXTS][:10]
        except Exception:
            listing = []
        if cpath is None or not cpath.exists():
            _set(t, state=FAILED,
                 error=f"在刮削目录找不到数据：{cpath}（确认刮削目录指向下载器实际保存的同一物理目录）"
                       f"｜刮削目录现有视频：{listing or '无'}")
        else:
            _set(t, state=FAILED,
                 error=f"下载内容里没有视频文件（定位={cpath}）｜刮削目录现有视频：{listing or '无'}")
        return

    # 做种【原地】：番号文件夹建在刮削目录（本项目视角）；
    #   做种 save_path 用下载器自报的 save_path（下载器视角，最可靠，免换算）；
    #   兜底取全局下载器设置的默认保存目录（即下载器的下载目录）。
    seed_dir_c = Path(our_root)
    seed_save_host = (dl_save or (config.get("publish_work_dir_host") or "").strip()
                      or downloader.default_save_path(config))
    try:
        seed_dir_c.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    # 2) 元数据：优先用入队后预抓的结果（列表级，标题/封面）；未抓到则现抓。
    if not t.get("meta_scraped"):
        await _scrape_meta(t, config)
    movie = t.get("movie_meta") or {}
    # 演员等需打开详情页才有的字段：仅此处补一次 enrich（走 FlareSolverr，只对真正发种的任务做）
    if movie and not movie.get("actors") and movie.get("url"):
        try:
            from scrapers import enrich
            enriched = await enrich([{"url": movie["url"], "source": movie.get("source", "")}], proxy=proxy)
            if enriched and enriched[0]:
                for k, v in enriched[0].items():
                    if v and not movie.get(k):
                        movie[k] = v
                t["movie_meta"] = movie
                # 同步详情页展示用的演员名
                names = [(a.get("name", "") if isinstance(a, dict) else str(a))
                         for a in (movie.get("actors") or [])]
                sr = t.get("scrape_result") or {}
                sr["actors"] = [n for n in names if n][:10]
                t["scrape_result"] = sr
        except Exception:
            pass
    title_jp = t.get("title_jp", "")
    _set(t, note=f"命中影片：{title_jp[:40] or '(无标题)'}")

    # 3) 规整：在【下载目录原地】建「番号文件夹」，视频移入其中（制种/做种都以该文件夹为单位）
    #    刮削开=视频改名番号.ext + 写 NFO/封面入文件夹；刮削关=保留原文件名、不写 NFO/封面
    #    .meta（截图/种子产物，不进种子）放工作目录根下，作番号文件夹的兄弟，不污染番号文件夹
    meta_root = Path(_work_root(config) or "") or seed_dir_c
    meta_dir = meta_root / ".meta" / t["code"]
    shots_dir = meta_dir / "shots"
    meta_dir.mkdir(parents=True, exist_ok=True)
    pub_folder = seed_dir_c / _safe_name(t["code"])   # 番号文件夹（torrent 根目录，原地）
    pub_folder.mkdir(parents=True, exist_ok=True)
    # 刮削总开关（全局，监控 & 发种共用）；兼容旧 publish_scrape_enabled
    scrape_on = config.get("scrape_meta_enabled", config.get("publish_scrape_enabled", True))
    safe_code = _safe_name(t["code"])
    multi = len(videos) > 1
    moved_videos = []
    try:
        for idx, v in enumerate(videos, start=1):
            if scrape_on:
                # 多段：番号-cd1/-cd2…（EMBY 可识别多段）；单片：番号.后缀
                vid_name = (f"{safe_code}-cd{idx}{v.suffix.lower()}" if multi
                            else f"{safe_code}{v.suffix.lower()}")
            else:
                vid_name = v.name
            dst = pub_folder / vid_name
            if str(v) != str(dst):
                if dst.exists():
                    dst.unlink()
                shutil.move(str(v), str(dst))
            moved_videos.append(dst)
    except Exception as e:
        _set(t, state=FAILED, error=f"规整移动失败：{e}")
        return
    # 主视频（最大那个）：供 mediainfo/截图/封面代表
    dst_video = max(moved_videos, key=lambda p: p.stat().st_size)
    _set(t, note=f"已规整 → {pub_folder.name}/（{len(moved_videos)} 个视频）",
         video_path=str(dst_video))

    # 4) 封面 + NFO（仅刮削开启时写入番号文件夹，随种子一起发布；供 EMBY 与帖子封面）
    cover_path = ""
    if scrape_on:
        cover_url = movie.get("cover", "") if movie else ""
        if cover_url:
            img = await _download_image(cover_url, proxy, _cover_referer(cover_url))
            if img:
                try:
                    (pub_folder / "poster.jpg").write_bytes(img)
                    cover_path = str(pub_folder / "poster.jpg")
                except Exception:
                    pass
        try:
            nfo_text = _build_nfo(movie or {"code": t["code"]}, f"{t['code']} {title_jp}".strip(), (movie or {}).get("description", ""))
            (pub_folder / f"{t['code']}.nfo").write_text(nfo_text, encoding="utf-8")
        except Exception:
            pass

    # 4.5) 清理番号文件夹内的无用文件（广告/样板/采样图）。
    #   仅刮削开启时清——此时我们已确定保留集（主视频 + NFO + 封面）。
    #   关键场景：磁力下载的文件夹名本就是番号 → 番号文件夹 == 原下载文件夹，广告与主视频同处，
    #   不清就会被打进种子、且无法靠删原种清掉（删原种会连做种数据一起删，见第 7 步）。
    if scrape_on:
        keep = {p.name for p in moved_videos}      # 保留全部主视频（含多段 CD1/CD2）
        keep.add(f"{t['code']}.nfo")
        if cover_path:
            keep.add("poster.jpg")
        removed = _clean_pub_folder(pub_folder, keep)
        if removed:
            _set(t, note=f"清理无用文件 {len(removed)} 项：{('、'.join(removed))[:80]}")

    # 5) mediainfo + 结构化摘要 + 截图
    mi = await mediainfo_mod.get_mediainfo_text(str(dst_video))
    mediainfo_text = mi.get("text", "") if mi.get("ok") else ""
    sm = await mediainfo_mod.get_media_summary(str(dst_video))
    summary = sm.get("summary", {}) if sm.get("ok") else {}
    shot_count = int(config.get("publish_screenshot_count", 6))
    ss = await screenshot_mod.take_screenshots(str(dst_video), str(shots_dir), count=shot_count)
    shot_files = ss.get("files", []) if ss.get("ok") else []
    _set(t, note=f"mediainfo={'有' if mediainfo_text else '无'} 截图 {len(shot_files)} 张"
              + (f" {summary.get('height', 0)}p {summary.get('video_codec', '')}" if summary else ""),
         mediainfo_text=mediainfo_text, media_summary=summary, source_site=(movie or {}).get("source", ""),
         cover_path=cover_path, shot_files=shot_files, nfo_path=str(pub_folder / f"{t['code']}.nfo"))

    # 6) 制种：对「番号文件夹」整体制种（含视频+封面+NFO），写 source+private
    torrent_out = meta_dir / f"{t['code']}.torrent"
    src = (config.get("mteam_source_flag") or "M-Team").strip()
    mk = await torrentmaker.make_torrent(str(pub_folder), str(torrent_out),
                                         source=src, private=True)
    if not mk["ok"]:
        _set(t, state=FAILED, error=f"制种失败：{mk['error']}")
        return
    _set(t, torrent_path=str(torrent_out), infohash_new=mk.get("infohash", ""),
         note=f"制种完成（文件夹 {pub_folder.name}/）infohash={mk.get('infohash', '')[:12]}")

    # 做种始终原地（番号文件夹就在下载目录里），save_path 用该种子的下载器视角 save_path
    #   seed_content_dir：番号文件夹的本项目容器视角路径，做种前用于校验数据确实在位
    _set(t, seed_save_host=seed_save_host, seed_content_dir=str(pub_folder),
         note=f"做种目录(下载器视角)：{seed_save_host}")

    # 6.5) 归档：额外硬链接一份到 EMBY 归档目录（与做种解耦，跨卷自动复制）
    arch = _archive_for_emby(pub_folder, t["code"], config)
    if arch["error"]:
        _set(t, note=f"归档失败（忽略，不影响发种）：{arch['error']}")
    elif arch["info"]:
        _set(t, archive_path=arch["info"], note=f"归档：{arch['info']}")

    # 7) 删除原磁力种子。是否连同文件删，取决于"原下载数据"与"番号做种文件夹"是否重叠：
    #    - 不重叠（下载文件夹名≠番号 / 单文件）：原文件夹只剩广告残留，delete_files=True 一并清掉；
    #    - 重叠（下载文件夹名就是番号 → 番号文件夹==原下载文件夹）：文件就是我们的做种数据，
    #      绝不能删文件，用 delete_files=False 只移除种子记录（广告已在第 4.5 步主动清掉）。
    #    删除后【回查确认】，避免 qB 对幽灵 hash 静默回 200 造成"假删除"。
    overlap = _paths_overlap(cpath, pub_folder)
    del_files = not overlap
    ih = (t.get("infohash") or "").strip()
    ok_del, derr = await _delete_and_verify(t, config, ih, delete_files=del_files)
    how = "仅移除记录(做种数据原地保留)" if overlap else "连同原文件(清广告残留)"
    if ok_del:
        _set(t, note=f"已删除原磁力种子 {ih[:12]}（{how}）")
    elif not ih:
        _set(t, note="原磁力种 infohash 未知，跳过删除")
    else:
        _set(t, note=f"删除原磁力种子未确认：{derr}（hash={ih[:12]}，可到下载器手动清理）")

    # 8) 发布前复查
    re_res = await mteam.search(config, keyword=t["code"], page_size=20)
    if re_res["ok"] and re_res["items"]:
        _set(t, state=ABORTED_TAKEN, note="复查发现已被抢发，终止")
        return

    # 9) READY / 自动发布（已确认或全局自动 → 直接发布，不再等待）
    if config.get("publish_auto") or t.get("confirmed"):
        _set(t, state=READY, note="已授权，自动进入发布")
        await _step_upload(t, config)
    else:
        _set(t, state=READY, note="待确认发布（可随时点确认）")


def _build_descr(t: dict, bbcodes: list, config: dict) -> str:
    parts = []
    if t.get("cover_bb"):
        parts.append(t["cover_bb"])
    if t.get("title_jp"):
        parts.append(f"[b]{t['code']}[/b] {t['title_jp']}")
    if bbcodes:
        parts.append(imagehost.build_gallery_bbcode(bbcodes, per_row=3))
    parts.append("\n[i]由 JAV Search 自动整理发布[/i]")
    return "\n\n".join(p for p in parts if p)


async def _step_upload(t: dict, config: dict):
    """截图→图床→组装→createOredit→取回官方种子做种。"""
    _set(t, state=UPLOADING, note="上传截图到图床")
    proxy = config.get("proxy") or None

    # 截图 + 封面 → 图床（按"优先图床"顺序：catbox/pixhost… 依次尝试，第一个成功即用）
    hosts = imagehost.order_hosts(config.get("image_host"))
    imgs = list(t.get("shot_files") or [])
    cover_bb = ""
    cover_err = ""
    used_host = ""
    if t.get("cover_path"):
        cu = await imagehost.upload_image(t["cover_path"], proxy=proxy, hosts=hosts)
        if cu.get("ok"):
            cover_bb = cu["bbcode"]
            used_host = cu.get("host", "")
        else:
            cover_err = cu.get("error", "")
    up = await imagehost.upload_images(imgs, proxy=proxy, hosts=hosts)
    bbcodes = up.get("bbcodes", [])
    t["cover_bb"] = cover_bb
    # 图床结果写进日志（不再静默）：用的哪个图床 + 封面 + 截图成功数 + 首个失败原因
    first_err = cover_err
    for r in up.get("results", []):
        if r.get("ok") and not used_host:
            used_host = r.get("host", "")
        if not r.get("ok") and not first_err:
            first_err = r.get("error", "")
    _set(t, note=f"图床[{used_host or '/'.join(hosts)}]：封面{'✓' if cover_bb else '✗'} "
                 f"截图{len(bbcodes)}/{len(imgs)}"
              + (f"；失败示例：{first_err[:120]}" if first_err else ""))
    if not cover_bb and not bbcodes:
        _set(t, note=f"⚠ 图床全部失败：简介将只含标题，无图。已尝试 {('、'.join(hosts))}，"
                     f"请检查代理是否可达这些图床")

    # 智能识别类型/规格（category/standard/videoCodec/audioCodec），匹配不到再兜底
    smart = {}
    try:
        smart = await mteam_enums.smart_fields(
            config, t.get("media_summary", {}), t["code"], t.get("source_site", ""))
    except Exception as e:
        _set(t, note=f"智能类型识别失败（用默认分类）：{e}")
    detected = smart.pop("_detected", {}) if smart else {}

    # 分类：手填的 publish_category 优先（识别可能不准），留空才用智能识别结果
    category = (str(config.get("publish_category") or "").strip()) or smart.get("category") or ""
    if not str(category).strip():
        _set(t, state=FAILED, error="未识别且未配置发布分类 category（发种设置里填，或点拉取分类）")
        return
    _set(t, note=f"类型识别：{detected.get('censorship','')} {detected.get('standard','')} "
                 f"{detected.get('videoCodec','')} {detected.get('audioCodec','')} "
                 f"category={category}")

    descr = _build_descr(t, bbcodes, config)
    # 主标题与副标题都用「番号 + 片名」：先剥掉片名里可能已含的番号前缀再统一加，避免缺番号或重复。
    _disp = _strip_code_prefix((t.get("title") or t.get("title_jp", "")).strip(), t["code"])
    main_title = _compose_title(t["code"], _disp)
    fields = {
        "name": main_title.strip()[:255],
        "smallDescr": f"{t['code']} {t.get('title_jp', '')}".strip()[:255],
        "descr": descr,
        "category": int(str(category)) if str(category).isdigit() else category,
        "dmmCode": (t.get("code") or "").strip().upper(),   # 番号填入 DMM 字段（M-Team 接受此格式）
        "mediainfo": t.get("mediainfo_text", ""),
        "anonymous": bool(config.get("publish_anonymous", False)),
    }
    # 智能识别到的规格字段（id），转 int
    for k in ("standard", "videoCodec", "audioCodec"):
        v = smart.get(k)
        if v:
            fields[k] = int(v) if str(v).isdigit() else v
    # 国家/地区：手填优先，否则用智能识别（JAV 自动选日本）
    countries = str(config.get("publish_countries") or "").strip() or str(smart.get("countries") or "")
    if countries:
        fields["countries"] = int(countries) if str(countries).isdigit() else countries
    try:
        torrent_bytes = Path(t["torrent_path"]).read_bytes()
    except Exception as e:
        _set(t, state=FAILED, error=f"读取种子失败：{e}")
        return
    nfo_bytes = None
    if t.get("nfo_path") and Path(t["nfo_path"]).exists():
        try:
            nfo_bytes = Path(t["nfo_path"]).read_bytes()
        except Exception:
            pass

    _set(t, note=f"提交发种：dmmCode={fields.get('dmmCode','')} category={category} "
                 f"简介图 {len(bbcodes)} 张{'＋封面' if cover_bb else ''}")
    res = await mteam.create_torrent(config, fields, torrent_bytes, nfo_bytes)
    if not res["ok"]:
        _set(t, state=FAILED, error=f"发种失败：{res['error']}")
        return
    _set(t, mteam_id=res["id"], note=f"发布成功 id={res['id']}，取回官方种子做种")
    await _step_reseed(t, config)


async def _step_reseed(t: dict, config: dict):
    """genDlToken 取回官方种子，加进下载器指向工作目录数据做种。"""
    if not t.get("mteam_id"):
        _set(t, state=SEEDING, note="无站点 id，跳过取回（已发布）")
        return
    tor = await mteam.fetch_torrent(config, t["mteam_id"])
    if not tor["ok"]:
        _set(t, state=SEEDING, error=f"取回官方种子失败（已发布，请手动做种）：{tor['error']}")
        return
    # 做种前校验：番号文件夹（本项目容器视角）确实存在，避免做种 save_path 配错时静默 0%。
    #   注意：这里只能确认"我们这侧能看到数据"；下载器侧能否按 save_path 看到同一物理目录，
    #   取决于 publish_work_dir 与下载器保存目录是否同一块物理盘——此项无法在本容器内核验。
    seed_content = t.get("seed_content_dir") or ""
    if seed_content and not Path(seed_content).exists():
        _set(t, note=f"⚠ 做种数据未找到（本项目视角）：{seed_content}"
                     f"——请检查 publish_work_dir 是否指向下载器实际保存目录")
    # 做种目录：原地做种——用该种子下载时下载器自报的 save_path（下载器视角，最可靠）；
    #   兜底取全局下载器设置里的默认保存目录（下载器自己的下载目录）。
    save_host = ((t.get("seed_save_host") or config.get("publish_work_dir_host") or "").strip()
                 or downloader.default_save_path(config))
    cat = (config.get("crossseed_category") or "mteam").strip()
    up_limit = int(config.get("publish_upload_limit_kbps", 0) or 0)
    res = await downloader.add_torrent(config, torrent_bytes=tor["content"],
                                       save_path=save_host, category=cat,
                                       skip_checking=True, paused=False,
                                       upload_limit_kbps=up_limit)
    if res.get("success"):
        _set(t, state=SEEDING, seed_started=time.time(),
             note="已加入下载器做种（已发布完成）")
    else:
        _set(t, state=SEEDING, error=f"做种添加失败（已发布）：{res.get('error', '')}")


async def _step_seed_check(t: dict, config: dict):
    """检查做种停止条件。"""
    tor = await _find_torrent(config, t.get("infohash_new") or "")
    ratio = tor.get("ratio", 0) if tor else 0
    seeding_time = tor.get("seeding_time", 0) if tor else 0
    stop_ratio = float(config.get("publish_stop_ratio", 0) or 0)
    stop_hours = float(config.get("publish_stop_hours", 72) or 0)
    hit = False
    if stop_ratio > 0 and ratio >= stop_ratio:
        hit = True
    if stop_hours > 0 and seeding_time >= stop_hours * 3600:
        hit = True
    if not hit:
        return
    # 达停止条件
    if config.get("publish_delete_after_stop"):
        del_files = bool(config.get("publish_delete_files", False))
        hashes = [h for h in [tor["hash"] if tor else ""] if h]
        if hashes:
            await downloader.delete_torrents(config, hashes, delete_files=del_files)
        # 做种种子已从下载器删除 → 文件不再被做种，置位让监控可正常归档
        _set(t, state=STOPPED, seed_torrent_removed=True,
             note=f"达停止条件(分享率{ratio:.2f})，已删除做种{'+文件' if del_files else ''}")
    else:
        # 仅停止『占用槽位』，官方种子仍留在下载器继续做种同一批文件 →
        # 不置 seed_torrent_removed，监控继续保护这些文件（见 _needs_file_protection）
        _set(t, state=STOPPED,
             note=f"达停止条件(分享率{ratio:.2f})，停止占用槽位（种子仍在下载器做种，文件继续保护）")


# ── 后台 worker ──
async def _advance(t: dict, config: dict):
    tid = t["id"]
    if tid in _BUSY:
        return
    _BUSY.add(tid)
    try:
        st = t["state"]
        if st == QUEUED:
            if await _step_check(t, config):
                await _step_download_start(t, config)
        elif st == DOWNLOADING:
            await _step_download_poll(t, config)
        elif st == UPLOADING:
            pass  # 由确认触发，不在 tick 重入
        elif st == SEEDING:
            await _step_seed_check(t, config)
    except Exception as e:
        _set(t, state=FAILED, error=f"流水线异常：{type(e).__name__}: {e}")
    finally:
        _BUSY.discard(tid)


async def _tick():
    config = load_config()
    interval = int(config.get("publish_poll_interval", 30))
    max_active = int(config.get("publish_max_active", 3))
    tasks = list(_TASKS.values())
    active = sum(1 for t in tasks if t["state"] in _ACTIVE_STATES)

    coros = []
    for t in tasks:
        st = t["state"]
        if st == QUEUED:
            if active < max_active and t["id"] not in _BUSY:
                active += 1
                coros.append(_advance(t, config))
        elif st in (DOWNLOADING, SEEDING):
            if t["id"] not in _BUSY:
                coros.append(_advance(t, config))
    if coros:
        await asyncio.gather(*coros, return_exceptions=True)
    return interval


async def _worker_loop():
    _log("发种 worker 启动")
    while True:
        try:
            interval = await _tick()
        except Exception as e:
            _log(f"worker tick 异常: {e}")
            interval = 30
        await asyncio.sleep(max(10, interval))


def start_worker():
    global _worker_task
    _load_tasks()
    if _worker_task and not _worker_task.done():
        return
    try:
        loop = asyncio.get_event_loop()
        _worker_task = loop.create_task(_worker_loop())
    except Exception as e:
        _log(f"worker 启动失败: {e}")


# ── 路由 ──
class EnqueueRequest(BaseModel):
    code: str
    download_url: str
    title: Optional[str] = ""
    # 「当前点击条目」自带的列表级元数据：后端据此直接取封面/标题/详情页，
    # 不再按番号或标题二次搜索，避免错乱（尤其 FC2 番号在 javbus/javdb 无对应）。
    cover: Optional[str] = ""
    source: Optional[str] = ""
    detail_url: Optional[str] = ""


@router.post("/enqueue")
async def api_enqueue(req: EnqueueRequest):
    if not req.code or not req.code.strip():
        raise HTTPException(status_code=400, detail="缺少番号")
    if not req.download_url or not req.download_url.strip():
        raise HTTPException(status_code=400, detail="缺少下载链接")
    item_meta = {
        "title": (req.title or "").strip(),
        "cover": (req.cover or "").strip(),
        "source": (req.source or "").strip(),
        "detail_url": (req.detail_url or "").strip(),
    }
    t = _new_task(req.code.strip(), req.download_url.strip(),
                  (req.title or "").strip(), item_meta=item_meta)
    _log(f"入队：{t['code']} ({t['id']})")
    # 入队即后台预抓元数据（日文原名/演员/封面），详情页可尽早展示，不阻塞入队响应
    try:
        asyncio.create_task(_scrape_meta(t, load_config()))
    except Exception:
        pass
    return {"success": True, "id": t["id"], "task": _public(t)}


@router.get("/tasks")
async def api_tasks():
    tasks = sorted(_TASKS.values(), key=lambda x: x["created"], reverse=True)
    pub = [_public(t) for t in tasks]
    # 各分类计数，供前端分类标签显示
    counts = {"all": len(pub), "ready": 0, "active": 0, "seeding": 0, "done": 0, "failed": 0}
    for p in pub:
        counts[p["group"]] = counts.get(p["group"], 0) + 1
    return {"success": True, "tasks": pub, "counts": counts}


@router.get("/{tid}")
async def api_task_detail(tid: str):
    t = _TASKS.get(tid)
    if not t:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"success": True, "task": _public(t)}


@router.post("/{tid}/cancel")
async def api_cancel(tid: str):
    """取消任务：标记为已取消（释放并发槽位）。做种中的任务会从下载器移除。"""
    t = _TASKS.get(tid)
    if not t:
        raise HTTPException(status_code=404, detail="任务不存在")
    if t["state"] in _TERMINAL:
        return {"success": True, "note": "已是终态"}
    # 做种中取消：尝试从下载器移除新种子（不删数据）
    if t["state"] == SEEDING and t.get("infohash_new"):
        try:
            config = load_config()
            await downloader.delete_torrents(config, [t["infohash_new"]], delete_files=False)
        except Exception:
            pass
    _set(t, state=CANCELLED, note="用户取消")
    return {"success": True}


@router.post("/{tid}/confirm")
async def api_confirm(tid: str):
    """
    确认发布——全程随时可点：
      - 任意未终止状态点确认即「预授权」，流水线跑到发布闸门时不再等待、自动发布。
      - 若任务已到 READY（待发布），立即触发发布。
    """
    t = _TASKS.get(tid)
    if not t:
        raise HTTPException(status_code=404, detail="任务不存在")
    if t["state"] in _TERMINAL:
        raise HTTPException(status_code=400, detail=f"任务已终止：{_STATE_LABELS.get(t['state'])}")
    t["confirmed"] = True
    if t["state"] == READY and tid not in _BUSY:
        config = load_config()
        _set(t, note="已确认，开始发布")
        asyncio.create_task(_confirm_run(t, config))
    else:
        _set(t, note="已确认（预授权）：流水线到发布闸门将自动发布")
    return {"success": True}


async def _confirm_run(t: dict, config: dict):
    _BUSY.add(t["id"])
    try:
        await _step_upload(t, config)
    except Exception as e:
        _set(t, state=FAILED, error=f"发布异常：{e}")
    finally:
        _BUSY.discard(t["id"])


@router.post("/{tid}/retry")
async def api_retry(tid: str):
    t = _TASKS.get(tid)
    if not t:
        raise HTTPException(status_code=404, detail="任务不存在")
    if t["state"] not in (FAILED, ABORTED_TAKEN, ABORTED_EXISTS, STOPPED, CANCELLED):
        raise HTTPException(status_code=400, detail="仅失败/终止的任务可重试")
    _set(t, state=QUEUED, error="", note="重新入队")
    return {"success": True}


@router.delete("/{tid}")
async def api_delete(tid: str):
    if tid in _TASKS:
        _TASKS.pop(tid, None)
        _save_tasks()
    return {"success": True}


@router.get("/mteam/conf")
async def api_mteam_conf():
    """拉取 M-Team 枚举（分类/国家等）并持久化，供发种自动选择 + 前端展示 id。"""
    config = load_config()
    data = await mteam_enums.refresh_conf(config)
    if not data:
        raise HTTPException(status_code=502, detail="拉取枚举失败（检查密钥/地址）")
    return {"success": True, "data": data}
