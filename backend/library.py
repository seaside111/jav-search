"""
媒体库刮削（V1.4）

职责：
  1. 扫描/监控下载器保存目录，找出「下载完成」的视频文件
  2. 对完成的文件刮削元数据（番号→搜索→翻译中文标题/简介）
  3. 写 Emby/Kodi 兼容的 NFO + 封面（poster/fanart）
  4. 刮削后（无论成功与否，按配置）把视频及其附属文件移动到归档目录，
     在归档目录下按当前年月（如 202605）建子目录存放

提供：
  - FastAPI 路由（手动扫描/刮削/查看监控状态/立即触发一次）
  - 后台监控协程 start_monitor()/stop_monitor()，由主程序在启动事件中拉起
"""
import asyncio
import array
import difflib
import errno
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Optional
from xml.dom import minidom

import httpx
from fastapi import APIRouter, HTTPException
from PIL import Image
from pydantic import BaseModel

from config_manager import load as load_config
from scrapers import search, SEARCH_MODE_CODE
from translator import translate

router = APIRouter(prefix="/api/library")


def _log(msg: str):
    """
    统一刮削日志输出（带时间戳，强制 flush 以便实时出现在 docker logs）。
    必须绝不抛异常：某些环境 stdout 编码非 UTF-8，print 中文/符号会 UnicodeEncodeError，
    若不吞掉会中断刮削/移动流程。这里做多重兜底。
    """
    line = f"[刮削 {datetime.now().strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        try:
            sys.stdout.buffer.write((line + "\n").encode("utf-8", "replace"))
            sys.stdout.flush()
        except Exception:
            pass

# 支持的视频扩展名
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".wmv", ".mov", ".ts", ".m2ts", ".rmvb", ".flv", ".iso"}

# 文件名里常见的站点/广告前缀噪声（会污染番号识别），如
#   hhd800.com@ / [javbus.com] / www.xxx.cc- / (98tang.com)
_SITE_NOISE = re.compile(
    r'(?:www\.)?[a-z0-9][a-z0-9-]*\.'
    r'(?:com|net|cc|xyz|tv|me|app|org|co|info|vip|club|site|top|fun|gg|la|cn|io|onl)'
    r'(?:@|[-_\s.])*',
    re.IGNORECASE,
)


def _clean_noise(stem: str) -> str:
    """去掉文件名里的方括号/圆括号标签与站点域名前缀，留下真正的番号上下文。"""
    s = re.sub(r'\[[^\]]*\]', ' ', stem)
    s = re.sub(r'\([^)]*\)', ' ', s)
    s = _SITE_NOISE.sub(' ', s)
    return s


# 结尾分集标记：番号_1 / 番号-cd2 / 番号_part3 / 番号A（紧跟）。
# 番号正则用 \b 收尾，而 `_` 是单词字符，故 `番号_N` 这类分集文件名整串识别不出（FC2/无码尤为常见）。
# 识别失败时剥掉此尾巴再试（见 _code_from_name），并以「剥后仍能匹配出番号」为护栏，
# 避免误伤把番号尾段当分集（如日期码 060226_01 剥成 060226 匹配不出 → 不采用、保持原样）。
_TRAILING_PART = re.compile(
    r'[._\s-]*'                                          # 番号与分集标记间的分隔符
    r'(?:(?:cd|dvd|disc|disk|part|pt|vol)[._\s-]?)?'     # 可选 CD/DVD/DISC/PART/VOL 关键词
    r'(?:\d{1,2}|[a-e])'                                 # 分集序号：1~2 位数字 或 单个字母 A~E
    r'\s*$',
    re.IGNORECASE,
)


def _strip_trailing_part(name: str) -> str:
    """剥掉文件名结尾的一个分集标记（_1 / -cd2 / 末尾A 等）；无则原样返回。"""
    return _TRAILING_PART.sub('', name, count=1)


# 番号正则：匹配 ABP-123、SSIS001、FC2-PPV-1234567、390JAC-234，以及无码格式
# （10musume/1pondo/Carib 060226_01、heydouga-4017-001）等。
# 顺序很重要：更「专」「长」的格式排在前，避免被宽松规则截断（如 heydouga 不被截成 HEYDOUGA-4017）。
_CODE_PATTERNS = [
    re.compile(r'\b(FC2-?PPV-?\d{5,8})(?![0-9])', re.IGNORECASE),        # FC2-PPV-1234567（容许 _1/_2 分集尾巴）
    re.compile(r'\b([A-Z]{3,10}-\d{3,5}-\d{2,4})\b', re.IGNORECASE),     # heydouga-4017-001（厂牌-数字-数字）
    re.compile(r'\b(\d{3,4}[A-Z]{2,6}-\d{2,5})\b', re.IGNORECASE),       # 390JAC-234 / 259LUXU-1234
    re.compile(r'\b([A-Z]{2,8}-\d{2,6})\b', re.IGNORECASE),              # ABP-123
    re.compile(r'\b([A-Z]{2,8})[-_]?(\d{2,6})\b', re.IGNORECASE),        # ABP123 / ABP_123
    # 无码「日期型」番号：10musume 060226_01 / 1pondo 060226_001 / Caribbean 060226-001 等。
    # 放最后、纯数字型，优先级最低，避免误吃文件名里的其它数字串；要求 6 位日期 + 分隔符。
    re.compile(r'\b(\d{6}[-_]\d{2,4})\b'),
]

# ─────────────────────────────────────────
# 监控/任务运行时状态（内存）
# ─────────────────────────────────────────
_scrape_jobs: dict[str, dict] = {}

# 文件大小稳定性追踪： path -> [last_size, stable_count]
_size_history: dict[str, list] = {}
# 过小文件仍每轮检查是否变大，但相同大小/mtime 只记录一次，避免广告文件刷屏。
_small_log_history: dict[str, tuple[int, int]] = {}
# 已处理（移动走或失败记录过）的文件路径，避免重复处理（进程内）
_processed: set[str] = set()

# ── 已归档状态持久化（修复 hardlink/copy 归档保留原文件 → 重启后被重复刮削/归档）──
# move 模式原文件被移走，下次扫描自然找不到；但 hardlink/copy 故意保留原文件做种，
# 仅靠内存 _processed 在容器重启后会清空，导致监控把早已归档的文件当新文件反复处理
# （还会因归档路径用「当前年月」而落进新月份目录重复堆叠）。
# 这里把「已归档」签名落盘到 CONFIG_DIR，键 = 解析后路径|文件大小，重启后仍能跳过。
_PROCESSED_FILE = Path(os.getenv("CONFIG_DIR", "/config")) / "scrape_processed.json"
_processed_sig: dict[str, float] = {}     # signature -> 处理时间戳
_processed_loaded = False
_PROCESSED_MAX = 20000                     # 上限：超出按时间裁掉最旧（极少触发；裁掉的最旧文件若仍在会被再处理一次）

# 已归档但缺 poster/fanart 的低频补全队列。任务记录最终归档目录，后续只补图片，
# 不重复翻译、写 NFO、归档视频或触发演员同步。
_ARTWORK_PENDING_FILE = Path(os.getenv("CONFIG_DIR", "/config")) / "scrape_artwork_pending.json"
_ARTWORK_TERMINAL_FILE = Path(os.getenv("CONFIG_DIR", "/config")) / "scrape_artwork_terminal.json"
_artwork_pending: dict[str, dict] = {}
_artwork_pending_loaded = False
_artwork_terminal: dict[str, list[str]] = {}
_artwork_terminal_loaded = False
_artwork_legacy_last = 0.0


def _file_sig(video_path: Path, size: int) -> str:
    """文件的去重签名：解析后绝对路径 + 字节大小（归档不改源文件大小，签名稳定）。"""
    try:
        rp = str(video_path.resolve())
    except Exception:
        rp = str(video_path)
    return f"{rp}|{size}"


def _load_processed() -> None:
    """首次扫描前从磁盘载入已归档签名（幂等）。"""
    global _processed_loaded
    if _processed_loaded:
        return
    _processed_loaded = True
    try:
        data = json.loads(_PROCESSED_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _processed_sig.update({str(k): float(v) for k, v in data.items()})
            _log(f"载入已归档记录 {len(_processed_sig)} 条（重启后避免重复刮削/归档）")
    except FileNotFoundError:
        pass
    except Exception as e:
        _log(f"载入已归档记录失败（忽略）：{e}")


def _save_processed() -> None:
    try:
        if len(_processed_sig) > _PROCESSED_MAX:
            for k in sorted(_processed_sig, key=lambda k: _processed_sig[k])[:len(_processed_sig) - _PROCESSED_MAX]:
                _processed_sig.pop(k, None)
        _PROCESSED_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PROCESSED_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_processed_sig, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_PROCESSED_FILE)
    except Exception as e:
        _log(f"保存已归档记录失败（忽略）：{e}")


def _mark_processed(video_path: Path, size: int) -> None:
    """把文件标记为已归档（内存 + 持久化）。"""
    _processed.add(str(video_path))
    _processed_sig[_file_sig(video_path, size)] = time.time()
    _save_processed()

_monitor_task: Optional[asyncio.Task] = None
_monitor_state: dict = {
    "running": False,
    "enabled": False,
    "last_scan": "",
    "scanning": False,
    "processed_total": 0,
    "recent": [],          # 最近处理结果（最多 30 条）
    "watch_dir": "",
    "output_dir": "",
    "message": "未启动",
}


# ─────────────────────────────────────────
# 请求模型
# ─────────────────────────────────────────

class ScanRequest(BaseModel):
    folder_path: str


class ScrapeRequest(BaseModel):
    filepath: str
    overwrite: bool = False
    move: bool = False
    translate_provider: Optional[str] = None


# ─────────────────────────────────────────
# 工具函数：番号/解析/NFO
# ─────────────────────────────────────────

def _norm(s: str) -> str:
    """归一化：转小写、仅保留字母数字（去掉分隔符/符号/空格），便于跨候选名比对去重。"""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _match_code(text: str) -> str:
    for pat in _CODE_PATTERNS:
        m = pat.search(text)
        if m:
            if len(m.groups()) == 2:
                return f"{m.group(1).upper()}-{m.group(2)}"
            return m.group(1).upper()
    return ""


def _code_from_name(name: str) -> str:
    """从单个名字（文件名去扩展 / 目录名）识别番号：先剔除站点前缀等噪声再匹配。
    回退时**仍剥掉站点/广告域名**（只保留方括号内容），避免把 hhd800.com 这类广告域名
    误当成番号（如 hhd800.com@060226_01 被识别成 HHD-800）。"""
    def _best(n: str) -> str:
        # 先去方括号+站点域名匹配；不中再保留方括号(番号可能在[]内)仅去广告域名匹配。
        return _match_code(_clean_noise(n)) or _match_code(_SITE_NOISE.sub(' ', n))

    full = _best(name)
    # 整串若被结尾分集标记(_1 / _cd2 / 末尾A 等)的 `_` 等截断 \b，会导致：
    #   ① 完全识别不出（如 ABP-123_2）；或 ② 被更宽松的规则截成更短的错号
    #      （如 heydouga-4017-001_2 误成 HEYDOUGA-4017）。
    # 故再用「剥掉结尾分集标记」的版本匹配一次，取更长(更具体)的番号。
    # 护栏：剥后匹配不出就不采用（日期码 060226_01 剥成 060226 无匹配 → 保持原样）。
    stripped = _strip_trailing_part(name)
    strip = _best(stripped) if (stripped and stripped != name) else ""
    if strip and len(_norm(strip)) > len(_norm(full)):
        return strip
    return full or strip


def _candidate_names(video_path: Path, watch_dir: str = "") -> list:
    """
    收集用于识别番号的候选名（就近优先）：
      文件名(去扩展) + 各级父目录名（截至监控根，最多上溯 6 级）。
    qB 单种子单目录场景下，种子文件夹名常等于完整番号，是文件名之外的重要佐证。
    """
    names = [video_path.stem]
    try:
        watch = Path(watch_dir).resolve() if watch_dir else None
    except Exception:
        watch = None
    parent = video_path.parent
    for _ in range(6):
        try:
            if watch and parent.resolve() == watch:
                break
        except Exception:
            pass
        if parent == parent.parent:    # 到文件系统根
            break
        if parent.name:
            names.append(parent.name)
        parent = parent.parent
    return [n for n in names if n]


def _recognize_code(video_path: Path, watch_dir: str = "") -> str:
    """
    综合「文件名 + 各级父目录名」识别番号（不依赖任何提前标记/下载器）。
    选取规则（越靠前越优先）：
      1. 在多个候选名中重复出现的番号最可信（如种子文件夹名与视频文件名一致）；
      2. 其次取归一化后更长（更具体）的番号；
      3. 再次按候选顺序（文件名优先于目录名，目录就近优先）。
    """
    found = []  # (code, 候选顺序)
    for idx, n in enumerate(_candidate_names(video_path, watch_dir)):
        c = _code_from_name(n)
        if c:
            found.append((c, idx))
    if not found:
        return ""
    # 按归一化串归并：统计出现次数，记录最靠前的来源顺序
    stats: dict[str, dict] = {}
    for code, idx in found:
        s = stats.setdefault(_norm(code), {"code": code, "count": 0, "first": idx})
        s["count"] += 1
        s["first"] = min(s["first"], idx)
    best = sorted(
        stats.values(),
        key=lambda s: (-s["count"], -len(_norm(s["code"])), s["first"]),
    )[0]
    return best["code"]


def _extract_code(filename: str) -> str:
    """（兼容旧接口）仅从单个文件名识别番号。需结合目录名时用 _recognize_code。"""
    return _code_from_name(Path(filename).stem)


# 分集/分卷标记：CD1 / DISC2 / PART1 / VOL.1，或纯 "1"/"2"/"A"/"B" 文件名
_CD_MARKER = re.compile(
    r'(?:^|[^a-z0-9])(?:cd|dvd|disc|disk|part|pt|vol)[\s._-]?\d{1,2}(?=$|[^a-z0-9])',
    re.IGNORECASE,
)


def _has_cd_marker(stem: str) -> bool:
    s = (stem or "").strip()
    if _CD_MARKER.search(s):
        return True
    t = s.lower()
    return bool(re.fullmatch(r'[a-e]', t) or re.fullmatch(r'\d{1,2}', t))


def _part_index(stem: str, code: str = "") -> Optional[int]:
    """从「该文件自身名字」解析它在分集中的位次（1 起）；无法判定返回 None。
    依据**仅来自文件名自身**，与处理/完成顺序、当前可见兄弟无关 —— 同番号多分片
    分批(staggered)完成时各分集后缀因此稳定不错位（修复 -cd 号缺失/堆叠错乱）。
    识别：CD2/DVD3/DISC1/PART2/PT1/VOL.4；整名 A/B/C 或 1/2/3；
         番号之后紧跟的「分隔符+数字/字母」残尾（FC2-PPV-xxxx_2、CODE-3、CODE_B …）。"""
    s = (stem or "").strip()
    # 1) 显式分集标记
    m = re.search(
        r'(?:^|[^a-z0-9])(?:cd|dvd|disc|disk|part|pt|vol)[\s._-]?(\d{1,2})(?=$|[^a-z0-9])',
        s, re.IGNORECASE,
    )
    if m:
        n = int(m.group(1))
        return n if n >= 1 else None
    # 2) 整名就是单个字母/数字（发布目录内常见的分卷文件 A.mp4 / 1.mp4）
    t = s.lower()
    if re.fullmatch(r'[a-e]', t):
        return ord(t) - ord('a') + 1
    if re.fullmatch(r'\d{1,2}', t):
        n = int(t)
        return n if n >= 1 else None
    # 3) 番号之后紧跟的「分隔符 + 数字/字母」残尾（区别于番号本身的尾段，故要求番号在前）
    if code:
        parts = [p for p in re.split(r'[-_.\s]+', code) if p]
        if parts:
            flex = r'[-_.\s]*'.join(re.escape(p) for p in parts)
            mm = re.search(flex + r'[-_.\s]+(\d{1,2})\s*$', s, re.IGNORECASE)
            if mm:
                n = int(mm.group(1))
                return n if n >= 1 else None
            mm = re.search(flex + r'[-_.\s]+([a-e])\s*$', s, re.IGNORECASE)
            if mm:
                return ord(mm.group(1).lower()) - ord('a') + 1
    return None


