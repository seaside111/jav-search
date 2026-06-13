"""
制种（V1.5 Phase 3 基建）

用纯 Python 的 torf 库给「规整后的内容」做 .torrent（免装 mktorrent）。
发种关键点：
  - private=True：私有种子（PT 必须）。
  - source=<标记>：写入 info.source（M-Team 用以区分来源、影响 infohash）。
    因 source 在 info 字典内，infohash 由「内容 + source + private + piece」决定，
    与 announce 无关 —— 所以发种时无需预先知道 announce/passkey：
    先按 source 制种确定 infohash → createOredit 上传 → 再 genDlToken 取回
    官方种子（同 infohash、已含你的 announce）做种即可。

torf 为同步库，统一用 asyncio.to_thread 包到协程里，避免阻塞事件循环。
本机未装 torf 不影响导入（函数内惰性导入）。
"""
import asyncio
from pathlib import Path


def _build(content_path: str, out_path: str, source: str, private: bool,
           announce, comment: str) -> dict:
    from torf import Torrent  # 惰性导入
    p = Path(content_path)
    if not p.exists():
        return {"ok": False, "error": f"内容不存在: {content_path}"}
    t = Torrent(path=str(p))
    t.private = bool(private)
    if source:
        t.source = source
    if announce:
        t.trackers = [announce] if isinstance(announce, str) else list(announce)
    if comment:
        t.comment = comment
    t.generate()  # 计算 piece hashes（大文件耗时）
    dst = Path(out_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    t.write(str(dst), overwrite=True)
    return {"ok": True, "path": str(dst), "infohash": t.infohash,
            "size": int(t.size or 0), "piece_size": int(t.piece_size or 0),
            "error": ""}


async def make_torrent(content_path: str, out_path: str, source: str = "M-Team",
                       private: bool = True, announce=None, comment: str = "") -> dict:
    """
    给 content_path（单文件或文件夹）制种，写到 out_path。
    返回 {ok, path, infohash, size, piece_size, error}。
    """
    try:
        return await asyncio.to_thread(
            _build, content_path, out_path, source, private, announce, comment)
    except Exception as e:
        return {"ok": False, "error": f"制种失败: {type(e).__name__}: {e}"}
