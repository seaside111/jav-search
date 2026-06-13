"""
辅种（cross-seed）编排（V1.5）

思路：本地已有某影片文件（通常是下载器已下完的） → 在 M-Team 按番号搜同一资源 →
按「总大小精确吻合」筛出候选 → 取其 .torrent → 加进当前下载器、保存目录指向本地已有
数据、触发哈希校验 → 校验 100% 即开始做种（无需重新下载/制种）。

关键约束（已记入规划）：
  - 辅种要求本地文件布局/大小与种子完全一致。jav-search 归档会重命名，故应针对下载器
    「原始下载目录」的文件，而非重命名后的归档目录。
  - 容器内看到的是映射后的路径；扫描用容器视角路径，但最终交给下载器的 save_path 必须是
    「下载器主机视角」的目录（与下载器配置一致）。前端用配置默认值预填、由用户确认。
  - 大小比对只做初筛；是否真能做种，最终以下载器重校验结果为准。
"""
from pathlib import Path
from typing import Optional

import mteam
import downloader

# 复用刮削模块里的视频扩展名与番号清洗，保持识别口径一致
from library import VIDEO_EXTS

_SCAN_HARD_CAP = 2000  # 单次扫描最多返回的文件数，防止超大目录拖死


def human_size(n: int) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.2f} TB"


def scan_dir(container_dir: str, min_size_mb: int = 100) -> dict:
    """
    扫描容器视角目录下的视频文件，返回 {ok, files:[{path,name,size,size_human}], error}。
    path 为容器内路径（仅用于展示/取大小）；辅种 save_path 由前端另填主机视角目录。
    """
    if not container_dir:
        return {"ok": False, "files": [], "error": "未指定扫描目录"}
    root = Path(container_dir)
    if not root.exists():
        return {"ok": False, "files": [],
                "error": f"目录不存在（容器视角）：{container_dir}"}
    min_bytes = max(0, int(min_size_mb or 0)) * 1024 * 1024
    out = []
    try:
        for p in root.rglob("*"):
            if len(out) >= _SCAN_HARD_CAP:
                break
            if not p.is_file() or p.suffix.lower() not in VIDEO_EXTS:
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size < min_bytes:
                continue
            out.append({
                "path": str(p),
                "name": p.name,
                "size": size,
                "size_human": human_size(size),
            })
    except Exception as e:
        return {"ok": False, "files": [], "error": f"扫描失败: {e}"}
    out.sort(key=lambda x: x["name"].lower())
    return {"ok": True, "files": out, "error": ""}


async def find_candidates(config: dict, keyword: str, target_size: int = 0,
                          mode: Optional[str] = None) -> dict:
    """
    在 M-Team 搜候选并按大小标注。返回 {ok, candidates, total, error}。
    每条候选加 size_match（与 target_size 精确相等）字段，前端据此高亮可辅种项。
    """
    res = await mteam.search(config, keyword=keyword, mode=mode or mteam.DEFAULT_MODE,
                             page_number=1, page_size=50)
    if not res["ok"]:
        return {"ok": False, "candidates": [], "total": 0, "error": res["error"]}
    target = int(target_size or 0)
    cands = []
    for it in res["items"]:
        it = dict(it)
        it.pop("raw", None)  # 不把原始大对象回传前端
        it["size_human"] = human_size(it.get("size", 0))
        it["size_match"] = bool(target and it.get("size") == target)
        cands.append(it)
    # 精确吻合的排前面
    cands.sort(key=lambda x: (not x["size_match"], x["name"].lower()))
    return {"ok": True, "candidates": cands, "total": res["total"], "error": ""}


async def apply(config: dict, torrent_id: str, save_path: str,
                paused: bool = False) -> dict:
    """
    取 M-Team 种子并加进当前下载器做辅种。
    save_path：下载器主机视角的「已有数据所在目录」。
    分类固定打 crossseed_category（受种子管理保护，永不自动删）。
    skip_checking=False：必须重校验以确认本地数据与种子吻合。
    返回 {success, message/error, hash?, downloader}。
    """
    if not torrent_id:
        return {"success": False, "error": "缺少种子 ID"}
    if not save_path or not save_path.strip():
        return {"success": False, "error": "缺少保存目录（下载器主机视角）"}

    tor = await mteam.fetch_torrent(config, torrent_id)
    if not tor["ok"]:
        return {"success": False, "error": f"取种子失败：{tor['error']}"}

    category = (config.get("crossseed_category") or "mteam").strip()
    result = await downloader.add_torrent(
        config,
        torrent_bytes=tor["content"],
        save_path=save_path.strip(),
        category=category,
        paused=paused,
        skip_checking=False,
    )
    result["downloader"] = downloader.active_type(config)
    if result.get("success"):
        result.setdefault("message", "已添加到下载器并触发校验")
        result["message"] += "；请在下载器中确认校验进度达 100% 后即开始做种"
    return result
