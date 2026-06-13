"""
视频截图（V1.5 Phase 3 基建）

用 ffmpeg 在视频时间轴上等分取 N 张截图（跳过头尾各 ~5%，避开黑场/片头），
供发种时上传图床、贴进 M-Team 简介。可选生成 contact sheet（缩略图拼图）。

依赖容器内已 apt 安装的 ffmpeg/ffprobe。本机未装不影响导入。
"""
import asyncio
import shutil
from pathlib import Path


def _has(bin_name: str) -> bool:
    return shutil.which(bin_name) is not None


async def _probe_duration(video_path: str, timeout: int = 30) -> float:
    """用 ffprobe 取时长（秒）。失败返回 0。"""
    if not _has("ffprobe"):
        return 0.0
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(video_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return float((out or b"").decode().strip() or 0)
    except Exception:
        return 0.0


async def take_screenshots(video_path: str, out_dir: str, count: int = 6,
                           fmt: str = "jpg", timeout: int = 120) -> dict:
    """
    等分截 count 张图。返回 {ok, files:[路径...], error}。
    """
    p = Path(video_path)
    if not p.exists():
        return {"ok": False, "files": [], "error": f"文件不存在: {video_path}"}
    if not _has("ffmpeg"):
        return {"ok": False, "files": [], "error": "容器内未安装 ffmpeg"}
    out = Path(out_dir)
    try:
        out.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return {"ok": False, "files": [], "error": f"无法创建输出目录: {e}"}

    count = max(1, min(int(count or 6), 20))
    duration = await _probe_duration(video_path)
    if duration <= 0:
        return {"ok": False, "files": [], "error": "无法获取视频时长（ffprobe 失败）"}

    # 跳过头尾 5%，在中间 90% 区间等分取点
    start, end = duration * 0.05, duration * 0.95
    span = max(0.0, end - start)
    files = []
    for i in range(count):
        ts = start + span * (i + 0.5) / count
        dst = out / f"shot_{i + 1:02d}.{fmt}"
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-ss", f"{ts:.2f}", "-i", str(p),
                "-frames:v", "1", "-q:v", "2", str(dst),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _o, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except Exception as e:
            return {"ok": False, "files": files, "error": f"第 {i + 1} 张截图失败: {e}"}
        if proc.returncode == 0 and dst.exists():
            files.append(str(dst))
    if not files:
        return {"ok": False, "files": [], "error": "未生成任何截图"}
    return {"ok": True, "files": files, "error": ""}


async def make_contact_sheet(video_path: str, out_path: str, grid: str = "3x3",
                             width: int = 1200, timeout: int = 180) -> dict:
    """
    生成 contact sheet（缩略图拼图）。grid 形如 "3x3"。返回 {ok, path, error}。
    """
    p = Path(video_path)
    if not p.exists():
        return {"ok": False, "path": "", "error": f"文件不存在: {video_path}"}
    if not _has("ffmpeg"):
        return {"ok": False, "path": "", "error": "容器内未安装 ffmpeg"}
    try:
        cols, rows = (int(x) for x in grid.lower().split("x"))
    except Exception:
        cols, rows = 3, 3
    n = cols * rows
    duration = await _probe_duration(video_path)
    if duration <= 0:
        return {"ok": False, "path": "", "error": "无法获取视频时长"}
    # 每隔 duration/n 取一帧，tile 拼成网格
    interval = max(1, int(duration / (n + 1)))
    tile_w = max(120, int(width / cols))
    vf = f"fps=1/{interval},scale={tile_w}:-1,tile={cols}x{rows}"
    dst = Path(out_path)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(p), "-vf", vf, "-frames:v", "1", str(dst),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _o, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except Exception as e:
        return {"ok": False, "path": "", "error": f"contact sheet 失败: {e}"}
    if proc.returncode == 0 and dst.exists():
        return {"ok": True, "path": str(dst), "error": ""}
    return {"ok": False, "path": "", "error": (err or b"").decode("utf-8", "replace")[:200]}
