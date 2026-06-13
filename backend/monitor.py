"""
监控（V1.5）

两路数据合一：
  - 全局：M-Team 会员资料 + 做种统计 + 魔力值（/member/profile、/tracker/myPeerStatistics、
    /tracker/mybonus；字段未文档化，尽力提取常见字段并附 raw 供前端兜底展示）。
  - 单种：发种任务（publish）× 下载器实时（上传速度/已传/分享率，按 infohash_new 匹配）
    × 站点审核状态（/member/getUserTorrentList 按 mteam_id 匹配）。
"""
import time

from fastapi import APIRouter

from config_manager import load as load_config
import downloader
import mteam
import publish

router = APIRouter(prefix="/api/monitor")

# 服务端缓存：避免监控页轮询把 M-Team 会员/统计接口打爆（站点有通用限流）。
# 即使前端每秒刷，M-Team 实际调用也被压到每 TTL 一次。
_GLOBAL_TTL = 60   # 全局数据 60s
_SITE_TTL = 60     # 我的种子(审核状态) 60s
_cache = {"global": {"ts": 0.0, "data": None}, "site": {"ts": 0.0, "data": None}}


def _pick(src: dict, *keys):
    if not isinstance(src, dict):
        return None
    for k in keys:
        v = src.get(k)
        if v not in (None, ""):
            return v
    return None


@router.get("/global")
async def api_global():
    # 命中缓存直接返回（压低 M-Team 调用频率）
    c = _cache["global"]
    if c["data"] and (time.time() - c["ts"] < _GLOBAL_TTL):
        return {**c["data"], "cached": True}
    config = load_config()
    prof = await mteam.member_profile(config)
    peer = await mteam.peer_statistics(config)
    bonus = await mteam.mybonus(config)
    p = prof.get("data") or {}
    pe = peer.get("data") or {}
    b = bonus.get("data") or {}
    # M-Team 资料常把统计放在 memberCount 子对象
    mc = p.get("memberCount") if isinstance(p.get("memberCount"), dict) else p
    out = {
        "success": True,
        "configured": bool((config.get("mteam_uid") or "").strip()),
        "username": _pick(p, "username", "userName", "name"),
        "uploaded": _pick(mc, "uploaded", "upload", "uploadByte"),
        "downloaded": _pick(mc, "downloaded", "download", "downloadByte"),
        "ratio": _pick(mc, "shareRate", "ratio"),
        "bonus": _pick(mc, "bonus") or _pick(b, "bonus", "karma", "point"),
        "seeding": _pick(pe, "seeder", "seeding", "seedCount", "uploadCount"),
        "leeching": _pick(pe, "leecher", "leeching", "leechCount", "downloadCount"),
        "errors": {"profile": prof.get("error", ""), "peer": peer.get("error", ""),
                   "bonus": bonus.get("error", "")},
        "raw": {"profile": p, "peer": pe, "bonus": b},
    }
    _cache["global"] = {"ts": time.time(), "data": out}
    return out


@router.get("/seeds")
async def api_seeds():
    config = load_config()
    tasks = publish.tasks_for_monitor()
    dl = await downloader.list_torrents(config)          # 本地下载器，便宜，每次取新
    dl_by_hash = {x["hash"]: x for x in dl}
    # 站点审核状态走缓存（M-Team 接口，避免轮询打爆）
    sc = _cache["site"]
    if sc["data"] and (time.time() - sc["ts"] < _SITE_TTL):
        site = sc["data"]
    else:
        site = await mteam.user_torrent_list(config)
        _cache["site"] = {"ts": time.time(), "data": site}
    site_by_id = {s["id"]: s for s in site.get("items", [])}

    rows = []
    for t in tasks:
        # 只关心已发布/做种中的任务
        if not t["mteam_id"] and t["state"] not in ("seeding", "uploading", "ready"):
            continue
        d = dl_by_hash.get((t["infohash_new"] or "").lower(), {})
        s = site_by_id.get(t["mteam_id"], {}) if t["mteam_id"] else {}
        rows.append({
            "code": t["code"], "title_jp": t["title_jp"], "mteam_id": t["mteam_id"],
            "state_label": t["state_label"],
            "upspeed": d.get("upspeed", 0), "uploaded": d.get("uploaded", 0),
            "ratio": d.get("ratio", 0), "seeding_time": d.get("seeding_time", 0),
            "in_downloader": bool(d),
            "site_status": s.get("status", ""), "site_visible": s.get("visible"),
        })
    return {"success": True, "rows": rows,
            "site_ok": site.get("ok", False), "site_error": site.get("error", "")}
