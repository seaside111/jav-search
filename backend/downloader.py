"""
下载器抽象调度层（V1.5）

把 qBittorrent 与 Transmission 统一到一组与具体后端无关的异步接口，
按配置项 downloader_type（"qb" | "transmission"）分发。
现有的 qB 推送、刮削、辅种、种子管理全部走这一层，新增下载器只需在此加分支。

各后端模块（qbittorrent.py / transmission.py）暴露同形函数：
  get_version / list_torrents / add_torrent / delete_torrents
本层负责：读配置取出对应后端的连接参数、补默认保存目录/分类、统一返回结构。
"""
from typing import Optional

import qbittorrent
import transmission

QB = "qb"
TRANSMISSION = "transmission"


def active_type(config: dict) -> str:
    """当前启用的下载器类型，默认 qBittorrent（向后兼容老配置）。"""
    t = (config.get("downloader_type") or QB).strip().lower()
    return TRANSMISSION if t in ("tr", "transmission") else QB


def _conn(config: dict, t: str) -> tuple[str, str, str]:
    """取出指定后端的 (url, username, password)。"""
    if t == TRANSMISSION:
        return (config.get("tr_url", "") or "").strip(), \
               config.get("tr_username", "") or "", \
               config.get("tr_password", "") or ""
    return (config.get("qb_url", "") or "").strip(), \
           config.get("qb_username", "") or "", \
           config.get("qb_password", "") or ""


def default_save_path(config: dict, t: Optional[str] = None) -> str:
    t = t or active_type(config)
    key = "tr_save_path" if t == TRANSMISSION else "qb_save_path"
    return (config.get(key, "") or "").strip()


def default_category(config: dict, t: Optional[str] = None) -> str:
    t = t or active_type(config)
    key = "tr_category" if t == TRANSMISSION else "qb_category"
    return (config.get(key, "") or "").strip()


def is_configured(config: dict) -> bool:
    url, _u, _p = _conn(config, active_type(config))
    return bool(url)


async def get_status(config: dict) -> dict:
    """连通性/版本检测。返回 {configured, online, type, message, version?}。"""
    t = active_type(config)
    url, user, pwd = _conn(config, t)
    if not url:
        return {"configured": False, "online": False, "type": t, "message": "未配置"}
    if t == TRANSMISSION:
        res = await transmission.get_version(url, user, pwd)
    else:
        res = await qbittorrent.get_version(url, user, pwd)
    res["configured"] = True
    res["type"] = t
    return res


async def list_torrents(config: dict) -> list:
    """列出当前下载器全部种子（统一字段结构）。"""
    t = active_type(config)
    url, user, pwd = _conn(config, t)
    if not url:
        return []
    if t == TRANSMISSION:
        return await transmission.list_torrents(url, user, pwd)
    return await qbittorrent.list_torrents(url, user, pwd)


async def add_torrent(
    config: dict,
    download_url: str = "",
    torrent_bytes: Optional[bytes] = None,
    save_path: Optional[str] = None,
    category: Optional[str] = None,
    paused: bool = False,
    skip_checking: bool = False,
    upload_limit_kbps: int = 0,
    reannounce: bool = True,
    public_trackers: bool = True,
) -> dict:
    """
    向当前下载器添加种子（链接或字节）。save_path/category 为 None 时用配置默认值。
    upload_limit_kbps>0 时给该种子设单种上传限速（防超 PT 单种限速被封）。
    reannounce=True 时加种后自动强制 tracker 重新汇报，规避「工作中却无 peer」。
    public_trackers=True 给磁力/普通种子补公共 tracker；发种取回的官方 PT 种子须传 False。
    """
    t = active_type(config)
    url, user, pwd = _conn(config, t)
    if not url:
        return {"success": False, "error": f"未配置下载器（{t}）地址"}
    sp = default_save_path(config, t) if save_path is None else save_path
    cat = default_category(config, t) if category is None else category
    paused = bool(paused if paused is not None else config.get("qb_paused", False))

    # 加裸磁力前确保公共 tracker 列表是新的（TTL 内零成本；qB/TR 共用这份内存态）
    try:
        await qbittorrent.ensure_trackers_fresh(config)
    except Exception:
        pass

    if t == TRANSMISSION:
        return await transmission.add_torrent(
            url, user, pwd, download_url=download_url, torrent_bytes=torrent_bytes,
            save_path=sp, category=cat, paused=paused, skip_checking=skip_checking,
            upload_limit_kbps=upload_limit_kbps, reannounce=reannounce,
            public_trackers=public_trackers,
        )
    return await qbittorrent.add_torrent(
        url, user, pwd, download_url=download_url, torrent_bytes=torrent_bytes,
        save_path=sp, category=cat, paused=paused, skip_checking=skip_checking,
        upload_limit_kbps=upload_limit_kbps, reannounce=reannounce,
        public_trackers=public_trackers,
    )


# qBittorrent 表示「下载已完成」（做种/已完成阶段）的状态。
# qB 凡处于上传/做种阶段的 state 都以 "UP" 结尾（uploading 例外），含暂停做种 pausedUP、
# 排队做种 queuedUP、卡住做种 stalledUP、强制做种 forcedUP、对完成数据再校验 checkingUP。
_QB_DONE_STATES = {"uploading", "completed"}
# Transmission status 整数语义：5=排队做种 6=做种。
_TR_SEEDING_STATES = {5, 6}


def is_download_complete(config: dict, torrent: dict) -> bool:
    """以下载器自身上报的【状态】为第一依据判断磁力/种子是否「下载完成」。

    仅当下载器明确处于「做种中 / 已完成」状态时才算完成，绝不依据文件大小（progress
    本质是 已下载字节/总字节 的比例）或文件名来判断——避免「选择性下载」「分片刚到 99%」
    等场景被误判为完成。

    - qBittorrent：state 以 "UP" 结尾（pausedUP/queuedUP/stalledUP/forcedUP/checkingUP）
      或为 uploading / completed 即视为完成。
    - Transmission：status 为 5（排队做种）或 6（做种）视为完成；
      已停止(0)但已下满(percentDone≈1，即用户主动停种或达比率自动停)亦算完成。
    """
    t = active_type(config)
    state = torrent.get("state")
    if t == TRANSMISSION:
        try:
            st = int(state)
        except (TypeError, ValueError):
            return False
        if st in _TR_SEEDING_STATES:
            return True
        # 停止态需借助完成度区分「下完后停种」与「未下完被停」——这是状态判断的必要补充，
        # 而非以大小为第一依据：仅在状态已是「停止」时才看是否下满。
        if st == 0 and float(torrent.get("progress") or 0.0) >= 0.999:
            return True
        return False
    # qBittorrent
    s = (state or "").strip()
    return s in _QB_DONE_STATES or s.endswith("UP")


async def delete_torrents(config: dict, hashes: list,
                          delete_files: bool = False) -> dict:
    """删除种子（可选连数据）。供种子管理使用。"""
    t = active_type(config)
    url, user, pwd = _conn(config, t)
    if not url:
        return {"success": False, "error": f"未配置下载器（{t}）地址"}
    if t == TRANSMISSION:
        return await transmission.delete_torrents(url, user, pwd, hashes, delete_files)
    return await qbittorrent.delete_torrents(url, user, pwd, hashes, delete_files)
