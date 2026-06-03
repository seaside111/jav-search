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

# 番号正则：匹配 ABP-123、SSIS001、FC2-PPV-1234567 等格式
_CODE_PATTERNS = [
    re.compile(r'\b(FC2-PPV-\d{5,8})\b', re.IGNORECASE),           # FC2-PPV-1234567
    re.compile(r'\b([A-Z]{2,8}-\d{2,6})\b', re.IGNORECASE),        # ABP-123
    re.compile(r'\b([A-Z]{2,8})[-_]?(\d{2,6})\b', re.IGNORECASE),  # ABP123 / ABP_123
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

def _extract_code(filename: str) -> str:
    stem = Path(filename).stem
    for pat in _CODE_PATTERNS:
        m = pat.search(stem)
        if m:
            if len(m.groups()) == 2:
                return f"{m.group(1).upper()}-{m.group(2)}"
            return m.group(1).upper()
    return ""


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

    code = _extract_code(path.name)
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
    segments, tags = [], []
    if name_part and _has_jp(name_part):
        segments.append(name_part); tags.append("name")
    if desc and _has_jp(desc):
        segments.append(desc); tags.append("desc")

    if segments:
        _log(f"翻译（仅日文部分）：{code}（服务 {provider}，{len(segments)} 段）")
        trans = await translate(text="\n\n".join(segments), provider=provider, config=config)
        if trans.get("success"):
            outs = trans["result"].split("\n\n")
            for i, tg in enumerate(tags):
                val = outs[i].strip() if i < len(outs) else ""
                if tg == "name" and val:
                    name_zh = val
                elif tg == "desc" and val:
                    plot_zh = val
            _log(f"翻译完成：{code} → 片名《{name_zh[:40]}》")
        else:
            _log(f"翻译失败（保留原文）：{code} — {trans.get('error','')}")
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
# 归档移动：视频(重命名为番号) + NFO/封面 → output_dir/YYYYMM/番号/
# ─────────────────────────────────────────

def _archive_file(video_path: Path, output_dir: str, code: str) -> dict:
    """
    把视频重命名为「番号.后缀」，连同以番号命名的 NFO/封面，
    移动到 归档目录/当前年月/番号/ 子目录下（Emby 单片单目录布局）。
    """
    safe_code = _safe_name(code) if code else video_path.stem
    out = Path(output_dir)
    target_dir = out / datetime.now().strftime("%Y%m") / safe_code
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        _log(f"无法创建归档目录 {target_dir}: {e}")
        return {"moved": False, "error": f"无法创建归档目录: {e}"}

    folder = video_path.parent
    moved = []

    # 1) 视频本体 → 番号.后缀
    video_dst = target_dir / f"{safe_code}{video_path.suffix.lower()}"
    if video_dst.exists():
        video_dst = target_dir / f"{safe_code}_{uuid.uuid4().hex[:6]}{video_path.suffix.lower()}"
    try:
        shutil.move(str(video_path), str(video_dst))
        moved.append(video_dst.name)
    except Exception as e:
        _log(f"视频移动失败 {video_path.name}: {e}")
        return {"moved": False, "error": f"视频移动失败: {e}", "target_dir": str(target_dir)}

    # 2) 以番号命名的 NFO/封面（刮削时已按番号生成）
    for extra in (folder / f"{safe_code}.nfo",
                  folder / f"{safe_code}-poster.jpg",
                  folder / f"{safe_code}-fanart.jpg"):
        if extra.exists():
            try:
                shutil.move(str(extra), str(target_dir / extra.name))
                moved.append(extra.name)
            except Exception as e:
                _log(f"附属文件移动失败 {extra.name}: {e}")

    _log(f"归档移动：{len(moved)} 个文件 → {target_dir} （{', '.join(moved)}）")
    return {"moved": bool(moved), "target_dir": str(target_dir), "files": moved}


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
    # 找出该子目录内（递归）剩余的达标视频
    try:
        remaining = [p for p in parent.rglob("*")
                     if p.is_file() and p.suffix.lower() in VIDEO_EXTS
                     and not _is_incomplete(p)
                     and p.stat().st_size >= min_bytes
                     and str(p) not in _processed]
    except Exception:
        remaining = []
    if remaining:
        _log(f"原目录仍有 {len(remaining)} 个待处理视频，暂不删除：{parent.name}")
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


async def _process_completed_file(video_path: Path, config: dict) -> dict:
    """对一个判定为下载完成的视频文件执行：刮削 → 按配置移动归档。"""
    fp = str(video_path)
    output_dir = config.get("scrape_output_dir", "").strip()
    move_on_fail = config.get("scrape_move_on_fail", True)

    try:
        scrape_res = await _scrape_one(fp, overwrite=False, config=config)
    except Exception as e:
        # 刮削过程意外报错也不应阻止移动归档（符合「刮削正常运行但刮不到也移动」）
        _log(f"刮削过程异常（将按失败处理）：{video_path.name} — {e}")
        scrape_res = {"success": False, "filepath": fp, "code": _extract_code(video_path.name),
                      "error": f"刮削异常: {e}"}
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

    # 刮削成功，或失败但配置允许，则移动归档
    if output_dir and (not failed or move_on_fail):
        if failed:
            _log(f"刮削未成功但按配置仍移动归档：{video_path.name}")
        watch_dir = Path(config.get("scrape_watch_dir", ""))
        min_bytes = int(config.get("scrape_min_size_mb", 100)) * 1024 * 1024
        code = scrape_res.get("code", "") or _extract_code(video_path.name)
        src_parent = video_path.parent
        mv = _archive_file(video_path, output_dir, code)
        record["moved"] = mv.get("moved", False)
        record["target_dir"] = mv.get("target_dir", "")
        if mv.get("moved"):
            # 移动成功后清理原下载目录（含遗留广告/样板文件，连同子目录删除）
            _cleanup_source(src_parent, watch_dir, min_bytes)
    elif not output_dir:
        _log(f"未配置归档目录，仅刮削未移动：{video_path.name}")
        record["note"] = "未配置归档目录，仅刮削未移动"
    else:
        _log(f"刮削失败且未开启「失败仍移动」，保留原处：{video_path.name}")

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
    n_total = n_done_before = n_incomplete = n_small = n_waiting = 0

    _log(f"开始扫描监控目录：{watch}（归档目录：{out_dir or '未配置'}）")
    for vf in _iter_video_files(watch_dir):
        n_total += 1
        fp = str(vf)
        if fp in _processed:
            n_done_before += 1
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
         f"等待稳定 {n_waiting}，下载中 {n_incomplete}，过小 {n_small}，先前已处理 {n_done_before}")
    return processed


async def _monitor_loop():
    _monitor_state["running"] = True
    _log("刮削监控协程已启动")
    while True:
        config = load_config()
        if not config.get("scrape_enabled"):
            _monitor_state["enabled"] = False
            _monitor_state["message"] = "监控已停用"
            _monitor_state["running"] = False
            _log("检测到监控开关已关闭，协程退出")
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
    """主程序启动事件中调用；若配置启用则拉起监控协程。"""
    global _monitor_task
    config = load_config()
    if not config.get("scrape_enabled"):
        _log("启动：刮削监控未启用（可在设置中开启）")
        _monitor_state["message"] = "未启用（可在设置中开启）"
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
    if config.get("scrape_enabled"):
        if not _monitor_task or _monitor_task.done():
            _log(f"配置变更：启用监控，拉起协程（监控目录 {config.get('scrape_watch_dir') or '未配置'}）")
            _monitor_task = asyncio.create_task(_monitor_loop())
        else:
            _log("配置变更：监控已在运行，沿用现有协程（新配置下轮扫描生效）")
    else:
        _log("配置变更：监控开关为关闭状态")
    # 停用时由循环自身检测 scrape_enabled 后退出


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
            "code": _extract_code(f.name),
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
