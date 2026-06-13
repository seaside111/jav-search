"""
媒体信息读取（V1.5 Phase 3 基建）

两条用途：
  1. get_mediainfo_text()  —— 取 mediainfo 的完整文本报告，原样填进 M-Team 上传表单的
     `mediainfo` 字段（PT 站要求）。
  2. get_media_summary()   —— 解析出分辨率/编码/时长等结构化字段，供后续映射 M-Team 的
     standard/videoCodec/audioCodec 等枚举 id。

依赖容器内已 apt 安装的 `mediainfo` CLI；解析用 pymediainfo（已在 requirements）。
本机未装也不影响导入：所有外部调用都在函数内、带异常兜底。
"""
import asyncio
import shutil
from pathlib import Path


def _has_mediainfo() -> bool:
    return shutil.which("mediainfo") is not None


async def get_mediainfo_text(video_path: str, timeout: int = 60) -> dict:
    """
    取 mediainfo 完整文本报告（默认人类可读格式，PT 站通用）。
    返回 {ok, text, error}。
    """
    p = Path(video_path)
    if not p.exists():
        return {"ok": False, "text": "", "error": f"文件不存在: {video_path}"}
    if not _has_mediainfo():
        return {"ok": False, "text": "", "error": "容器内未安装 mediainfo"}
    try:
        proc = await asyncio.create_subprocess_exec(
            "mediainfo", str(p),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        return {"ok": False, "text": "", "error": "mediainfo 超时"}
    except Exception as e:
        return {"ok": False, "text": "", "error": f"mediainfo 执行失败: {e}"}
    if proc.returncode != 0:
        return {"ok": False, "text": "", "error": (err or b"").decode("utf-8", "replace")[:200]}
    return {"ok": True, "text": (out or b"").decode("utf-8", "replace").strip(), "error": ""}


async def get_media_summary(video_path: str) -> dict:
    """
    解析结构化媒体信息。返回 {ok, summary:{...}, error}。
    summary: duration_sec, width, height, video_codec, audio_codec,
             overall_bitrate, file_size。
    """
    p = Path(video_path)
    if not p.exists():
        return {"ok": False, "summary": {}, "error": f"文件不存在: {video_path}"}
    try:
        from pymediainfo import MediaInfo
    except Exception as e:
        return {"ok": False, "summary": {}, "error": f"pymediainfo 不可用: {e}"}

    def _parse():
        mi = MediaInfo.parse(str(p))
        s = {"duration_sec": 0, "width": 0, "height": 0, "video_codec": "",
             "audio_codec": "", "overall_bitrate": 0, "file_size": 0}
        for t in mi.tracks:
            if t.track_type == "General":
                s["file_size"] = int(t.file_size or 0)
                s["overall_bitrate"] = int(t.overall_bit_rate or 0)
                if t.duration:
                    s["duration_sec"] = int(float(t.duration) / 1000)
            elif t.track_type == "Video" and not s["video_codec"]:
                s["video_codec"] = (t.format or "").upper()
                s["width"] = int(t.width or 0)
                s["height"] = int(t.height or 0)
            elif t.track_type == "Audio" and not s["audio_codec"]:
                s["audio_codec"] = (t.format or "").upper()
        return s

    try:
        summary = await asyncio.to_thread(_parse)
    except Exception as e:
        return {"ok": False, "summary": {}, "error": f"解析失败: {e}"}
    return {"ok": True, "summary": summary, "error": ""}


def standard_label(height: int) -> str:
    """按高度粗分清晰度档（用于映射 standard 枚举 / 展示）。"""
    if height >= 2000:
        return "4K"
    if height >= 1000:
        return "1080p"
    if height >= 700:
        return "720p"
    if height > 0:
        return "SD"
    return ""
