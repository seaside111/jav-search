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
import os
import re
import shutil
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Optional
from xml.dom import minidom

import httpx
from fastapi import APIRouter, HTTPException
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


# 番号正则：匹配 ABP-123、SSIS001、FC2-PPV-1234567、390JAC-234，以及无码格式
# （10musume/1pondo/Carib 060226_01、heydouga-4017-001）等。
# 顺序很重要：更「专」「长」的格式排在前，避免被宽松规则截断（如 heydouga 不被截成 HEYDOUGA-4017）。
_CODE_PATTERNS = [
    re.compile(r'\b(FC2-?PPV-?\d{5,8})\b', re.IGNORECASE),               # FC2-PPV-1234567
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
# 已处理（移动走或失败记录过）的文件路径，避免重复处理
_processed: set[str] = set()

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
    cleaned = _clean_noise(name)
    c = _match_code(cleaned)
    if c:
        return c
    # 回退：不去方括号（番号可能在 []内），但仍去广告域名
    return _match_code(_SITE_NOISE.sub(' ', name))


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
    mains.sort(key=lambda p: p.name.lower())
    return mains


def _part_suffix(video_path: Path, code: str, watch_dir: str = "") -> str:
    """同番号在同目录有多个正片（分段）时，返回该视频的分段后缀「-cd{N}」
    （Emby/Kodi 多文件堆叠为同一影片）；单文件返回 ""。
    N 取该视频在「同番号正片按文件名排序」中的位次，与处理顺序无关、稳定不冲突。"""
    mains = _same_code_main_videos(video_path, code, watch_dir)
    if len(mains) <= 1:
        return ""
    idx = next((i for i, p in enumerate(mains) if p.name == video_path.name), 0)
    return f"-cd{idx + 1}"


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


def _build_nfo(movie: dict, title_zh: str, plot_zh: str) -> str:
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
        if avatar:
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


def _get_file_status(video_path: Path, code: str = "") -> dict:
    """检查视频旁是否已有以 番号 命名的 NFO/封面。"""
    folder = video_path.parent
    stem = code or video_path.stem
    nfo_path = folder / f"{stem}.nfo"
    poster_path = folder / f"{stem}-poster.jpg"
    fanart_path = folder / f"{stem}-fanart.jpg"
    return {
        "has_nfo": nfo_path.exists(),
        "has_cover": poster_path.exists() or fanart_path.exists(),
    }


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
    if not overwrite and status["has_nfo"] and status["has_cover"]:
        _log(f"已存在 NFO 和封面，跳过刮削：{code}")
        return {"success": True, "skipped": True, "filepath": filepath, "code": code,
                "reason": "NFO 和封面已存在"}

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
            nfo_file.write_text(_build_nfo(movie, title_for_nfo, plot_zh), encoding="utf-8")
            saved_nfo = True
            _log(f"已写入 NFO：{nfo_file.name}")
        except Exception as e:
            _log(f"NFO 写入失败 {filepath}: {e}")

    cover_url = movie.get("cover", "")
    if cover_url and (overwrite or not status["has_cover"]):
        _log(f"下载封面：{code} ← {cover_url[:60]}")
        img = await _download_image(cover_url, proxy, _cover_referer(cover_url))
        if img:
            try:
                (folder / f"{code}-poster.jpg").write_bytes(img)
                (folder / f"{code}-fanart.jpg").write_bytes(img)
                saved_cover = True
                _log(f"已写入封面：{code}-poster.jpg / -fanart.jpg")
            except Exception as e:
                _log(f"封面保存失败 {filepath}: {e}")
        else:
            _log(f"封面下载失败：{code}")

    _log(f"刮削结束：{code}（NFO={'有' if saved_nfo else '无'} 封面={'有' if saved_cover else '无'}）")
    return {"success": True, "skipped": False, "filepath": filepath, "code": code,
            "title_zh": title_for_nfo, "saved_nfo": saved_nfo, "saved_cover": saved_cover}


# ─────────────────────────────────────────
# 归档：视频(重命名为番号) + NFO/封面 → output_dir/YYYYMM/番号/
#   V1.5 统一：归档方式由全局 archive_mode 决定（hardlink/copy 保留原文件；move 移动后清原目录），
#   与发种流水线共用同一归档目录与按年月结构。
# ─────────────────────────────────────────

def _transfer(src: Path, dst: Path, mode: str) -> bool:
    """按归档模式把 src 落到 dst：move=移动；hardlink=硬链接(跨卷自动退化为复制)；copy=复制。"""
    try:
        if dst.exists():
            dst.unlink()
    except Exception:
        pass
    try:
        if mode == "move":
            shutil.move(str(src), str(dst))
        elif mode == "copy":
            shutil.copy2(str(src), str(dst))
        else:  # hardlink（默认）
            try:
                os.link(str(src), str(dst))
            except OSError:
                shutil.copy2(str(src), str(dst))   # 跨卷无法硬链 → 复制
        return True
    except Exception as e:
        _log(f"归档落地失败 {src.name}（{mode}）: {e}")
        return False


def _archive_file(video_path: Path, output_dir: str, code: str,
                  mode: str = "hardlink", rename: bool = True,
                  watch_dir: str = "") -> dict:
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
    target_dir = out / datetime.now().strftime("%Y%m") / safe_code
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        _log(f"无法创建归档目录 {target_dir}: {e}")
        return {"archived": False, "moved_original": False, "error": f"无法创建归档目录: {e}"}

    folder = video_path.parent
    done = []

    # 1) 视频本体 → 番号[-cdN].后缀（刮削关则保留原文件名）
    #    多分段时加 -cd1/-cd2… 堆叠后缀，确保 A/B/C、1/2/3、CD1/CD2 等全部归档不互相覆盖。
    part = _part_suffix(video_path, code, watch_dir) if (rename and code) else ""
    video_name = f"{safe_code}{part}{video_path.suffix.lower()}" if (rename and code) else video_path.name
    video_dst = target_dir / video_name
    if not _transfer(video_path, video_dst, mode):
        return {"archived": False, "moved_original": False,
                "error": "视频归档失败", "target_dir": str(target_dir)}
    done.append(video_dst.name)

    # 2) 以番号命名的 NFO/封面（刮削时已按番号生成）
    for extra in (folder / f"{safe_code}.nfo",
                  folder / f"{safe_code}-poster.jpg",
                  folder / f"{safe_code}-fanart.jpg"):
        if extra.exists():
            # NFO/封面体积小：move 模式随视频一起移走；hardlink/copy 模式一律复制一份（不动原件）
            sub_mode = "move" if mode == "move" else "copy"
            if _transfer(extra, target_dir / extra.name, sub_mode):
                done.append(extra.name)

    how = {"move": "移动", "copy": "复制", "hardlink": "硬链接"}.get(mode, mode)
    _log(f"归档（{how}）：{len(done)} 个文件 → {target_dir} （{', '.join(done)}）")
    return {"archived": bool(done), "moved_original": (mode == "move"),
            "target_dir": str(target_dir), "files": done}


def _cleanup_source(video_parent: Path, watch_dir: Path, min_bytes: int):
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
    # 找出该子目录内（递归）剩余的、仍待处理的「正片」视频。
    # 广告/赠片（_looks_primary 为假）不计入，否则真片归档后会被它们长期挡住、
    # 导致原目录连同广告一直残留。
    watch_str = str(watch_dir)
    try:
        remaining = [p for p in parent.rglob("*")
                     if p.is_file() and p.suffix.lower() in VIDEO_EXTS
                     and not _is_incomplete(p)
                     and p.stat().st_size >= min_bytes
                     and str(p) not in _processed
                     and _looks_primary(p, watch_str)]
    except Exception:
        remaining = []
    if remaining:
        _log(f"原目录仍有 {len(remaining)} 个待处理正片，暂不删除：{parent.name}")
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


def _iter_video_files(watch_dir: Path):
    try:
        for p in watch_dir.rglob("*"):
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
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


async def _process_completed_file(video_path: Path, config: dict) -> dict:
    """对一个判定为下载完成的视频文件执行：刮削(可关) → 按配置归档(可关)。"""
    fp = str(video_path)
    output_dir = config.get("scrape_output_dir", "").strip()
    move_on_fail = config.get("scrape_move_on_fail", True)
    # 全局刮削/归档总开关（监控 & 发种共用）；兼容旧 publish_* 键
    scrape_meta = config.get("scrape_meta_enabled", config.get("publish_scrape_enabled", True))
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
        code = scrape_res.get("code", "") or await _resolve_code(video_path, config)
        src_parent = video_path.parent
        # V1.5 统一：归档方式取全局 archive_mode（默认 hardlink 保留原文件；move 才移走+清原目录）
        mode = (config.get("archive_mode") or "hardlink").lower()
        mv = _archive_file(video_path, output_dir, code, mode=mode, rename=scrape_meta,
                           watch_dir=str(watch_dir))
        record["moved"] = mv.get("archived", False)
        record["archive_mode"] = mode
        record["target_dir"] = mv.get("target_dir", "")
        if mv.get("moved_original"):
            # 仅 move 模式：原文件已移走，清理原下载目录（含遗留广告/样板文件，连同子目录删除）。
            # hardlink/copy 模式保留原文件（可继续做种/辅种），绝不删原目录。
            _cleanup_source(src_parent, watch_dir, min_bytes)
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

    stable_needed = int(config.get("scrape_stable_checks", 2))
    settle_seconds = int(config.get("scrape_settle_seconds", 60))
    min_bytes = int(config.get("scrape_min_size_mb", 100)) * 1024 * 1024
    out_dir = config.get("scrape_output_dir", "").strip()
    now = time.time()
    processed = 0
    n_total = n_done_before = n_incomplete = n_small = n_waiting = n_extra = n_publish = 0

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

    _log(f"开始扫描监控目录：{watch}（归档目录：{out_dir or '未配置'}）")
    for vf in _iter_video_files(watch_dir):
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
        # 仍有 qB 未完成分片标记 → 正在下载
        if _is_incomplete(vf):
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
        # 广告/赠片清理（一律清理）：该视频自身无番号、无分集标记，且同目录存在「正片兄弟」
        #   （带番号或分集标记的视频）→ 确认是发布目录里的广告/赠片，【直接删除】（含过小小广告）。
        #   主片/分段/其它番号正片绝不删。不论 hardlink/move 归档模式都执行；必须放在「过小忽略」
        #   之前，否则小广告会先被尺寸过滤跳过、永远清不掉（用户反馈的现象）。
        #   注意：若该种子整体仍在做种，删其中文件会让该种子校验缺文件（用户已知并选择一律清理）。
        if (not _code_from_name(vf.stem) and not _has_cd_marker(vf.stem)
                and _has_primary_sibling(vf)
                and (_is_extra_video(vf, watch) or size < min_bytes)):
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
            _log(f"文件过小忽略：{vf.name}（{round(size/1024/1024,1)}MB < {min_bytes//1024//1024}MB）")
            continue

        # 完成判定（无 .!qB 前提下，满足任一即视为下载完成）：
        #   a) 静置：mtime 已超过 settle_seconds 不再写入 → 立即处理（最快，且适配手动放入的文件）
        #   b) 兜底：大小连续 stable_needed 次扫描不变（应对 mtime 不可靠的网络存储）
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
            _log(f"等待下载完成：{vf.name}（{int(age)}s 前写入 < {settle_seconds}s；"
                 f"大小稳定 {stable_count}/{stable_needed}）")
            continue

        reason = f"静置 {int(age)}s" if settled_by_mtime else f"大小稳定 {stable_count}次"
        _log(f"判定下载完成（{reason}），准备处理：{vf.name}（{round(size/1024/1024,1)}MB）")
        _monitor_state["message"] = f"正在刮削 {vf.name}"
        try:
            rec = await _process_completed_file(vf, config)
            _record_recent(rec)
        except Exception as e:
            _log(f"处理文件异常：{vf.name} — {e}")
            _record_recent({"file": vf.name, "scrape_ok": False,
                            "scrape_error": str(e), "moved": False,
                            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        _processed.add(fp)
        _size_history.pop(fp, None)
        processed += 1

    _log(f"扫描完成：共 {n_total} 个视频 → 本次处理 {processed}，"
         f"等待稳定 {n_waiting}，下载中 {n_incomplete}，过小 {n_small}，"
         f"广告/赠片 {n_extra}，发种占用 {n_publish}，先前已处理 {n_done_before}")
    return processed


def _monitor_should_run(config: dict) -> bool:
    """监控是否该运行：刮削、归档任一开启即运行（无单独的监控开关）。
    两者都关＝无事可做＝不监控。监控只负责非发种的下载/手动放入文件，按这两个全局
    开关统一处理（发种任务占用的文件由 active_codes/active_paths 自动跳过）。"""
    scrape_meta = config.get("scrape_meta_enabled", config.get("publish_scrape_enabled", True))
    archive_on = config.get("archive_enabled", config.get("publish_archive_enabled", True))
    return bool(scrape_meta or archive_on)


async def _monitor_loop():
    _monitor_state["running"] = True
    _log("刮削监控协程已启动")
    while True:
        config = load_config()
        if not _monitor_should_run(config):
            _monitor_state["enabled"] = False
            _monitor_state["message"] = "未启用（刮削、归档都关闭）"
            _monitor_state["running"] = False
            _log("检测到刮削与归档均关闭，监控协程退出")
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
        mv = _archive_file(Path(req.filepath), config["scrape_output_dir"])
        result["moved"] = mv.get("moved", False)
        result["target_dir"] = mv.get("target_dir", "")
    return result