def _folder_code(video_path: Path, watch_dir: str = "") -> str:
    """仅取「目录名」识别出的番号（就近优先），不看文件名本身。无则返回 ""。"""
    for n in _candidate_names(video_path, watch_dir)[1:]:   # [0] 是文件名，跳过
        c = _code_from_name(n)
        if c:
            return c
    return ""


def _sibling_videos(video_path: Path) -> list:
    """同一直接父目录下的所有视频文件。"""
    try:
        return [p for p in video_path.parent.iterdir()
                if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
    except Exception:
        return []


def _has_primary_sibling(video_path: Path) -> bool:
    """同目录是否存在「正片」兄弟视频（带番号或分集标记 CD1/A/1）。
    用于判定本文件是否为发布目录里的广告/赠片——有正片兄弟才认定，避免误删独立小视频。"""
    return any(s != video_path and (_code_from_name(s.stem) or _has_cd_marker(s.stem))
               for s in _sibling_videos(video_path))


def _looks_primary(video_path: Path, watch_dir: str = "") -> bool:
    """
    该视频自身是否「像正片」（用于判断目录是否还有待处理正片，决定能否清理整目录）。
    判定不依赖兄弟文件，保证文件移动前后结论稳定：
      - 自身文件名能识别出番号 → 正片；
      - 带分集标记（CD1/PART2…）→ 正片（分集）；
      - 所在目录名也无番号 → 信息不足，保守当正片（不误删）；
      - 否则（目录名有番号，但自身无番号、无分集标记）→ 视为广告/附属，非正片。
    """
    if _code_from_name(video_path.stem):
        return True
    if _has_cd_marker(video_path.stem):
        return True
    if not _folder_code(video_path, watch_dir):
        return True
    return False


def _is_extra_video(video_path: Path, watch_dir: str = "") -> bool:
    """
    是否为「广告/赠片」应跳过不刮削。仅在证据充分时才丢弃，确保正片/分集不被误删：
      - 自身文件名能识别出番号        → 不是广告（按自身番号刮削）；
      - 带分集标记（CD1/PART2/纯编号）→ 不是广告（分集保留）；
      - 目录名无番号                  → 信息不足，不丢弃；
      - 仅当目录名有番号、自身无番号无分集标记，
        且同目录确实存在「正片」兄弟（带正确番号或分集标记）时 → 判为广告，跳过。
    """
    if _code_from_name(video_path.stem):
        return False
    if _has_cd_marker(video_path.stem):
        return False
    folder_code = _folder_code(video_path, watch_dir)
    if not folder_code:
        return False
    fc = _norm(folder_code)
    for s in _sibling_videos(video_path):
        if s == video_path:
            continue
        sc = _code_from_name(s.stem)
        if (sc and _norm(sc) == fc) or _has_cd_marker(s.stem):
            return True      # 存在明确正片/分集兄弟 → 本文件是广告
    return False


def _norm_stem(stem: str) -> str:
    return re.sub(r'[^a-z0-9]', '', (stem or '').lower())


def _name_similarity(a: str, b: str) -> float:
    sa, sb = _norm_stem(a), _norm_stem(b)
    return difflib.SequenceMatcher(None, sa, sb).ratio() if sa and sb else 0.0


_QUALITY_MARKER = re.compile(
    r'(?<![a-z0-9])(?:480p|720p|1080[pi]?|1440p|2160p|4k|8k|uhd|fhd|hd|sd|'
    r'hdr|sdr|10bit|8bit|x26[45]|h[._-]?26[45]|hevc|av1)(?![a-z0-9])',
    re.IGNORECASE,
)
_VIDEO_PROBE_CACHE = {}


def _quality_markers(stem: str) -> set[str]:
    """Return explicit encode/quality labels; these describe variants, never parts."""
    return {m.group(0).lower().replace("_", "").replace("-", "").replace(".", "")
            for m in _QUALITY_MARKER.finditer(stem or "")}


def _probe_video(video_path: Path) -> dict:
    """Read duration and video geometry with ffprobe when available."""
    try:
        st = video_path.stat()
        key = (str(video_path), st.st_size, st.st_mtime_ns)
    except Exception:
        return {}
    cached = _VIDEO_PROBE_CACHE.get(key)
    if cached is not None:
        return cached
    probe = shutil.which("ffprobe")
    if not probe:
        _VIDEO_PROBE_CACHE[key] = {}
        return {}
    try:
        cp = subprocess.run(
            [probe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "format=duration:stream=width,height,codec_name",
             "-of", "json", str(video_path)],
            capture_output=True, text=True, timeout=12, check=False,
        )
        raw = json.loads(cp.stdout or "{}") if cp.returncode == 0 else {}
        stream = next(iter(raw.get("streams") or []), {})
        result = {
            "duration": float((raw.get("format") or {}).get("duration") or 0),
            "width": int(stream.get("width") or 0),
            "height": int(stream.get("height") or 0),
            "codec": (stream.get("codec_name") or "").lower(),
        }
    except Exception:
        result = {}
    _VIDEO_PROBE_CACHE[key] = result
    return result


def _is_quality_variant(a: Path, b: Path) -> bool:
    """Whether two same-code files are alternate encodes, not sequential parts."""
    if a == b:
        return False
    a_part, b_part = _part_index(a.stem), _part_index(b.stem)
    if a_part is not None and b_part is not None and a_part != b_part:
        return False
    am, bm = _quality_markers(a.stem), _quality_markers(b.stem)
    af, bf = _probe_video(a), _probe_video(b)
    ad, bd = af.get("duration", 0), bf.get("duration", 0)
    close_duration = bool(ad and bd and abs(ad - bd) <= max(3.0, max(ad, bd) * 0.005))
    geometry_differs = bool(af.get("width") and bf.get("width") and
                            (af["width"], af["height"]) != (bf["width"], bf["height"]))
    encode_differs = bool(af.get("codec") and bf.get("codec") and
                          af["codec"] != bf["codec"])
    explicit_quality_differs = bool(am and bm and am != bm)
    return ((geometry_differs or encode_differs or explicit_quality_differs) and close_duration
            or (not ad and not bd and explicit_quality_differs))


def _variant_preference(video_path: Path) -> tuple:
    """Deterministically prefer the highest-resolution/largest alternate encode."""
    facts = _probe_video(video_path)
    try:
        size = video_path.stat().st_size
    except Exception:
        size = 0
    return (facts.get("width", 0) * facts.get("height", 0), size, video_path.name.lower())


def _seq_order_key(stem: str, code: str = "") -> Optional[int]:
    idx = _part_index(stem, code)
    if idx is not None:
        return idx
    s = (stem or '').strip().lower()
    m = re.search(r'(\d{1,2})\s*$', s)
    if m:
        return int(m.group(1))
    m = re.search(r'(?:^|[^a-z])([a-e])\s*$', s)
    if m:
        return ord(m.group(1)) - ord('a') + 1
    m = re.match(r'\s*(\d{1,2})(?:[^0-9]|$)', s)
    return int(m.group(1)) if m else None


def classify_videos(videos: list, watch_dir: str = "",
                    min_bytes: int = 100 * 1024 * 1024,
                    keep_bytes: int = 300 * 1024 * 1024,
                    sim_threshold: float = 0.6):
    """保守地划分正片/分段与广告；任何不确定情况优先保留。"""
    vids = [p for p in (videos or []) if p and p.suffix.lower() in VIDEO_EXTS]
    if len(vids) <= 1:
        return list(vids), set()
    sized = []
    for p in vids:
        try:
            sized.append((p, p.stat().st_size))
        except Exception:
            sized.append((p, keep_bytes))
    largest = max(sized, key=lambda item: item[1])[0]
    keep, drop = [], set()
    for p, size in sized:
        if (p == largest or size >= keep_bytes or _code_from_name(p.stem)
                or _has_cd_marker(p.stem)):
            keep.append(p)
        elif size < min_bytes:
            drop.add(p)
        elif _name_similarity(p.stem, largest.stem) >= sim_threshold:
            keep.append(p)
        else:
            drop.add(p)
    if not keep:
        return list(vids), set()
    folder_code = next((_folder_code(p, watch_dir) for p in keep
                        if _folder_code(p, watch_dir)), "")
    sizes = dict(sized)
    ordered = sorted(keep, key=lambda p: (
        _seq_order_key(p.stem, folder_code) is None,
        _seq_order_key(p.stem, folder_code) or 999,
        -sizes.get(p, 0), p.name.lower()))
    return ordered, drop


def _same_code_main_videos(video_path: Path, code: str, watch_dir: str = "") -> list:
    """同一直接父目录下、与 code 同番号的全部「正片」视频（含自身、排除广告/赠片），
    按文件名排序返回。用于多分段（CD1/CD2、A/B/C、1/2/3…）归档时确定各段顺序。
    分段文件（纯编号/字母/CDx）经 _recognize_code 会从父目录认出同一番号，故能聚到一起。"""
    norm = _norm(code)
    mains = []
    for s in _sibling_videos(video_path):
        if _is_extra_video(s, watch_dir):
            continue
        if _norm(_recognize_code(s, watch_dir)) == norm:
            mains.append(s)
    representatives = []
    for candidate in sorted(mains, key=lambda p: p.name.lower()):
        group_pos = next((i for i, current in enumerate(representatives)
                          if _is_quality_variant(candidate, current)), None)
        if group_pos is None:
            representatives.append(candidate)
        elif _variant_preference(candidate) > _variant_preference(representatives[group_pos]):
            representatives[group_pos] = candidate
    representatives.sort(key=lambda p: p.name.lower())
    return representatives


def _is_nonpreferred_variant(video_path: Path, code: str, watch_dir: str = "") -> bool:
    """Keep alternate encodes at source while only the preferred one is archived."""
    representatives = _same_code_main_videos(video_path, code, watch_dir)
    if video_path in representatives:
        return False
    return any(_is_quality_variant(video_path, current) for current in representatives)


def _part_suffix(video_path: Path, code: str, watch_dir: str = "",
                 archived_parts: int = 0) -> str:
    """同番号有多个正片（分段）时，返回该视频的分段后缀「-cd{N}」
    （Emby/Kodi 多文件堆叠为同一影片）；单片返回 ""。
    N 优先取「文件名自带的分集序号」(_part_index：CD2/_3/-B/纯数字…)——与处理/完成
    顺序无关，分批(staggered)完成也不错位；无自带序号时回退到同番号可见正片中的位次。
    archived_parts：目标归档目录内已存在的同番号分集数（分批先到的分集已落地时据此判定多分段）。"""
    own = _part_index(video_path.stem, code)
    mains = _same_code_main_videos(video_path, code, watch_dir)   # 含自身（仍在源目录）
    known_parts = {_part_index(p.stem, code) for p in mains}
    known_parts.discard(None)
    # 文件数量、大小和名称相似度都不是分段证据。只有至少两个不同的明确
    # 分段编号，或当前文件自己明确为第 2 段以后，才生成 -cdN。
    multipart = (len(known_parts) >= 2 or (own is not None and own >= 2)
                 or (own is not None and archived_parts > 0))
    if not multipart:
        return ""
    if own is not None:
        return f"-cd{own}"
    # 当前文件没有自己的序号时不按扫描/文件名顺序猜号。
    return ""


async def _resolve_code(video_path: Path, config: dict) -> str:
    """
    番号识别：直接分析「文件名 + 各级父目录名」（不做提前标记，不依赖下载器 API）。
    适用于 qB 推送下载、迅雷下载、手动复制到监控目录等所有场景。
    """
    code = _recognize_code(video_path, config.get("scrape_watch_dir", ""))
    if code:
        _log(f"识别番号：{video_path.name} → {code}")
    else:
        _log(f"未能识别番号（文件名/目录名均无匹配）：{video_path.name}")
    return code


# 日文（含假名/汉字）检测：用于判断是否需要翻译
_JP_RE = re.compile(r'[぀-ヿ㐀-鿿]')


def _has_jp(text: str) -> bool:
    return bool(_JP_RE.search(text or ""))


def _safe_name(name: str) -> str:
    """清洗成可作文件/目录名的安全字符串（去掉非法字符）。"""
    return re.sub(r'[\\/:*?"<>|]', "", (name or "").strip()).strip(" .") or "untitled"


def _strip_code_prefix(title: str, code: str) -> str:
    """从标题里去掉开头的番号，留下真正的（通常是日文）片名部分。"""
    if not title:
        return ""
    t = title.strip()
    if code:
        # 去掉开头的 番号（带或不带分隔符），如 "MOON-057 ..." / "MOON057 ..."
        pat = re.compile(r'^\s*' + re.escape(code).replace(r'\-', r'[-_ ]?') + r'[\s:：\-_]*',
                         re.IGNORECASE)
        t = pat.sub("", t)
    return t.strip()


def _compose_title(code: str, name_zh: str) -> str:
    """NFO <title>：番号不翻译，作为前缀；后接翻译后的中文片名（若有）。"""
    name_zh = (name_zh or "").strip()
    if code and name_zh and name_zh.upper() != code.upper():
        return f"{code} {name_zh}"
    return code or name_zh


def _parse_runtime(duration: str) -> str:
    if not duration:
        return ""
    nums = re.findall(r'\d+', duration)
    return nums[0] if nums else ""


def _parse_rating(score: str) -> str:
    if not score:
        return ""
    nums = re.findall(r'\d+\.?\d*', score)
    return nums[0] if nums else ""


def _build_nfo(movie: dict, title_zh: str, plot_zh: str,
               actor_thumb_in_nfo: bool = True) -> str:
    """生成 Emby/Kodi 标准 movie.nfo（标题/简介为翻译后的中文）"""
    root = ET.Element("movie")

    def add(tag: str, text: str):
        if text:
            el = ET.SubElement(root, tag)
            el.text = text

    add("title", title_zh or movie.get("title", ""))
    add("originaltitle", movie.get("title", ""))
    add("sorttitle", movie.get("code", ""))
    add("plot", plot_zh or movie.get("description", ""))
    add("outline", plot_zh or movie.get("description", ""))

    rating = _parse_rating(movie.get("score", ""))
    if rating:
        ratings = ET.SubElement(root, "ratings")
        r = ET.SubElement(ratings, "rating", name="javdb", max="10", default="true")
        ET.SubElement(r, "value").text = rating
        ET.SubElement(r, "votes").text = "0"
        add("rating", rating)

    release_date = movie.get("release_date", "")
    if release_date:
        year = release_date[:4] if len(release_date) >= 4 else ""
        add("year", year)
        add("premiered", release_date)
        add("releasedate", release_date)

    add("runtime", _parse_runtime(movie.get("duration", "")))
    add("studio", movie.get("studio", ""))
    add("label", movie.get("label", ""))
    add("director", movie.get("director", ""))

    code = movie.get("code", "")
    if code:
        uid = ET.SubElement(root, "uniqueid", type="num", default="true")
        uid.text = code

    series = movie.get("series", "")
    if series:
        s = ET.SubElement(root, "set")
        ET.SubElement(s, "name").text = series
        ET.SubElement(s, "overview").text = ""

    for tag in (movie.get("tags") or [])[:12]:
        add("genre", tag)
        add("tag", tag)

    for actor in (movie.get("actors") or []):
        name = actor.get("name", "")
        if not name:
            continue
        a_el = ET.SubElement(root, "actor")
        ET.SubElement(a_el, "name").text = name
        avatar = actor.get("avatar", "")
        if avatar and actor_thumb_in_nfo:
            ET.SubElement(a_el, "thumb").text = avatar
        ET.SubElement(a_el, "role").text = ""
        ET.SubElement(a_el, "order").text = "0"

    raw = ET.tostring(root, encoding="unicode")
    doc = minidom.parseString(raw.encode("utf-8"))
    pretty = doc.toprettyxml(indent="  ")
    lines = [l for l in pretty.splitlines() if l.strip()]
    lines[0] = '<?xml version="1.0" encoding="utf-8" standalone="yes"?>'
    return "\n".join(lines)


def _cover_referer(cover_url: str) -> str:
    if "javdb" in cover_url:
        return "https://javdb.com/"
    if "dmm" in cover_url or "fanza" in cover_url:
        return "https://www.dmm.co.jp/"
    return "https://www.javbus.com/"


async def _download_image(url: str, proxy: Optional[str], referer: str) -> Optional[bytes]:
    if not url or not url.startswith("http"):
        return None
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": referer,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    try:
        async with httpx.AsyncClient(proxy=proxy or None, timeout=30, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.content
    except Exception as e:
        print(f"[Library] 图片下载失败 {url}: {e}")
    return None


def _merge_actor_avatars(movie: dict, source_actors: list) -> int:
    """Fill missing avatar URLs by actor name without replacing existing metadata."""
    def key(value: str) -> str:
        return re.sub(r"[\s・·._-]+", "", (value or "")).casefold()

    avatars = {
        key(actor.get("name", "")): actor.get("avatar", "").strip()
        for actor in (source_actors or [])
        if actor.get("name") and (actor.get("avatar") or "").startswith("http")
    }
    filled = 0
    for actor in movie.get("actors") or []:
        if (actor.get("avatar") or "").startswith("http"):
            continue
        avatar = avatars.get(key(actor.get("name", "")))
        if avatar:
            actor["avatar"] = avatar
            filled += 1
    return filled


async def _fill_actor_avatars(movie: dict, code: str, config: dict,
                              proxy: Optional[str]) -> int:
    """Use a JavBus detail lookup when metadata has actor names but no portraits."""
    if not config.get("scrape_actor_images_enabled", False):
        return 0
    actors = movie.get("actors") or []
    if not actors or all((a.get("avatar") or "").startswith("http") for a in actors):
        return 0
    try:
        from scrapers import enrich
        candidates = await search(query=code, mode=SEARCH_MODE_CODE, proxy=proxy,
                                  sources=["javbus"], max_results=5)
        details = await enrich(candidates[:2], proxy=proxy) if candidates else []
        filled = 0
        for detail in details:
            if detail:
                filled += _merge_actor_avatars(movie, detail.get("actors") or [])
        if filled:
            _log(f"演员头像补查完成：{code}（从 JavBus 补全 {filled} 个头像地址）")
        else:
            _log(f"演员头像补查未找到可用头像：{code}")
        return filled
    except Exception as e:
        _log(f"演员头像补查失败（继续保存现有刮削结果）：{code}: {e}")
        return 0


async def _save_actor_images(movie: dict, config: dict, proxy: Optional[str],
                             media_folder: Optional[Path] = None) -> int:
    """先缓存到影片旁可见的 actors 目录，再可选同步到 Emby metadata/people。"""
    if not config.get("scrape_actor_images_enabled", False):
        return 0
    root_value = (config.get("scrape_actor_images_dir") or "").strip()
    root = Path(root_value) if root_value else None
    local_root = media_folder / "actors" if media_folder else None
    saved = 0
    available = 0
    for actor in movie.get("actors") or []:
        name = (actor.get("name") or "").strip()
        url = (actor.get("avatar") or "").strip()
        if not name or not url.startswith("http"):
            continue
        available += 1
        safe_name = _safe_name(name)
        local_img = local_root / f"{safe_name}.jpg" if local_root else None
        img = local_img.read_bytes() if local_img and local_img.exists() else None
        if not img:
            img = await _download_image(url, proxy, _cover_referer(url))
        if not img:
            continue
        try:
            if local_img and not local_img.exists():
                local_img.parent.mkdir(parents=True, exist_ok=True)
                local_img.write_bytes(img)
            if root:
                person_dir = root / safe_name
                person_dir.mkdir(parents=True, exist_ok=True)
                folder_img = person_dir / "folder.jpg"
                portrait_img = person_dir / "portrait.jpg"
                if not folder_img.exists():
                    folder_img.write_bytes(img)
                if not portrait_img.exists():
                    portrait_img.write_bytes(img)
            saved += 1
        except Exception as e:
            _log(f"演员头像保存失败 {name}: {e}")
    if saved:
        where = "影片 actors 缓存"
        if root:
            where += f"，并同步到 Emby people：{root}"
        else:
            where += "（未配置 Emby people 路径，仍会随影片归档）"
        _log(f"已缓存 {saved} 位演员头像到{where}")
    elif not available:
        _log("影片包含演员信息，但当前刮削源未提供可下载的头像地址")
    return saved


def _archive_folder_name(code: str, title_original: str, title_translated: str,
                         actors: list, config: dict) -> str:
    """构造稳定且跨平台安全的影片文件夹名。"""
    safe_code = _safe_name(code) if code else "unknown"
    mode = config.get("scrape_folder_naming", "code")
    suffix = ""
    if mode == "code_title":
        use_translated = config.get("scrape_folder_title_translate", False)
        suffix = title_translated if use_translated else title_original
    elif mode == "code_title_actor":
        use_translated = config.get("scrape_folder_title_translate", False)
        title = title_translated if use_translated else title_original
        names = [(a.get("name") or "").strip() for a in (actors or [])]
        names = list(dict.fromkeys(n for n in names if n))
        if config.get("scrape_folder_actor_mode", "first") != "all":
            names = names[:1]
        suffix = " ".join(part for part in [title, " ".join(names)] if (part or "").strip())
    elif mode == "code_actor":
        names = [(a.get("name") or "").strip() for a in (actors or [])]
        names = list(dict.fromkeys(n for n in names if n))
        if config.get("scrape_folder_actor_mode", "first") != "all":
            names = names[:1]
        suffix = " ".join(names)
    suffix = _safe_name(suffix.strip()) if (suffix or "").strip() else ""
    # 留出年月及文件名长度；缺元数据时稳定回退纯番号。
    return (f"{safe_code} {suffix}" if suffix else safe_code)[:150].rstrip(" .")


async def _fetch_cover(url: str, proxy: Optional[str]) -> Optional[bytes]:
    """获取封面字节。

    首选复用首页/详情那套图片缓存（内存+磁盘，CONFIG_DIR/imgcache）：首页浏览时封面已抓并落盘，
    刮削直接命中即零上游请求，不再按地址重下。缓存未命中时走与 /api/img 同一套防盗链抓取
    （多 Referer + 代理/直连兜底），修复 FC2（MissAV/fourhoi 域名）等封面因 Referer 错误下载失败。
    仅在拿不到 main.fetch_image_cached 时才退回旧的单 Referer 直连下载。"""
    try:
        import main
        got = await main.fetch_image_cached(url)
        return got[0] if (got and got[0]) else None
    except Exception as e:
        _log(f"复用图片缓存失败，回退直连下载：{e}")
        return await _download_image(url, proxy, _cover_referer(url))


def _artwork_urls(movie: dict) -> tuple[str, str]:
    """Select independent source images for poster and fanart; never synthesize one."""
    poster = (movie.get("cover") or "").strip()
    fanart = next(((url or "").strip() for url in (movie.get("samples") or [])
                   if (url or "").strip() and (url or "").strip() != poster), "")
    return poster, fanart


def _image_dimensions(data: bytes) -> tuple[int, int]:
    """Read common web image dimensions without adding a Pillow dependency."""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:2] == b"\xff\xd8":
        offset = 2
        while offset + 9 < len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            marker = data[offset + 1]
            offset += 2
            if marker in {0xD8, 0xD9}:
                continue
            if offset + 2 > len(data):
                break
            size = int.from_bytes(data[offset:offset + 2], "big")
            if size < 2 or offset + size > len(data):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and size >= 7:
                return (int.from_bytes(data[offset + 5:offset + 7], "big"),
                        int.from_bytes(data[offset + 3:offset + 5], "big"))
            offset += size
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        if data[12:16] == b"VP8X":
            return (1 + int.from_bytes(data[24:27], "little"),
                    1 + int.from_bytes(data[27:30], "little"))
    return 0, 0


def _is_fanart_image(data: bytes) -> bool:
    """Accept backdrop-shaped images and reject portrait/DVD-cover artwork."""
    width, height = _image_dimensions(data)
    return width >= 640 and height >= 360 and width / max(height, 1) >= 1.35


def _is_confirmed_jacket_cover(movie: dict, cover_url: str) -> bool:
    """Require both trusted cover provenance and a source-specific cover URL."""
    url = (cover_url or "").strip().lower().split("?", 1)[0]
    if not url or url != (movie.get("cover") or "").strip().lower().split("?", 1)[0]:
        return False
    if any(url == (sample or "").strip().lower().split("?", 1)[0]
           for sample in (movie.get("samples") or [])):
        return False
    source = (movie.get("poster_source") or movie.get("source") or "").strip().lower()
    if source == "javbus":
        javbus_cover = "/pics/cover/" in url and bool(
            re.search(r"_b\.(?:jpe?g|png|webp)$", url))
        dmm_package = "pics.dmm.co.jp/" in url and bool(
            re.search(r"pl\.(?:jpe?g|png|webp)$", url))
        return javbus_cover or dmm_package
    if source == "javdb":
        return "/covers/" in url and bool(
            re.search(r"\.(?:jpe?g|png|webp)$", url))
    return False


def _poster_bytes(data: bytes, confirmed_jacket: bool = False) -> tuple[bytes, bool]:
    """Crop the right/front panel of a confirmed full horizontal DVD jacket.

    Japanese DVD jacket scans normally place the front panel on the right. A
    conservative aspect-ratio gate prevents ordinary 16:9 stills from being
    mistaken for a jacket. Already-portrait source artwork is kept unchanged.
    """
    if not confirmed_jacket:
        return data, False
    width, height = _image_dimensions(data)
    ratio = width / max(height, 1)
    if width < 700 or height < 450 or not 1.30 <= ratio <= 1.65:
        return data, False
    try:
        with Image.open(BytesIO(data)) as source:
            source.load()
            # Start just to the left of the centre seam.  A full jacket's
            # front panel is the right half; retaining another 3% of the full
            # width avoids shaving off artwork/text printed across the fold.
            crop_width = round(source.width * 0.53)
            if crop_width <= 0 or crop_width > source.width:
                return data, False
            front = source.crop((source.width - crop_width, 0,
                                 source.width, source.height)).convert("RGB")
            output = BytesIO()
            front.save(output, format="JPEG", quality=94, optimize=True)
            return output.getvalue(), True
    except (OSError, ValueError):
        return data, False


async def _fetch_fanart(movie: dict, proxy: Optional[str]) -> tuple[Optional[bytes], str]:
    """Download the first genuinely wide, independent sample image."""
    poster = (movie.get("cover") or "").strip()
    urls = list(dict.fromkeys((u or "").strip() for u in (movie.get("samples") or [])))
    for url in urls[:8]:
        if not url or url == poster:
            continue
        data = await _fetch_cover(url, proxy)
        if data and _is_fanart_image(data):
            return data, url
        if data:
            width, height = _image_dimensions(data)
            _log(f"跳过非横向背景候选：{url[:60]}（{width}x{height}）")
    return None, ""


def _merge_artwork(target: dict, detail: dict, source: str = "") -> bool:
    """Fill only missing source artwork and report whether anything changed."""
    changed = False
    if not target.get("cover") and detail.get("cover"):
        target["cover"] = detail["cover"]
        target["poster_source"] = source or detail.get("source", "")
        changed = True
    if not target.get("samples") and detail.get("samples"):
        target["samples"] = list(detail["samples"])
        target["fanart_source"] = source or detail.get("source", "")
        changed = True
    return changed


def _enabled_artwork_sources(code: str, config: dict) -> list[str]:
    enabled = [s for s in (config.get("sources") or ["javbus", "javdb"])
               if s in {"javbus", "javdb", "avsox", "avmoo", "fc2", "jav321", "dmm"}]
    if not _norm(code).startswith("fc2ppv"):
        enabled = [s for s in enabled if s != "fc2"]
    return enabled


async def _backfill_artwork(movie: dict, code: str, config: dict,
                            proxy: Optional[str]) -> dict:
    """Lazily fill missing poster/fanart by trying enabled sources sequentially.

    Existing per-source detail URLs and the shared detail cache are used first.
    A small code search is performed only when pushed metadata has no alternate
    source URLs. Each source is tried sequentially and work stops as soon as
    both artwork types are available.
    """
    if movie.get("cover") and movie.get("samples"):
        return movie
    configured_limit = max(0, min(int(config.get("scrape_artwork_fallback_limit", 2)), 6))
    if configured_limit <= 0:
        return movie
    from scrapers import enrich, search_source_status

    enabled = _enabled_artwork_sources(code, config)
    def source_key(value: str) -> str:
        key = (value or "").strip().lower()
        return "dmm" if key in {"dmm", "dmm/fanza", "fanza"} else key

    source_urls = {source_key(src): url for src, url in
                   dict(movie.get("source_urls") or {}).items() if source_key(src)}
    if movie.get("source") and movie.get("url"):
        source_urls.setdefault(source_key(movie["source"]), movie["url"])

    # Prefer sources known to expose independent samples, then the current
    # source. Only enabled sources participate, so user source choices remain
    # authoritative.
    priority = {"dmm": 0, "jav321": 1, "javdb": 2, "avsox": 3,
                "avmoo": 4, "fc2": 5, "javbus": 6}
    # 缺图时真正按来源逐个尝试。已有详情 URL 直接复用；没有 URL 的来源单独做一次
    # 小范围番号搜索，避免一次聚合搜索中某源超时后候选 URL 丢失。任一来源拿到
    # 独立 fanart 即停止，全部已启用来源都无结果才允许后续先归档。
    pending_only = {source_key(src) for src in (movie.get("_artwork_pending_sources") or [])}
    candidates = sorted(
        (src for src in enabled if not pending_only or src in pending_only),
        key=lambda src: priority.get(src, 99))
    attempts = 0
    conclusive = 0
    transient = []
    for source in candidates:
        if ((movie.get("cover") and movie.get("samples"))
                or conclusive >= configured_limit):
            break
        attempts += 1
        url = source_urls.get(source, "")
        try:
            detail = None
            source_status = "ok"
            if not url:
                found, source_status = await search_source_status(
                    code, SEARCH_MODE_CODE, source, proxy=proxy, max_results=3)
                if source_status in {"timeout", "error"}:
                    transient.append(source)
                    _log(f"图片备用来源暂时失败：{code}（{source}，不占补查额度）")
                    continue
                norm = _norm(code)
                detail = next((item for item in found
                               if _norm(item.get("code", "")) == norm), None)
                if detail:
                    url = detail.get("url", "")
                    if url:
                        source_urls[source] = url
                    _merge_artwork(movie, detail, source)
            if url and not movie.get("samples"):
                rows = await enrich([{"url": url, "source": source}], proxy=proxy,
                                    concurrency=1, per_timeout=12.0, with_status=True)
                entry = rows[0] if rows else (None, "error")
                if isinstance(entry, tuple) and len(entry) == 2:
                    enriched, enrich_status = entry
                else:  # 兼容测试替身及旧的自定义 enrich 包装
                    enriched, enrich_status = entry, ("ok" if entry else "empty")
                if enrich_status in {"timeout", "error"}:
                    transient.append(source)
                    _log(f"图片备用来源暂时失败：{code}（{source}，不占补查额度）")
                    continue
                if enriched:
                    detail = enriched
                    _merge_artwork(movie, enriched, source)
            # 只有确实取得该番号的来源条目/详情才占补查额度。超时、异常、空结果
            # 都保持 detail=None，继续尝试后面的 JAV321/DMM 等来源。
            if detail:
                conclusive += 1
            if detail and movie.get("samples"):
                _log(f"图片备用来源补全：{code}（{source}，有效响应 {conclusive}/{configured_limit}）")
            elif not movie.get("samples"):
                counted = f"有效响应 {conclusive}/{configured_limit}" if detail else "不占补查额度"
                _log(f"图片备用来源未取得独立 fanart：{code}（{source}，{counted}，继续下一来源）")
        except Exception as e:
            transient.append(source)
            _log(f"图片备用来源失败：{code}（{source}）: {e}")
    movie["_artwork_pending_sources"] = list(dict.fromkeys(transient))
    if not movie.get("samples"):
        if conclusive >= configured_limit and attempts < len(candidates):
            _log(f"已达到 {configured_limit} 个有效补查来源上限，暂未找到独立 fanart：{code}")
        else:
            _log(f"已尝试全部已启用图片来源，暂未找到独立 fanart：{code}")
    return movie


def _load_artwork_pending() -> None:
    global _artwork_pending_loaded
    if _artwork_pending_loaded:
        return
    _artwork_pending_loaded = True
    try:
        data = json.loads(_ARTWORK_PENDING_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _artwork_pending.update({str(k): v for k, v in data.items() if isinstance(v, dict)})
    except FileNotFoundError:
        pass
    except Exception as e:
        _log(f"载入归档图片补全队列失败（忽略）：{e}")


def _save_artwork_pending() -> None:
    try:
        _ARTWORK_PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _ARTWORK_PENDING_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_artwork_pending, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_ARTWORK_PENDING_FILE)
    except Exception as e:
        _log(f"保存归档图片补全队列失败（忽略）：{e}")


def _load_artwork_terminal() -> None:
    global _artwork_terminal_loaded
    if _artwork_terminal_loaded:
        return
    _artwork_terminal_loaded = True
    try:
        data = json.loads(_ARTWORK_TERMINAL_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _artwork_terminal.update({str(k): sorted(map(str, v))
                                      for k, v in data.items() if isinstance(v, list)})
    except FileNotFoundError:
        pass
    except Exception as e:
        _log(f"载入归档图片终止记录失败（忽略）：{e}")


def _artwork_source_signature(code: str, config: dict) -> list[str]:
    return sorted(_enabled_artwork_sources(code, config))


def _save_artwork_terminal() -> None:
    try:
        _ARTWORK_TERMINAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _ARTWORK_TERMINAL_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_artwork_terminal, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_ARTWORK_TERMINAL_FILE)
    except Exception as e:
        _log(f"保存归档图片终止记录失败（忽略）：{e}")


def _mark_artwork_terminal(target_dir: Path, code: str, config: dict) -> None:
    _load_artwork_terminal()
    _artwork_terminal[str(target_dir)] = _artwork_source_signature(code, config)
    while len(_artwork_terminal) > _PROCESSED_MAX:
        _artwork_terminal.pop(next(iter(_artwork_terminal)), None)
    _save_artwork_terminal()


def _artwork_context(movie: dict) -> dict:
    """仅保留补图所需、可安全持久化的来源上下文。"""
    return {
        "code": movie.get("code", ""), "cover": movie.get("cover", ""),
        "samples": list(movie.get("samples") or []),
        "source": movie.get("source", ""), "url": movie.get("url", ""),
        "poster_source": movie.get("poster_source", ""),
        "source_urls": dict(movie.get("source_urls") or {}),
        "_artwork_pending_sources": list(movie.get("_artwork_pending_sources") or []),
    }


def _queue_artwork_backfill(target_dir: Path, code: str, movie: Optional[dict],
                            config: dict) -> bool:
    """把准确的最终归档目录加入低频补图队列；已有任务不重置退避时间。"""
    if not code or int(config.get("scrape_artwork_fallback_limit", 2)) <= 0:
        return False
    target_dir = Path(target_dir)
    poster = target_dir / f"{code}-poster.jpg"
    fanart = target_dir / f"{code}-fanart.jpg"
    if poster.exists() and fanart.exists():
        return False
    _load_artwork_pending()
    _load_artwork_terminal()
    key = str(target_dir)
    signature = _artwork_source_signature(code, config)
    if _artwork_terminal.get(key) == signature:
        return False
    if key in _artwork_terminal:  # 来源集合改变，允许只重新检查一次
        _artwork_terminal.pop(key, None)
        _save_artwork_terminal()
    context = _artwork_context(movie or {})
    if movie is None:
        # 旧归档尚未做过这套状态化检查，只允许进入一次有限检查流程。
        context["_artwork_pending_sources"] = _enabled_artwork_sources(code, config)
    elif not context.get("_artwork_pending_sources"):
        _mark_artwork_terminal(target_dir, code, config)
        _log(f"归档图片补全终止：{code}（已启用来源均已确认无图，不进入持久化队列）")
        return False
    current = _artwork_pending.get(key)
    if current:
        old = current.setdefault("movie", {})
        _merge_artwork(old, context, context.get("source", ""))
        if not old.get("source_urls") and context.get("source_urls"):
            old["source_urls"] = context["source_urls"]
        if "_artwork_pending_sources" in context:
            old["_artwork_pending_sources"] = context["_artwork_pending_sources"]
            if not context["_artwork_pending_sources"]:
                _artwork_pending.pop(key, None)
                _save_artwork_pending()
                _mark_artwork_terminal(target_dir, code, config)
        return False
    _artwork_pending[key] = {
        "target_dir": key, "code": code, "movie": context,
        "attempts": 0, "next_attempt": time.time() + 15 * 60,
    }
    _save_artwork_pending()
    _log(f"归档图片待补全：{code}（最终目录 {target_dir}，15 分钟后低频重试）")
    return True


def _write_artwork_if_missing(path: Path, data: bytes) -> bool:
    """在最终归档目录原子补图，绝不覆盖已有图片。"""
    if path.exists() or not data:
        return False
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp.write_bytes(data)
        _commit_temp_without_overwrite(tmp, path)
        return True
    except FileExistsError:
        return False
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


async def _run_pending_artwork(config: dict) -> int:
    """每轮最多处理一个到期任务；只重试暂时失败来源，最多三轮。"""
    _load_artwork_pending()
    if not _artwork_pending or int(config.get("scrape_artwork_fallback_limit", 2)) <= 0:
        return 0
    now = time.time()
    due = sorted(
        ((key, task) for key, task in _artwork_pending.items()
         if float(task.get("next_attempt", 0)) <= now),
        key=lambda item: float(item[1].get("next_attempt", 0)),
    )
    if not due:
        return 0
    key, task = due[0]
    target = Path(task.get("target_dir") or key)
    code = (task.get("code") or "").strip()
    output = (config.get("scrape_output_dir") or "").strip()
    try:
        resolved = target.resolve()
        root = Path(output).resolve()
        if not output or (resolved != root and root not in resolved.parents):
            raise ValueError("目标目录不在当前归档根目录内")
    except Exception as e:
        _log(f"取消无效归档图片补全任务：{code or key}（{e}）")
        _artwork_pending.pop(key, None)
        _save_artwork_pending()
        return 0
    poster_path = target / f"{code}-poster.jpg"
    fanart_path = target / f"{code}-fanart.jpg"
    if poster_path.exists() and fanart_path.exists():
        _artwork_pending.pop(key, None)
        _save_artwork_pending()
        return 0

    movie = dict(task.get("movie") or {})
    movie["code"] = code
    try:
        await _backfill_artwork(movie, code, config, config.get("proxy") or None)
        poster_url, fanart_url = _artwork_urls(movie)
        saved = []
        download_failed = False
        if not poster_path.exists() and poster_url:
            data = await _fetch_cover(poster_url, config.get("proxy") or None)
            if data:
                data, cropped = _poster_bytes(
                    data, _is_confirmed_jacket_cover(movie, poster_url))
                if cropped:
                    _log(f"完整横向封套已裁取右侧正面：{code}-poster.jpg")
            if data and _write_artwork_if_missing(poster_path, data):
                saved.append(poster_path.name)
            elif not poster_path.exists():
                download_failed = True
        if not fanart_path.exists() and fanart_url:
            data, fanart_url = await _fetch_fanart(movie, config.get("proxy") or None)
            if data and _write_artwork_if_missing(fanart_path, data):
                saved.append(fanart_path.name)
            elif not fanart_path.exists():
                download_failed = True
        if poster_path.exists() and fanart_path.exists():
            _artwork_pending.pop(key, None)
            _save_artwork_pending()
            _log(f"归档图片补全完成：{code} → {target}（{', '.join(saved) or '图片已存在'}）")
            if saved and config.get("emby_actor_sync_enabled", False):
                try:
                    import actor_scraper
                    notified = await actor_scraper.notify_emby_folder(target, config)
                    _log(f"Emby 已定向刷新补图目录：{code}（{notified.get('message', '')}）")
                except Exception as e:
                    _log(f"Emby 补图目录通知失败（不影响图片落盘）：{code}（{e}）")
            return 1
        if download_failed:
            movie.setdefault("_artwork_pending_sources", []).append("_download")
        task["movie"] = _artwork_context(movie)
    except Exception as e:
        _log(f"归档图片补全失败：{code}（{e}）")

    pending_sources = list(movie.get("_artwork_pending_sources") or [])
    if not pending_sources:
        _artwork_pending.pop(key, None)
        _save_artwork_pending()
        _mark_artwork_terminal(target, code, config)
        _log(f"归档图片补全任务结束：{code}（现有来源已全部确认无 fanart）")
        return 0

    task["attempts"] = int(task.get("attempts", 0)) + 1
    if task["attempts"] >= 3:
        _artwork_pending.pop(key, None)
        _save_artwork_pending()
        _mark_artwork_terminal(target, code, config)
        _log(f"归档图片补全任务结束：{code}（暂时失败来源已重试 3 次，不再查询）")
        return 0
    delays = (15 * 60, 60 * 60, 6 * 60 * 60)
    delay = delays[min(task["attempts"] - 1, len(delays) - 1)]
    task["next_attempt"] = time.time() + delay
    _artwork_pending[key] = task
    _save_artwork_pending()
    _log(f"归档图片仍缺失：{code}（下次约 {delay // 60} 分钟后重试）")
    return 0


def _get_file_status(video_path: Path, code: str = "") -> dict:
    """检查视频旁是否已有以 番号 命名的 NFO/封面。"""
    folder = video_path.parent
    stem = code or video_path.stem
    nfo_path = folder / f"{stem}.nfo"
    poster_path = folder / f"{stem}-poster.jpg"
    fanart_path = folder / f"{stem}-fanart.jpg"
    return {
        "has_nfo": nfo_path.exists(),
        "has_poster": poster_path.exists(),
        "has_fanart": fanart_path.exists(),
        "has_cover": poster_path.exists() or fanart_path.exists(),
    }


def _read_existing_nfo_metadata(video_path: Path, code: str) -> dict:
    """已有刮削结果也可参与自定义文件夹命名，避免跳过刮削后退回纯番号。"""
    path = video_path.parent / f"{code}.nfo"
    try:
        root = ET.parse(path).getroot()
        original = (root.findtext("originaltitle") or "").strip()
        title = _strip_code_prefix((root.findtext("title") or "").strip(), code)
        actors = [{"name": (node.findtext("name") or "").strip(),
                   "avatar": (node.findtext("thumb") or "").strip()}
                  for node in root.findall("actor") if (node.findtext("name") or "").strip()]
        return {"title_original": _strip_code_prefix(original, code),
                "folder_title": title, "actors": actors}
    except Exception:
        return {"title_original": "", "folder_title": "", "actors": []}


# ─────────────────────────────────────────
# 核心：刮削单个文件（就地写 NFO + 封面）
# ─────────────────────────────────────────

async def _scrape_one(filepath: str, overwrite: bool, config: dict) -> dict:
    path = Path(filepath)
    if not path.exists():
        _log(f"刮削跳过：文件不存在 {filepath}")
        return {"success": False, "filepath": filepath, "error": "文件不存在"}

    # 优先：用「推送时列表已呈现的元数据」（番号/封面/演员/标签…）。
    #   命中则直接拿来刮削，免去从文件名重识别番号 + 重新刮削——纯数字番号(如 AVSOX「061326_01」)
    #   在文件名识别阶段易出错刮错封面/NFO，用已呈现内容最准。未命中再回退常规识别+搜索。
    pushed_meta = None
    try:
        import intake
        _ih, pushed_meta = await intake.resolve_for_file(
            path, config.get("scrape_watch_dir", ""), config)
    except Exception as e:
        _log(f"读取推送入库元数据失败（忽略，回退常规刮削）：{e}")
        pushed_meta = None

    if pushed_meta and pushed_meta.get("code"):
        code = pushed_meta["code"]
        _log(f"命中推送元数据：{path.name} → 番号 {code}（用已呈现内容刮削，免重识别/重刮削）")
    else:
        code = await _resolve_code(path, config)
        if not code:
            _log(f"刮削失败：无法从文件名提取番号 → {path.name}")
            return {"success": False, "filepath": filepath, "error": "无法从文件名提取番号"}
        _log(f"开始刮削：{path.name} → 番号 {code}")

    status = _get_file_status(path, code)
    if (not overwrite and status["has_nfo"] and status["has_poster"]
            and status["has_fanart"]):
        existing = _read_existing_nfo_metadata(path, code)
        actor_images_saved = 0
        if (config.get("scrape_actor_images_enabled", False)
                or config.get("emby_actor_sync_enabled", False)) and config.get("actor_scrape_auto", True):
            import actor_scraper
            # 当前目录尚未归档到 Emby 媒体库；这里只下载/回写头像，归档成功后再定向同步。
            result = await actor_scraper.process_nfo(
                path.parent / f"{code}.nfo", config, sync_emby=False)
            actor_images_saved = result.get("saved", 0)
            existing["actors"] = result.get("actors") or existing.get("actors", [])
            _log(f"NFO 和封面已存在，已补查演员头像：{code}（保存 {actor_images_saved} 张）")
        else:
            _log(f"已存在 NFO 和封面，跳过刮削：{code}")
        return {"success": True, "skipped": True, "filepath": filepath, "code": code,
                "reason": "NFO 和封面已存在", "actor_images_saved": actor_images_saved,
                **existing}

    proxy = config.get("proxy") or None
    provider = (config.get("scrape_translate_provider")
                or config.get("default_translate_provider", "baidu"))

    if pushed_meta and pushed_meta.get("code"):
        # 用推送时已呈现的元数据（番号/封面/标题最准）。
        movie = {k: v for k, v in pushed_meta.items() if not k.startswith("_")}
        # 但推送可能发生在详情尚未加载完时（点太快），元数据不全。此时【回到该条目自己的
        # 数据源、按它的 url 补抓】缺失字段，而不是按番号盲目搜索（纯数字番号会搜错来源/错片）。
        #   - detail_loaded=False：列表级条目，详情没加载过 → 补抓；
        #   - 或关键字段缺失（标题/封面）作兜底触发。
        # 只补「当前缺的」字段，已有的（来自展示）不覆盖；不同源字段差异（如 AVSOX 有简介、
        # 别的源没有）天然由「只查这一个源」保证——该源没有的就是没有，不去别处硬凑。
        incomplete = (not movie.get("detail_loaded")) or not movie.get("title") or not movie.get("cover")
        if incomplete and movie.get("url"):
            try:
                from scrapers import enrich
                enriched = await enrich([{"url": movie["url"], "source": movie.get("source", "")}], proxy=proxy)
                if enriched and enriched[0]:
                    filled = [k for k, v in enriched[0].items() if v and not movie.get(k)]
                    for k in filled:
                        movie[k] = enriched[0][k]
                    _log(f"推送元数据不全，回原源补抓：{code}（{movie.get('source','')}，补全 {len(filled)} 项）")
            except Exception as e:
                _log(f"原源补抓失败（用已有内容继续）：{code}: {e}")
        _log(f"用推送元数据刮削：{code} 标题《{(movie.get('title') or '')[:40]}》来源 {movie.get('source','')}")
    else:
        _log(f"搜索元数据：{code}（数据源 javbus/javdb，代理 {'有' if proxy else '无'}）")
        results = await search(query=code, mode=SEARCH_MODE_CODE, proxy=proxy,
                               sources=["javbus", "javdb"])
        if not results:
            _log(f"未找到影片信息：{code}（站点不可达或无该番号）")
            return {"success": False, "filepath": filepath, "code": code, "error": "未找到影片信息"}

        # 列表条目可能缺详情，补全第一条
        movie = results[0]
        _log(f"命中影片：{code} 标题《{(movie.get('title') or '')[:40]}》来源 {movie.get('source','')}")
        if not movie.get("actors") and movie.get("url"):
            try:
                from scrapers import enrich
                enriched = await enrich([{"url": movie["url"], "source": movie.get("source", "")}], proxy=proxy)
                if enriched and enriched[0]:
                    detail = enriched[0]
                    for k, v in detail.items():
                        if v and not movie.get(k):
                            movie[k] = v
                    _log(f"详情补全完成：{code}（演员 {len(movie.get('actors') or [])} 人）")
            except Exception as e:
                _log(f"详情补全失败 {code}: {e}")

    # 图片齐全时零额外请求；仅缺 poster/fanart 时按有限来源逐个补查，命中即停。
    if not movie.get("cover") or not movie.get("samples"):
        await _backfill_artwork(movie, code, config, proxy)

    # ── 标题/简介翻译 ──
    # 番号（字母+数字）不翻译，仅作前缀；只对真正的日文片名/简介长句翻译。
    raw_title = movie.get("title", "")
    name_part = _strip_code_prefix(raw_title, movie.get("code", "") or code)
    desc = movie.get("description", "")

    name_zh = name_part            # 默认保留原文（非日文时不翻译）
    plot_zh = desc

    # 刮削翻译总开关：
    #   关：标题/简介全部保留日文原文，不调翻译服务。
    #   开：标题、简介【各自独立翻译】、各自整体替换原日文——绝不拼接后再切分。
    #       （旧实现把标题+简介用 \n\n 拼一起翻译再按 \n\n 切回，简介含空行/翻译服务不保留
    #        分隔符时会错位：简介译文窜到标题、日文简介还残留在简介——本次修复点。）
    translate_on = config.get("scrape_translate_enabled", True)
    if not translate_on:
        _log(f"刮削翻译已关闭，标题/简介保留日文原文：{code}")
    else:
        async def _tr(text: str, what: str) -> str:
            # 空或不含日文：原样返回（不强译、不混日文）；翻译失败也回退原文
            if not text or not _has_jp(text):
                return text
            r = await translate(text=text, provider=provider, config=config)
            if r.get("success") and (r.get("result") or "").strip():
                return r["result"].strip()
            _log(f"翻译失败（保留原文）：{code} {what} — {r.get('error', '')}")
            return text

        if (name_part and _has_jp(name_part)) or (desc and _has_jp(desc)):
            _log(f"翻译（标题/简介各自独立）：{code}（服务 {provider}）")
            name_zh = await _tr(name_part, "标题")
            plot_zh = await _tr(desc, "简介")
            _log(f"翻译完成：{code} → 片名《{name_zh[:40]}》")
        else:
            _log(f"无需翻译（无日文片名/简介）：{code}")

    # NFO <title> = 番号 + 中文片名（番号永不翻译）
    title_for_nfo = _compose_title(code, name_zh)
    _log(f"NFO 标题：{title_for_nfo}")

    folder = path.parent
    saved_nfo = saved_cover = False

    if overwrite or not status["has_nfo"]:
        try:
            nfo_file = folder / f"{code}.nfo"
            nfo_file.write_text(_build_nfo(
                movie, title_for_nfo, plot_zh,
                config.get("scrape_actor_thumb_in_nfo", True)), encoding="utf-8")
            saved_nfo = True
            _log(f"已写入 NFO：{nfo_file.name}")
        except Exception as e:
            _log(f"NFO 写入失败 {filepath}: {e}")

    cover_url, fanart_url = _artwork_urls(movie)
    if cover_url and (overwrite or not status["has_poster"]
                      or (fanart_url and not status["has_fanart"])):
        # 优先复用首页/详情已缓存的封面（命中即零上游请求）；未命中再回源（含 FC2 防盗链兜底）
        _log(f"获取封面（优先复用缓存）：{code} ← {cover_url[:60]}")
        img = await _fetch_cover(cover_url, proxy) if (overwrite or not status["has_poster"]) else None
        if img:
            try:
                img, cropped = _poster_bytes(
                    img, _is_confirmed_jacket_cover(movie, cover_url))
                (folder / f"{code}-poster.jpg").write_bytes(img)
                saved_cover = True
                if cropped:
                    _log(f"已从完整横向封套右侧裁取标准竖版：{code}-poster.jpg")
                else:
                    _log(f"已写入封面：{code}-poster.jpg")
            except Exception as e:
                _log(f"封面保存失败 {filepath}: {e}")
        elif overwrite or not status["has_poster"]:
            movie.setdefault("_artwork_pending_sources", []).append("_download")
            _log(f"封面获取失败：{code}")

        # fanart/backdrop 必须来自独立且尺寸合格的横向样品图或宣传图。
        # 没有独立来源时宁可缺省，也不复制 poster 制造背景图。
        if fanart_url and (overwrite or not status["has_fanart"]):
            _log(f"获取背景图（独立样品/宣传图）：{code} ← {fanart_url[:60]}")
            fanart, selected_fanart_url = await _fetch_fanart(movie, proxy)
            if fanart:
                try:
                    (folder / f"{code}-fanart.jpg").write_bytes(fanart)
                    saved_cover = True
                    _log(f"已写入背景图：{code}-fanart.jpg")
                except Exception as e:
                    _log(f"背景图保存失败 {filepath}: {e}")
            else:
                movie.setdefault("_artwork_pending_sources", []).append("_download")
                _log(f"背景图获取失败，不复制封面兜底：{code}")
        elif not fanart_url:
            _log(f"无独立背景图来源，仅保存 poster：{code}")

    folder_title = name_zh
    # 文件夹标题单独开启翻译时，只把 name_part（纯标题）交给翻译器；演员列表始终保持源站原名，
    # 之后才由 _archive_folder_name 分字段拼接，禁止把“标题 + 演员”整体翻译。
    if (config.get("scrape_folder_naming") in {"code_title", "code_title_actor"}
            and config.get("scrape_folder_title_translate", False)
            and name_part and _has_jp(name_part) and name_zh == name_part):
        translated = await translate(text=name_part, provider=provider, config=config)
        if translated.get("success") and (translated.get("result") or "").strip():
            folder_title = translated["result"].strip()
    actor_images_saved = 0
    if (config.get("scrape_actor_images_enabled", False)
            or config.get("emby_actor_sync_enabled", False)) and config.get("actor_scrape_auto", True):
        try:
            import actor_scraper
            # 当前目录尚未归档到 Emby 媒体库；这里只下载/回写头像，归档成功后再定向同步。
            actor_result = await actor_scraper.process_nfo(
                folder / f"{code}.nfo", config, sync_emby=False)
            actor_images_saved = actor_result.get("saved", 0)
            if actor_result.get("actors"):
                movie["actors"] = actor_result["actors"]
        except Exception as e:
            _log(f"独立演员头像任务失败（不影响影片刮削）：{code}: {e}")

    _log(f"刮削结束：{code}（NFO={'有' if saved_nfo else '无'} 封面={'有' if saved_cover else '无'}）")
    return {"success": True, "skipped": False, "filepath": filepath, "code": code,
            "title_zh": title_for_nfo, "title_original": name_part,
            "folder_title": folder_title, "actors": movie.get("actors") or [],
            "actor_images_saved": actor_images_saved,
            "saved_nfo": saved_nfo, "saved_cover": saved_cover,
            "artwork": _artwork_context(movie)}


# ─────────────────────────────────────────
# 归档：视频(重命名为番号) + NFO/封面 → output_dir/YYYYMM/番号/
#   V1.5 统一：归档方式由全局 archive_mode 决定（hardlink/copy 保留原文件；move 移动后清原目录），
#   与发种流水线共用同一归档目录与按年月结构。
# ─────────────────────────────────────────

def _commit_temp_without_overwrite(tmp: Path, dst: Path) -> None:
    """把目标侧临时文件提交为 dst，并保证目标已存在时绝不覆盖。"""
    try:
        # tmp 与 dst 在同一目录/文件系统；link 是原子的，且目标存在时必定失败。
        os.link(str(tmp), str(dst))
        tmp.unlink()
        return
    except FileExistsError:
        raise
    except OSError:
        pass

    # 不支持硬链接的目标文件系统（部分 NAS/网络卷）：用独占创建，仍不覆盖已有目标。
    created = False
    try:
        with tmp.open("rb") as inp, dst.open("xb") as out:
            created = True
            shutil.copyfileobj(inp, out, length=4 * 1024 * 1024)
            out.flush()
            os.fsync(out.fileno())
        shutil.copystat(str(tmp), str(dst))
        tmp.unlink()
    except Exception:
        if created:
            try:
                dst.unlink()
            except Exception:
                pass
        raise


def _diagnose_source_remove_error(path: Path, error: Exception) -> tuple[str, str]:
    """把删除源文件的系统异常转成用户可操作的原因与建议；不尝试强制解锁。"""
    winerror = getattr(error, "winerror", None)
    err_no = getattr(error, "errno", None)
    readonly = False
    try:
        readonly = not bool(path.stat().st_mode & stat.S_IWUSR)
    except Exception:
        pass

    is_linux = sys.platform.startswith("linux")

    if is_linux and err_no == errno.EBUSY:
        return ("文件或所在挂载点正忙（Linux EBUSY）",
                "请检查该路径是否为活动挂载点、套娃挂载、NFS/CIFS 正在重连，或被系统服务占用")
    if is_linux and err_no == getattr(errno, "ESTALE", -1):
        return ("NFS/CIFS 文件句柄已失效（ESTALE）",
                "请确认 NAS 网络与远端共享正常，重新挂载共享后再扫描；不要直接删除当前目录")
    if is_linux and err_no in {
            errno.EIO, getattr(errno, "EREMOTEIO", -1),
            getattr(errno, "ENOTCONN", -1), errno.ETIMEDOUT}:
        return ("NAS/网络文件系统 I/O 或连接异常",
                "请检查群晖存储池、磁盘健康、NFS/CIFS 连接和远端服务器日志，恢复后重试")
    if is_linux and err_no == getattr(errno, "ETXTBSY", -1):
        return ("文件作为正在运行的程序映像被系统占用（ETXTBSY）",
                "请停止对应进程或容器后重试")
    if winerror in {32, 33} or (not is_linux and err_no == errno.EBUSY):
        return ("文件被其他程序占用或锁定",
                "请停止下载器校验/做种占用、播放器、Emby 扫描或安全软件实时扫描后重试")
    if winerror == 5 or err_no in {errno.EACCES, errno.EPERM}:
        if readonly:
            return ("源文件带只读属性",
                    "请取消文件只读属性，并确认容器挂载不是只读后重试")
        return ("当前进程权限不足，或被安全软件/ACL 拒绝",
                "请检查源文件及父目录权限、Docker 挂载读写权限和安全软件拦截记录")
    if err_no == errno.EROFS:
        return ("源目录位于只读文件系统或只读挂载",
                "请将下载目录改为可写挂载（Docker 卷不要使用 :ro）后重试")
    if err_no == errno.ENOENT:
        return ("源文件在删除前已被外部程序移动或删除",
                "请刷新下载器与监控目录状态后重新扫描")
    codes = []
    if winerror is not None:
        codes.append(f"winerror={winerror}")
    if err_no is not None:
        codes.append(f"errno={err_no}")
    suffix = f"（{', '.join(codes)}）" if codes else ""
    return (f"其他文件系统错误{suffix}",
            "请根据下方系统错误检查磁盘、文件系统、挂载状态和容器日志")


def _linux_remove_context(path: Path) -> str:
    """采集群晖/Linux 删除失败上下文；所有检测均只读，失败时静默降级。"""
    if not sys.platform.startswith("linux"):
        return ""
    facts = []
    try:
        resolved = path.resolve()
    except Exception:
        resolved = path

    # 删除权限取决于父目录的写入+执行权限，而不是视频文件本身是否可写。
    try:
        parent = resolved.parent
        st = parent.stat()
        can_delete = os.access(str(parent), os.W_OK | os.X_OK)
        facts.append(
            f"父目录权限={'可写入/遍历' if can_delete else '不可写入或不可遍历'}"
            f"(mode={stat.filemode(st.st_mode)}, uid={st.st_uid}, gid={st.st_gid}, "
            f"进程uid={os.geteuid()}, gid={os.getegid()})")
    except Exception as e:
        facts.append(f"父目录权限检测失败:{type(e).__name__}")

    # statvfs 能直接反映当前容器视角是否为只读挂载。
    try:
        vfs = os.statvfs(str(resolved.parent))
        readonly_flag = getattr(os, "ST_RDONLY", 1)
        facts.append(f"挂载状态={'只读' if vfs.f_flag & readonly_flag else '可写'}")
    except Exception as e:
        facts.append(f"挂载状态检测失败:{type(e).__name__}")

    # Linux ext/btrfs 等文件系统的 immutable/append-only 属性会让 root 也无法 unlink。
    try:
        import fcntl
        flags = array.array("I", [0])
        fd = os.open(str(resolved), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        try:
            fcntl.ioctl(fd, 0x80086601, flags, True)  # FS_IOC_GETFLAGS
        finally:
            os.close(fd)
        attrs = []
        if flags[0] & 0x10:
            attrs.append("immutable(+i)")
        if flags[0] & 0x20:
            attrs.append("append-only(+a)")
        if attrs:
            facts.append("文件属性=" + ",".join(attrs))
    except Exception:
        pass

    # /proc/locks 只包含 POSIX/flock 等建议锁；锁通常不阻止 Linux unlink，但可提示相关 PID。
    lock_pids = set()
    try:
        st = resolved.stat()
        wanted = (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)
        for line in Path("/proc/locks").read_text(encoding="utf-8", errors="replace").splitlines():
            fields = line.split()
            if len(fields) < 6:
                continue
            dev_inode = fields[5].split(":")
            if len(dev_inode) != 3:
                continue
            try:
                current = (int(dev_inode[0], 16), int(dev_inode[1], 16), int(dev_inode[2]))
            except ValueError:
                continue
            if current == wanted:
                lock_pids.add(fields[4])
        if lock_pids:
            facts.append(f"检测到Linux建议锁PID={','.join(sorted(lock_pids))}（建议锁通常不阻止unlink）")
    except Exception:
        pass

    # 找可见的本机进程句柄；容器权限不足或 /proc 隔离时只能看到部分进程。
    holders = []
    try:
        wanted = str(resolved)
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            fd_dir = proc / "fd"
            try:
                matched = any(str(fd.resolve()) == wanted for fd in fd_dir.iterdir())
            except Exception:
                continue
            if not matched:
                continue
            try:
                name = (proc / "comm").read_text(encoding="utf-8", errors="replace").strip()
            except Exception:
                name = "unknown"
            holders.append(f"{proc.name}:{name}")
            if len(holders) >= 8:
                break
        if holders:
            facts.append("可见打开句柄=" + ",".join(holders)
                         + "（Linux普通打开句柄通常不阻止删除）")
    except Exception:
        pass
    return "；".join(facts)


def _transfer(src: Path, dst: Path, mode: str) -> bool:
    """安全地把 src 落到 dst；绝不覆盖已有的不同文件。

    move 优先用同卷硬链接+删除源文件，跨卷才先复制到目标侧临时文件；只有目标完整落地后
    才删除源文件。copy 也先写临时文件再原子落位，避免失败时留下半个成片。
    """
    mode = mode if mode in {"move", "copy", "hardlink"} else "hardlink"
    # 归档目录位于监控目录内时，旧版本会再次扫到归档成品。src/dst 若为同一文件，
    # 绝不能先 unlink(dst)，否则影片会被自身归档流程删除。
    try:
        if src.resolve() == dst.resolve() or (dst.exists() and os.path.samefile(src, dst)):
            _log(f"跳过同路径归档：{src}")
            return True
    except (OSError, ValueError):
        pass

    # 已有目标一律保留。视频重名会由上层按分段命名避让；NFO/封面/头像冲突则保留先到版本。
    if dst.exists():
        _log(f"归档目标已存在，拒绝覆盖：{dst}")
        return False

    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.{time.time_ns()}.tmp")
    move_target_created = False
    move_phase = ""
    try:
        if mode == "move":
            try:
                # 同卷：建立目标硬链接成功后再删源，任何一步失败都至少保留一份完整数据。
                os.link(str(src), str(dst))
                move_target_created = True
            except OSError:
                # 跨卷：先完整复制到目标侧临时文件，再落位；源文件最后才删除。
                shutil.copy2(str(src), str(tmp))
                _commit_temp_without_overwrite(tmp, dst)
                move_target_created = True
            if not dst.exists() or dst.stat().st_size != src.stat().st_size:
                raise OSError("归档目标校验失败，保留源文件")
            move_phase = "remove_source"
            src.unlink()
        elif mode == "copy":
            shutil.copy2(str(src), str(tmp))
            _commit_temp_without_overwrite(tmp, dst)
        else:  # hardlink（默认）
            try:
                os.link(str(src), str(dst))
            except OSError:
                shutil.copy2(str(src), str(tmp))   # 跨卷无法硬链 → 安全复制
                _commit_temp_without_overwrite(tmp, dst)
        return True
    except Exception as e:
        # move 的最后一步（校验/删除源）失败时撤销本次新建目标，恢复成可安全重试的原状态。
        # 如果回滚本身失败也仍至少保留源文件，不会继续做目录清理。
        rollback = "未创建目标，无需回滚"
        if mode == "move" and move_target_created and src.exists():
            try:
                dst.unlink()
                rollback = "已撤销本次新建的归档目标，源文件保持原样"
            except Exception as rollback_error:
                rollback = f"目标回滚失败，但源文件仍保留：{rollback_error}"
                _log(f"移动失败后目标回滚失败（源文件仍保留）：{dst} — {rollback_error}")
        if mode == "move" and move_phase == "remove_source":
            reason, action = _diagnose_source_remove_error(src, e)
            linux_context = _linux_remove_context(src)
            error_codes = ", ".join(
                part for part in [
                    f"winerror={getattr(e, 'winerror', None)}" if getattr(e, "winerror", None) is not None else "",
                    f"errno={getattr(e, 'errno', None)}" if getattr(e, "errno", None) is not None else "",
                ] if part) or "无错误码"
            _log(f"移动未完成：无法删除源文件 {src}；原因判断：{reason}；"
                 f"建议：{action}；安全处理：{rollback}；"
                 f"系统错误：{type(e).__name__}: {e}（{error_codes}）"
                 + (f"；Linux现场检测：{linux_context}" if linux_context else ""))
        else:
            _log(f"归档落地失败 {src.name}（{mode}）: {type(e).__name__}: {e}")
        return False
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def _build_archive_index(output_dir: str) -> dict:
    """扫描归档目录，建立「已归档」索引，作为去重签名(scrape_processed.json)缺失时的兜底，
    避免重启后把原目录里早已归档的文件重新识别/重刮/重复归档（含错误的 -cd 重编号）。
    签名文件会在以下情况对老文件为空：旧版本归档（当时未落盘签名）、/config 卷被重置、
    归档目录路径变更等。此索引直接以「归档目录现有内容」为准，不依赖签名文件：
      - inodes：{(st_dev, st_ino)} —— hardlink 模式(默认)源文件与归档文件同 inode，精确命中；
      - sizes ：{(归一化番号, 字节数)} —— copy 模式跨 inode，用 番号+字节数 命中。
    （move 模式源文件已移走，本就不会被再次扫描，无需索引。）"""
    idx = {"inodes": set(), "sizes": set(),
           "paths_by_inode": {}, "paths_by_size": {}}
    if not output_dir:
        return idx
    out = Path(output_dir)
    try:
        for p in out.rglob("*"):
                if not (p.is_file() and p.suffix.lower() in VIDEO_EXTS):
                    continue
                safe = _norm(_code_from_name(p.parent.name) or _code_from_name(p.stem) or "")
                try:
                    stt = p.stat()
                except Exception:
                    continue
                if stt.st_ino:
                    inode_key = (stt.st_dev, stt.st_ino)
                    idx["inodes"].add(inode_key)
                    idx["paths_by_inode"].setdefault(inode_key, p)
                if safe:
                    size_key = (safe, stt.st_size)
                    idx["sizes"].add(size_key)
                    idx["paths_by_size"].setdefault(size_key, p)
    except Exception as e:
        _log(f"建立归档索引失败（忽略，回退仅靠签名去重）：{e}")
    return idx


def _is_already_archived(video_path: Path, size: int, code: str, idx: dict) -> bool:
    """该文件是否已存在于归档目录（用 _build_archive_index 的索引判定）。"""
    if not idx:
        return False
    try:
        stt = video_path.stat()
        if stt.st_ino and (stt.st_dev, stt.st_ino) in idx["inodes"]:
            return True              # hardlink：与归档文件同 inode → 必是同一份
    except Exception:
        pass
    if code:
        return (_norm(_safe_name(code)), size) in idx["sizes"]   # copy：番号+字节数命中
    return False


def _archived_target_for_source(video_path: Path, size: int, code: str,
                                idx: dict) -> Optional[Path]:
    """从归档索引反查 hardlink/copy 源文件已经落到的准确最终目录。"""
    try:
        stt = video_path.stat()
        if stt.st_ino:
            found = idx.get("paths_by_inode", {}).get((stt.st_dev, stt.st_ino))
            if found:
                return Path(found).parent
    except Exception:
        pass
    if code:
        found = idx.get("paths_by_size", {}).get((_norm(_safe_name(code)), size))
        if found:
            return Path(found).parent
    return None


def _discover_legacy_archive_artwork(archive_idx: dict, config: dict) -> bool:
    """每小时最多发现一个旧归档缺图任务，优先最近归档，避免升级后批量联网。"""
    global _artwork_legacy_last
    now = time.time()
    if now - _artwork_legacy_last < 60 * 60:
        return False
    _artwork_legacy_last = now
    paths = set(archive_idx.get("paths_by_inode", {}).values())
    paths.update(archive_idx.get("paths_by_size", {}).values())
    try:
        paths = sorted(paths, key=lambda p: Path(p).stat().st_mtime, reverse=True)
    except Exception:
        paths = list(paths)
    seen = set()
    for video in paths:
        folder = Path(video).parent
        if folder in seen:
            continue
        seen.add(folder)
        code = _code_from_name(folder.name) or _code_from_name(Path(video).stem)
        if not code:
            continue
        # 只接管已有 NFO/poster、但缺 fanart 的旧归档；其它不完整归档不在这里重刮。
        if ((folder / f"{code}.nfo").exists()
                and (folder / f"{code}-poster.jpg").exists()
                and not (folder / f"{code}-fanart.jpg").exists()):
            return _queue_artwork_backfill(folder, code, None, config)
    return False


def _archived_video_parts(target_dir: Path, safe_code: str) -> list:
    """目标归档目录内、属于该番号的已归档视频文件（CODE.ext 或 CODE-cdN.ext）。
    用于判定是否多分段、以及对先到分集做命名自愈。"""
    out = []
    low = (safe_code or "").lower()
    if not low:
        return out
    pat = re.compile(re.escape(low) + r'(?:-cd\d{1,2})?$')
    try:
        for p in target_dir.iterdir():
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS and pat.fullmatch(p.stem.lower()):
                out.append(p)
    except Exception:
        pass
    return out


def _heal_archived_parts(target_dir: Path, safe_code: str) -> None:
    """分集命名自愈：分批完成时，先到的分集曾因「当时只看到 1 个正片」被归档成无后缀的
    CODE.ext。后续分集落地后，本目录内若仍残留无后缀文件，则给它补上缺失的最小 -cdN 号，
    消除「CODE.mp4 + CODE-cd2.mp4」这类堆叠错乱（缺 -cd1）。
    仅重命名归档产物，不触碰源文件，也不写去重签名（签名按源路径计，与此无关）。"""
    files = _archived_video_parts(target_dir, safe_code)
    if len(files) <= 1:
        return
    low = safe_code.lower()
    plain = [p for p in files if p.stem.lower() == low]
    if not plain:
        return                          # 都已带 -cdN，无需纠正
    used = set()
    for p in files:
        m = re.search(r'-cd(\d{1,2})$', p.stem.lower())
        if m:
            used.add(int(m.group(1)))
    for p in plain:
        n = 1
        while n in used:
            n += 1
        used.add(n)
        dst = p.with_name(f"{safe_code}-cd{n}{p.suffix.lower()}")
        if dst.exists():
            continue
        try:
            p.rename(dst)
            _log(f"分集命名自愈：{p.name} → {dst.name}（同番号已有分集，补齐缺失的 -cd 号）")
        except Exception as e:
            _log(f"分集命名自愈失败 {p.name}: {e}")


def _archive_file(video_path: Path, output_dir: str, code: str,
                  mode: str = "hardlink", rename: bool = True,
                  watch_dir: str = "", folder_name: str = "",
                  by_month: bool = True) -> dict:
    """
    把视频归档到 归档目录/年月/番号/ 子目录下（Emby 单片单目录布局）。
    rename：开（刮削开）= 视频改名「番号.后缀」、随带番号命名的 NFO/封面；
            关（刮削关）= 保留原文件名、不带 NFO/封面。
    mode：hardlink/copy 保留原下载文件（原文件留存供做种/辅种）；move 移动（原文件离开下载目录）。
    多分段（同番号多个正片）：视频名加 -cd1/-cd2… 堆叠后缀，避免同名互相覆盖、确保全部归档。
    返回 {archived, moved_original, target_dir, files}。
    """
    mode = (mode or "hardlink").lower()
    safe_code = _safe_name(code) if code else video_path.stem
    out = Path(output_dir)
    month_dir = out / datetime.now().strftime("%Y%m") if by_month else out
    target_dir = month_dir / (folder_name or safe_code)
    # 同番号已经归档时复用原目录，避免标题翻译变化制造重复影片目录。
    try:
        safe_low = safe_code.lower()
        existing = next((p for p in month_dir.iterdir()
                         if p.is_dir() and (p.name.lower() == safe_low
                                            or p.name.lower().startswith(safe_low + " "))), None)
        if existing:
            target_dir = existing
    except FileNotFoundError:
        pass
    except Exception as e:
        _log(f"检查已有归档目录失败，停止归档以免落入错误目录：{e}")
        return {"archived": False, "moved_original": False,
                "error": f"检查已有归档目录失败: {e}"}
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        _log(f"无法创建归档目录 {target_dir}: {e}")
        return {"archived": False, "moved_original": False, "error": f"无法创建归档目录: {e}"}

    folder = video_path.parent
    done = []

    # 文件已经位于最终归档目录时视为完成，绝不再次推导分段号或改名。
    # 否则它会把自身计入 archived_before，错误地从 CODE.mp4 改成 CODE-cd1.mp4。
    try:
        if folder.resolve() == target_dir.resolve():
            _log(f"影片已在最终归档目录，跳过移动和改名：{video_path}")
            return {"archived": video_path.exists(), "moved_original": False,
                    "target_dir": str(target_dir), "files": [video_path.name]}
    except Exception:
        pass

    # 1) 视频本体 → 番号[-cdN].后缀（刮削关则保留原文件名）
    #    多分段时加 -cd1/-cd2… 堆叠后缀，确保 A/B/C、1/2/3、CD1/CD2 等全部归档不互相覆盖。
    #    -cdN 取自文件名自带的分集序号，与完成顺序无关；已归档分集计入多分段判定。
    archived_before = _archived_video_parts(target_dir, safe_code) if (rename and code) else []
    part = (_part_suffix(video_path, code, watch_dir, archived_parts=len(archived_before))
            if (rename and code) else "")
    video_name = f"{safe_code}{part}{video_path.suffix.lower()}" if (rename and code) else video_path.name
    video_dst = target_dir / video_name
    if not _transfer(video_path, video_dst, mode):
        return {"archived": False, "moved_original": False,
                "error": "视频归档失败", "target_dir": str(target_dir)}
    video_source_removed = mode == "move" and not video_path.exists() and video_dst.exists()
    done.append(video_dst.name)
    # 本次落地的是某个 -cdN 分集时，纠正同番号中先到、曾被命名成无后缀的分集（补齐缺失号）。
    if rename and code and part:
        _heal_archived_parts(target_dir, safe_code)

    # 2) NFO/封面：Linux 大小写敏感，按小写文件名匹配后统一用番号命名落地。
    expected = {
        f"{safe_code}.nfo".lower(): f"{safe_code}.nfo",
        f"{safe_code}-poster.jpg".lower(): f"{safe_code}-poster.jpg",
        f"{safe_code}-fanart.jpg".lower(): f"{safe_code}-fanart.jpg",
    }
    found_extras = {}
    try:
        for candidate in folder.iterdir():
            if candidate.is_file() and candidate.name.lower() in expected:
                found_extras[candidate.name.lower()] = candidate
    except Exception as e:
        _log(f"扫描刮削附属文件失败：{folder} — {e}")
    sub_mode = "move" if mode == "move" else "copy"
    for lower_name, dst_name in expected.items():
        extra = found_extras.get(lower_name)
        if extra and _transfer(extra, target_dir / dst_name, sub_mode):
            done.append(dst_name)
        elif (target_dir / dst_name).exists():
            # 多分段第二段处理时，附属文件可能已随第一段归档。
            continue
        else:
            _log(f"归档提示：未找到附属文件 {dst_name}")

    # 3) 演员头像本地缓存始终随影片归档；它是保底缓存，不依赖 Emby 全局 people 映射。
    # 新版本使用可见的 actors；旧版 .actors 也合并进去，避免升级后丢失缓存。
    for actors_src in (folder / "actors", folder / ".actors"):
      if actors_src.is_dir():
        actors_dst = target_dir / "actors"
        actors_dst.mkdir(parents=True, exist_ok=True)
        for actor_img in actors_src.iterdir():
            if not actor_img.is_file():
                continue
            if _transfer(actor_img, actors_dst / actor_img.name, sub_mode):
                done.append(f"actors/{actor_img.name}")

    how = {"move": "移动", "copy": "复制", "hardlink": "硬链接"}.get(mode, mode)
    _log(f"归档（{how}）：{len(done)} 个文件 → {target_dir} （{', '.join(done)}）")
    return {"archived": bool(done), "moved_original": video_source_removed,
            "target_dir": str(target_dir), "files": done}


def _cleanup_source(video_parent: Path, watch_dir: Path, min_bytes: int,
                    keep_bytes: int = 300 * 1024 * 1024):
    """
    移动走视频后清理原下载位置：
      - 若视频原本在 watch_dir 下的「子目录」里（典型 qB 单种子单目录），
        且该子目录已无其它达标视频待处理 → 整个子目录连同遗留广告/样板文件一并删除；
      - 若还有其它达标视频 → 保留子目录，待最后一个视频处理完再删；
      - 若视频直接位于 watch_dir 根目录 → 不删根目录（仅此前已移走视频本体）。
    """
    try:
        watch_dir = watch_dir.resolve()
        parent = video_parent.resolve()
    except Exception:
        return
    if parent == watch_dir or watch_dir not in parent.parents:
        # 视频直接在根目录，或父目录不在监控目录内：不做整目录删除
        return
    # 只要还存在任何视频就保留整个目录，不按大小、命名或“广告”推断删除。
    # 自动清理可以少做，但不能把短片、命名异常影片或未完成文件随目录误删。
    try:
        remaining = [p for p in parent.rglob("*")
                     if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
    except Exception as e:
        # 无法证明目录已经没有正片时，绝不能执行整目录删除。
        _log(f"检查原目录剩余文件失败，取消清理并保留目录：{parent} — {e}")
        return
    if remaining:
        _log(f"原目录仍有 {len(remaining)} 个视频，安全保留整个目录：{parent.name}")
        return
    try:
        shutil.rmtree(parent)
        _log(f"已删除原下载目录（含遗留文件）：{parent}")
    except Exception as e:
        _log(f"删除原目录失败 {parent}: {e}")


# ─────────────────────────────────────────
# 监控：完成检测 + 处理一个文件（刮削 → 移动）
# ─────────────────────────────────────────

def _is_incomplete(video_path: Path) -> bool:
    """qBittorrent 未完成分片会有 同名 + .!qB 标记文件。"""
    return (video_path.parent / (video_path.name + ".!qB")).exists()


def _iter_video_files(watch_dir: Path, excluded_roots=None):
    excluded = set(excluded_roots or ())
    try:
        for p in watch_dir.rglob("*"):
            if (p.is_file() and p.suffix.lower() in VIDEO_EXTS
                    and not _under_any(p, excluded)):
                yield p
    except Exception as e:
        _log(f"遍历监控目录失败: {e}")


def _under_any(path: Path, roots: set) -> bool:
    """path 是否等于、或位于 roots 中任一目录的子树下（均按 resolve 比较）。
    用于发种占用第二层保护：roots 由 publish.active_paths 提供（番号文件夹/原始下载内容路径）。"""
    if not roots:
        return False
    try:
        p = path.resolve()
    except Exception:
        return False
    for r in roots:
        if p == r or r in p.parents:
            return True
    return False


def _path_key(value: str) -> str:
    """下载器路径统一为小写 POSIX 形式，仅用于跨容器路径匹配。"""
    return re.sub(r'/+', '/', (value or '').replace('\\', '/').strip('/')).lower()


def _match_downloader_torrent(video_path: Path, watch_dir: Path,
                              torrents: list) -> Optional[dict]:
    """按相对路径、任务名、content_path 末级和 TR 文件清单匹配所属任务。

    下载器与本容器通常使用不同的挂载前缀，因此不比较绝对路径前缀。
    """
    try:
        relative = video_path.resolve().relative_to(watch_dir.resolve())
        rel = _path_key(str(relative))
    except Exception:
        rel = _path_key(video_path.name)
    if not rel:
        return None
    parts = rel.split('/')
    first, filename = parts[0], parts[-1]
    for torrent in torrents or []:
        name = _path_key(torrent.get("name", ""))
        content_name = _path_key(Path(torrent.get("content_path", "")).name)
        file_names = [_path_key(f) for f in (torrent.get("files") or []) if f]
        # 单文件任务：任务名通常就是完整文件名。
        if name and (rel == name or (len(parts) == 1 and filename == name)):
            return torrent
        # 多文件任务：监控目录下第一层一般就是 torrent name/content_path 末级。
        if len(parts) > 1 and first in {name, content_name} - {""}:
            return torrent
        # Transmission 可直接返回 torrent 内相对文件清单。
        if any(rel == f or rel.endswith('/' + f) or f.endswith('/' + rel)
               for f in file_names):
            return torrent
    return None


async def _download_state_snapshot(config: dict) -> tuple[bool, list]:
    """返回（是否可安全依赖下载器状态，任务列表）。

    已配置但连接失败时返回 False：调用方必须暂停本轮，不能降级用文件大小猜测。
    未配置下载器时返回 True, []，允许手动文件继续走静置兜底。
    """
    import downloader
    if not downloader.is_configured(config):
        return True, []
    status = await downloader.get_status(config)
    if not status.get("online"):
        _log(f"下载器状态不可用，暂停本轮自动处理：{status.get('message', '连接失败')}")
        return False, []
    return True, await downloader.list_torrents(config)


async def _process_completed_file(video_path: Path, config: dict) -> dict:
    """对一个判定为下载完成的视频文件执行：刮削(可关) → 按配置归档(可关)。"""
    fp = str(video_path)
    output_dir = config.get("scrape_output_dir", "").strip()
    move_on_fail = config.get("scrape_move_on_fail", True)
    # 全局刮削/归档总开关（监控 & 发种共用）；兼容旧 publish_* 键
    scrape_meta = config.get("scrape_meta_enabled", config.get("publish_scrape_enabled", True))
    organize_on = config.get("scrape_organize_enabled", scrape_meta)
    archive_on = config.get("archive_enabled", config.get("publish_archive_enabled", True))

    if scrape_meta:
        try:
            scrape_res = await _scrape_one(fp, overwrite=False, config=config)
        except Exception as e:
            # 刮削过程意外报错也不应阻止归档（符合「刮削正常运行但刮不到也归档」）
            _log(f"刮削过程异常（将按失败处理）：{video_path.name} — {e}")
            scrape_res = {"success": False, "filepath": fp, "code": await _resolve_code(video_path, config),
                          "error": f"刮削异常: {e}"}
    else:
        # 刮削关：不抓元数据/不写 NFO/封面，仅识别番号用于归档分目录（保留原文件名）
        _log(f"刮削已关闭，仅识别番号后归档（保留原文件名）：{video_path.name}")
        scrape_res = {"success": True, "filepath": fp,
                      "code": await _resolve_code(video_path, config),
                      "title_zh": "", "error": "", "skipped": True}
    # success 含「已跳过」；真正失败（找不到番号/影片信息）才是 success=False
    failed = not scrape_res.get("success")

    record = {
        "file": video_path.name,
        "code": scrape_res.get("code", ""),
        "title_zh": scrape_res.get("title_zh", ""),
        "scrape_ok": scrape_res.get("success", False),
        "scrape_error": scrape_res.get("error", ""),
        "moved": False,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    # 归档：需 归档总开关开 + 配了归档目录 + (刮削成功 或 允许失败仍归档)
    if not archive_on:
        _log(f"归档已关闭（仅刮削，保留原处）：{video_path.name}")
        record["note"] = "归档已关闭，保留原处"
    elif output_dir and (not failed or move_on_fail):
        if failed:
            _log(f"刮削未成功但按配置仍归档：{video_path.name}")
        watch_dir = Path(config.get("scrape_watch_dir", ""))
        min_bytes = int(config.get("scrape_min_size_mb", 100)) * 1024 * 1024
        keep_bytes = int(config.get("scrape_keep_size_mb", 300)) * 1024 * 1024
        code = scrape_res.get("code", "") or await _resolve_code(video_path, config)
        folder_name = _archive_folder_name(
            code, scrape_res.get("title_original", ""),
            scrape_res.get("folder_title", ""), scrape_res.get("actors", []), config)
        src_parent = video_path.parent
        # V1.5 统一：归档方式取全局 archive_mode（默认 hardlink 保留原文件；move 才移走+清原目录）
        mode = (config.get("archive_mode") or "hardlink").lower()
        mv = _archive_file(video_path, output_dir, code, mode=mode, rename=organize_on,
                           watch_dir=str(watch_dir), folder_name=folder_name,
                           by_month=config.get("archive_by_month", True))
        record["moved"] = mv.get("archived", False)
        record["archive_mode"] = mode
        record["target_dir"] = mv.get("target_dir", "")
        if (mv.get("archived") and scrape_meta and mv.get("target_dir")
                and scrape_res.get("artwork") is not None):
            _queue_artwork_backfill(
                Path(mv["target_dir"]), code, scrape_res.get("artwork"), config)
        if (mv.get("archived") and config.get("emby_actor_sync_enabled", False)
                and scrape_res.get("actor_images_saved", 0)):
            try:
                import actor_scraper
                emby_result = await actor_scraper.sync_emby_folder(
                    Path(mv["target_dir"]), config, code, scrape_res.get("actors") or None)
                record["emby_updated"] = emby_result.get("emby_updated", 0)
                record["emby_message"] = emby_result.get("message", "")
            except Exception as e:
                record["emby_updated"] = 0
                record["emby_message"] = f"Emby 定向同步异常：{e}"
                _log(record["emby_message"])
        if mv.get("moved_original"):
            # 仅 move 模式：原文件已移走，清理原下载目录（含遗留广告/样板文件，连同子目录删除）。
            # hardlink/copy 模式保留原文件（可继续做种/辅种），绝不删原目录。
            _cleanup_source(src_parent, watch_dir, min_bytes, keep_bytes)
    elif not output_dir:
        _log(f"未配置归档目录，仅刮削未归档：{video_path.name}")
        record["note"] = "未配置归档目录，仅刮削未归档"
    else:
        _log(f"刮削失败且未开启「失败仍归档」，保留原处：{video_path.name}")

    return record


def _record_recent(rec: dict):
    _monitor_state["recent"].insert(0, rec)
    del _monitor_state["recent"][30:]
    _monitor_state["processed_total"] += 1


async def _scan_once(config: dict) -> int:
    """扫描监控目录一遍：对稳定且完成的文件做处理。返回本轮处理数。"""
    watch = config.get("scrape_watch_dir", "").strip()
    if not watch:
        _log("未配置监控目录，跳过扫描")
        return 0
    watch_dir = Path(watch)
    if not watch_dir.exists():
        _log(f"监控目录不存在（检查 Docker 卷映射 / 容器内路径）：{watch}")
        _monitor_state["message"] = f"监控目录不存在: {watch}"
        return 0

    _load_processed()   # 重启后从磁盘恢复「已归档」记录，避免 hardlink/copy 保留的原文件被反复处理
    stable_needed = int(config.get("scrape_stable_checks", 2))
    settle_seconds = int(config.get("scrape_settle_seconds", 60))
    min_bytes = int(config.get("scrape_min_size_mb", 100)) * 1024 * 1024
    keep_bytes = int(config.get("scrape_keep_size_mb", 300)) * 1024 * 1024
    organize_on = config.get("scrape_organize_enabled",
                             config.get("scrape_meta_enabled", True))
    delete_extras = bool(config.get("scrape_delete_extras", False))
    out_dir = config.get("scrape_output_dir", "").strip()
    excluded_roots = set()
    if out_dir:
        try:
            output_root = Path(out_dir).resolve()
            watch_root = watch_dir.resolve()
            if output_root == watch_root or watch_root in output_root.parents:
                excluded_roots.add(output_root)
                _log(f"归档目录位于监控目录内，扫描时已排除归档子树：{output_root}")
        except Exception as e:
            _log(f"归档目录排除检查失败，保守跳过本轮扫描：{e}")
            return 0
    # 以归档目录现有内容为准的兜底去重索引（签名文件缺失时仍能跳过早已归档的原文件）。
    archive_idx = _build_archive_index(out_dir)
    # 归档图片补全与视频处理解耦：每轮最多一个到期任务，直接写最终归档目录。
    await _run_pending_artwork(config)
    _discover_legacy_archive_artwork(archive_idx, config)
    now = time.time()
    processed = 0
    n_total = n_done_before = n_incomplete = n_small = n_waiting = n_extra = n_publish = 0
    n_backfilled = 0   # 本轮以归档目录为准补登签名（兜底跳过）的文件数

    # 发种占用：这些文件正被发种流水线原地做种，监控绝不能移动/删除（否则做种丢文件）。
    # 两层保护互补，命中任一即跳过：
    #   ① 按番号 active_codes —— 路径对不上（如未拿到 content_path）但番号识别得出时仍保护；
    #   ② 按路径 active_paths —— 番号识别不出（命名怪异/嵌套深）但文件落在发种占用路径下时仍保护。
    # 懒加载导入避免与 publish.py 的循环依赖。
    try:
        import publish as _publish
        pub_active = _publish.active_codes()
        pub_paths = _publish.active_paths(config)
    except Exception:
        pub_active = set()
        pub_paths = set()

    state_ok, torrent_tasks = await _download_state_snapshot(config)
    if not state_ok:
        _monitor_state["message"] = "下载器状态不可用，已暂停自动处理以防提前规整"
        return 0
    if torrent_tasks:
        done_count = sum(1 for task in torrent_tasks if task.get("completed"))
        _log(f"已读取下载器任务状态：共 {len(torrent_tasks)} 个，已完成 {done_count} 个")

    _log(f"开始扫描监控目录：{watch}（归档目录：{out_dir or '未配置'}）")
    for vf in _iter_video_files(watch_dir, excluded_roots):
        n_total += 1
        fp = str(vf)
        if fp in _processed:
            n_done_before += 1
            continue
        # 发种任务占用（未终止）→ 跳过本轮，不入 _processed：待发种结束(终态)后自动恢复正常归档。
        # ① 按番号：识别必须与归档同深度（上溯各级父目录，复用 _recognize_code）——否则视频嵌在
        #    「番号/子目录/video.mp4」这类多层结构里时，这里只看文件名+直接父目录会识别不出番号、
        #    不跳过，而归档却能从祖父目录认出番号照常移动+删原目录，把正在做种的发种数据搬空。
        # ② 按路径：命名怪异、连父目录都不含番号时，只要文件落在发种占用路径（番号文件夹/原始下载
        #    内容）的子树下就跳过，作为番号识别的兜底。
        if pub_active or pub_paths:
            occupied = False
            if pub_active:
                _code_guess = _recognize_code(vf, watch)
                if _code_guess and _norm(_code_guess) in pub_active:
                    occupied = True
            if not occupied and pub_paths and _under_any(vf, pub_paths):
                occupied = True
            if occupied:
                n_publish += 1
                _size_history.pop(fp, None)
                continue
        torrent_task = _match_downloader_torrent(vf, watch_dir, torrent_tasks)
        if torrent_task and not torrent_task.get("completed", False):
            n_waiting += 1
            _size_history.pop(fp, None)
            progress = float(torrent_task.get("progress") or 0.0) * 100
            _log(f"下载器报告任务未完成，跳过：{vf.name}（{progress:.1f}% / "
                 f"状态 {torrent_task.get('state', '未知')}）")
            continue
        # 仍有 qB 未完成分片标记 → 正在下载
        if not torrent_task and _is_incomplete(vf):
            n_incomplete += 1
            _size_history.pop(fp, None)
            continue
        try:
            st = vf.stat()
            size = st.st_size
            age = max(0, now - st.st_mtime)   # 距上次写入的秒数
        except Exception as e:
            _log(f"读取文件信息失败，跳过：{vf.name}（{e}）")
            continue
        # 持久化「已归档」去重：hardlink/copy 模式原文件保留在监控目录，重启后内存 _processed 清空，
        # 靠落盘签名（路径|大小）识别出早已归档的文件并跳过，不再重复刮削/重复归档到新月份目录。
        if _file_sig(vf, size) in _processed_sig:
            _processed.add(fp)        # 回填内存，后续轮次走最快路径
            n_done_before += 1
            _size_history.pop(fp, None)
            continue
        # 兜底去重：签名文件对该文件为空（旧版本归档 / /config 重置 / 归档路径变更）时，
        # 若它已存在于归档目录则补登签名并跳过，避免重启后重新识别/重刮/重复归档（含 -cd 错号）。
        if archive_idx:
            _code_arc = _recognize_code(vf, watch)
            if _is_already_archived(vf, size, _code_arc, archive_idx):
                _processed.add(fp)                          # 内存去重
                _processed_sig[_file_sig(vf, size)] = now   # 补登签名（本轮末统一落盘，避免逐条写）
                n_backfilled += 1
                n_done_before += 1
                _size_history.pop(fp, None)
                continue
        # 广告/赠片清理（一律清理）：该视频自身无番号、无分集标记，且同目录存在「正片兄弟」
        #   （带番号或分集标记的视频）→ 确认是发布目录里的广告/赠片，【直接删除】（含过小小广告）。
        #   主片/分段/其它番号正片绝不删。不论 hardlink/move 归档模式都执行；必须放在「过小忽略」
        #   之前，否则小广告会先被尺寸过滤跳过、永远清不掉（用户反馈的现象）。
        #   注意：若该种子整体仍在做种，删其中文件会让该种子校验缺文件（用户已知并选择一律清理）。
        drop_set = set()
        # 监控根目录可能混放彼此无关的视频，绝不跨影片互相分类/删除；只清理下载子目录。
        in_download_subdir = False
        try:
            in_download_subdir = vf.parent.resolve() != watch_dir.resolve()
        except Exception:
            pass
        if organize_on and delete_extras and in_download_subdir and _has_primary_sibling(vf):
            try:
                _, drop_set = classify_videos(
                    _sibling_videos(vf), watch, min_bytes, keep_bytes)
            except Exception as e:
                _log(f"广告分类失败，保守保留全部文件：{vf.parent.name} — {e}")
        if vf in drop_set:
            try:
                vf.unlink()
                n_extra += 1
                _log(f"已删除广告/赠片视频：{vf.name}（{round(size/1024/1024,1)}MB）")
            except Exception as e:
                _log(f"删除广告/赠片视频失败：{vf.name} — {e}")
                _processed.add(fp)   # 删不掉就别每轮重试刷屏
            _size_history.pop(fp, None)
            continue

        if size < min_bytes:
            n_small += 1
            small_sig = (size, int(getattr(st, "st_mtime_ns", st.st_mtime * 1_000_000_000)))
            if _small_log_history.get(fp) != small_sig:
                _small_log_history[fp] = small_sig
                _log(f"文件过小忽略：{vf.name}（{round(size/1024/1024,1)}MB < {min_bytes//1024//1024}MB）")
            continue
        _small_log_history.pop(fp, None)

        # 同内容不同清晰度/编码不是 CD 分段。只归档确定的最佳版本，其余版本
        # 保留在下载目录，不删除、不改成 -cdN，也不阻塞最佳版本继续处理。
        variant_code = _recognize_code(vf, watch)
        if (variant_code and organize_on
                and _is_nonpreferred_variant(vf, variant_code, watch)):
            _processed.add(fp)
            _size_history.pop(fp, None)
            _log(f"检测到同片不同清晰度/编码，保留非首选版本在原处：{vf.name}")
            continue

        if torrent_task:
            # 属于下载器任务时，只认下载器 API 的 completed/progress/amount_left，
            # 文件大小、mtime、临时后缀都不参与完成判断（兼容预分配和关闭临时后缀）。
            reason = (f"下载器报告完成，状态 {torrent_task.get('state', '未知')}，"
                      f"剩余 {int(torrent_task.get('amount_left') or 0)} 字节")
            _size_history.pop(fp, None)
        else:
            # 只有不属于任何下载器任务的手动文件，才允许静置/大小稳定兜底。
            settled_by_mtime = age >= settle_seconds
            hist = _size_history.get(fp)
            if hist and hist[0] == size:
                hist[1] += 1
            else:
                _size_history[fp] = [size, 1]
            stable_count = _size_history[fp][1]
            settled_by_size = stable_count >= stable_needed
            if not (settled_by_mtime or settled_by_size):
                n_waiting += 1
                _log(f"等待手动文件写入完成：{vf.name}（{int(age)}s 前写入；"
                     f"大小稳定 {stable_count}/{stable_needed}）")
                continue
            reason = (f"手动文件静置 {int(age)}s" if settled_by_mtime
                      else f"手动文件大小稳定 {stable_count}次")
        _log(f"判定下载完成（{reason}），准备处理：{vf.name}（{round(size/1024/1024,1)}MB）")
        _monitor_state["message"] = f"正在刮削 {vf.name}"
        rec = None
        try:
            rec = await _process_completed_file(vf, config)
            _record_recent(rec)
        except Exception as e:
            _log(f"处理文件异常：{vf.name} — {e}")
            _record_recent({"file": vf.name, "scrape_ok": False,
                            "scrape_error": str(e), "moved": False,
                            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        # 进程内始终标记，避免本次运行内重复处理；
        # 仅在「确实归档成功」时才持久化落盘（hardlink/copy 保留原文件→重启后据此跳过）。
        # 归档失败/未开启归档时不落盘：留待重启后重试（避免站点临时不可达被永久跳过；
        # 纯刮削场景由旁边已存在的 NFO/封面天然幂等跳过）。
        _processed.add(fp)
        if rec and rec.get("moved"):
            _mark_processed(vf, size)
        _size_history.pop(fp, None)
        processed += 1

    if n_backfilled:
        _save_processed()   # 本轮兜底补登的签名统一落盘一次（避免逐条全量写文件）
        _log(f"按归档目录补登已归档签名 {n_backfilled} 条（重启/换版后跳过原目录已归档文件）")
    _log(f"扫描完成：共 {n_total} 个视频 → 本次处理 {processed}，"
         f"等待稳定 {n_waiting}，下载中 {n_incomplete}，过小 {n_small}，"
         f"广告/赠片 {n_extra}，发种占用 {n_publish}，先前已处理 {n_done_before}")
    return processed


def _monitor_should_run(config: dict) -> bool:
    """监控是否该运行：刮削、归档任一开启即运行（无单独的监控开关）。
    两者都关＝无事可做＝不监控。监控只负责非发种的下载/手动放入文件，按这两个全局
    开关统一处理（发种任务占用的文件由 active_codes/active_paths 自动跳过）。"""
    scrape_meta = config.get("scrape_meta_enabled", config.get("publish_scrape_enabled", True))
    organize_on = config.get("scrape_organize_enabled", scrape_meta)
    archive_on = config.get("archive_enabled", config.get("publish_archive_enabled", True))
    return bool(scrape_meta or organize_on or archive_on)


async def _monitor_loop():
    _monitor_state["running"] = True
    _log("刮削监控协程已启动")
    while True:
        config = load_config()
        if not _monitor_should_run(config):
            _monitor_state["enabled"] = False
            _monitor_state["message"] = "未启用（刮削、规整、归档都关闭）"
            _monitor_state["running"] = False
            _log("检测到刮削、规整与归档均关闭，监控协程退出")
            return
        _monitor_state["enabled"] = True
        _monitor_state["watch_dir"] = config.get("scrape_watch_dir", "")
        _monitor_state["output_dir"] = config.get("scrape_output_dir", "")
        _monitor_state["scanning"] = True
        try:
            n = await _scan_once(config)
            _monitor_state["last_scan"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _monitor_state["message"] = f"上次扫描处理 {n} 个文件" if n else "空闲中"
        except Exception as e:
            _monitor_state["message"] = f"扫描异常: {e}"
            _log(f"监控扫描异常: {e}")
        finally:
            _monitor_state["scanning"] = False

        interval = max(30, int(config.get("scrape_interval", 300)))
        _log(f"本轮结束，{interval} 秒后再次扫描")
        await asyncio.sleep(interval)


def start_monitor():
    """主程序启动事件中调用；刮削或归档任一开启则拉起监控协程。"""
    global _monitor_task
    config = load_config()
    if not _monitor_should_run(config):
        _log("启动：刮削与归档均关闭，监控未启用")
        _monitor_state["message"] = "未启用（刮削、归档都关闭）"
        return
    if _monitor_task and not _monitor_task.done():
        _log("启动：监控已在运行，跳过")
        return
    _log(f"启动：拉起刮削监控（监控目录 {config.get('scrape_watch_dir') or '未配置'}，"
         f"归档目录 {config.get('scrape_output_dir') or '未配置'}，"
         f"间隔 {config.get('scrape_interval', 300)}s）")
    _monitor_task = asyncio.create_task(_monitor_loop())


def ensure_monitor():
    """配置变更后调用：按最新配置启动或保持监控。"""
    global _monitor_task
    config = load_config()
    if _monitor_should_run(config):
        if not _monitor_task or _monitor_task.done():
            _log(f"配置变更：刮削/归档已开，拉起监控协程（监控目录 {config.get('scrape_watch_dir') or '未配置'}）")
            _monitor_task = asyncio.create_task(_monitor_loop())
        else:
            _log("配置变更：监控已在运行，沿用现有协程（新配置下轮扫描生效）")
    else:
        _log("配置变更：刮削与归档均关闭，监控将停止")
    # 停用时由循环自身检测后退出


# ─────────────────────────────────────────
# 路由
# ─────────────────────────────────────────

@router.get("/scrape/monitor")
async def api_monitor_status():
    """查看后台刮削监控状态"""
    return dict(_monitor_state)


@router.post("/scrape/monitor/refresh")
async def api_monitor_refresh():
    """按当前配置启动/刷新监控（保存设置后调用）"""
    ensure_monitor()
    return {"success": True, "running": bool(_monitor_task and not _monitor_task.done())}


@router.post("/scrape/run-once")
async def api_run_once():
    """立即手动触发一次扫描（不依赖监控开关）"""
    config = load_config()
    if not config.get("scrape_watch_dir"):
        raise HTTPException(status_code=400, detail="未配置监控目录")
    n = await _scan_once(config)
    return {"success": True, "processed": n, "recent": _monitor_state["recent"][:10]}


@router.post("/scan")
async def api_scan_folder(req: ScanRequest):
    """扫描指定目录，返回视频文件列表及刮削状态（手动管理用）"""
    folder = Path(req.folder_path)
    if not folder.exists() or not folder.is_dir():
        raise HTTPException(status_code=404, detail=f"目录不存在: {req.folder_path}")
    files = []
    for f in sorted(folder.iterdir()):
        if not f.is_file() or f.suffix.lower() not in VIDEO_EXTS:
            continue
        files.append({
            "filename": f.name,
            "filepath": str(f),
            "code": _recognize_code(f),
            "size_mb": round(f.stat().st_size / 1024 / 1024, 1),
            **_get_file_status(f),
        })
    return {"folder": req.folder_path, "total": len(files), "files": files}


@router.post("/scrape/single")
async def api_scrape_single(req: ScrapeRequest):
    """手动刮削单个文件，可选移动归档"""
    config = load_config()
    if req.translate_provider:
        config["scrape_translate_provider"] = req.translate_provider
    result = await _scrape_one(req.filepath, req.overwrite, config)
    if not result.get("success"):
        raise HTTPException(status_code=422, detail=result.get("error", "刮削失败"))
    if req.move and config.get("scrape_output_dir"):
        video_path = Path(req.filepath)
        code = result.get("code", "") or await _resolve_code(video_path, config)
        folder_name = _archive_folder_name(
            code, result.get("title_original", ""), result.get("folder_title", ""),
            result.get("actors", []), config)
        mv = _archive_file(
            video_path, config["scrape_output_dir"], code,
            mode=(config.get("archive_mode") or "hardlink").lower(),
            rename=config.get("scrape_organize_enabled", True),
            watch_dir=config.get("scrape_watch_dir", ""),
            folder_name=folder_name,
            by_month=config.get("archive_by_month", True))
        result["moved"] = mv.get("archived", False)
        result["moved_original"] = mv.get("moved_original", False)
        result["target_dir"] = mv.get("target_dir", "")
        if not mv.get("archived"):
            result["archive_error"] = mv.get("error", "归档失败，源文件已保留")
    return result
