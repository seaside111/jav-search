"""
推送入库辅助（V1.4.5）。

解决两件事，都围绕「经『推送』加入下载器的磁力链种子」：

1) 刮削直接用「列表里已呈现的内容」（番号/标题/封面/演员/标签/简介…）：
   推送时把该影片的展示元数据按下载器实际入库的 infohash 记下来；下载完成后刮削时
   直接拿来写 NFO/封面，不再从文件名重新识别番号 + 重新刮削。
   —— 尤其修复纯数字番号（如 AVSOX「061326_01」）在文件名识别阶段出错、刮错封面/NFO 的问题。

2) 磁力链「下完即删种」（保留文件）：开关开启时推送的磁力，记 _autodel 标记；
   后台轮询到它下载完成后删除该种子（仅删种、保留已下载文件），因为这里的磁力只用于下载、不做种。

存储持久化到 CONFIG_DIR/pushed_intake.json，键为 infohash，容器重启不丢。
为让「下完即删种」后刮削仍能匹配到元数据，轮询会在删种前把下载内容的目录/文件名(_name)记进条目，
刮削时即可按名字匹配（跨容器挂载名一致），不依赖种子是否还在下载器里。
"""
import json
import os
import time
import asyncio
from pathlib import Path
from typing import Optional

_FILE = Path(os.getenv("CONFIG_DIR", "/config")) / "pushed_intake.json"

# 随影片一起记下、供刮削写 NFO/封面用的字段（与 scrapers 列表/详情字段对齐）。
# detail_loaded 表示推送时详情是否已加载完整——刮削时据此决定要不要回原源补抓缺失字段。
_META_FIELDS = ("code", "title", "cover", "cover_thumb", "source", "url",
                "release_date", "duration", "director", "studio", "label",
                "series", "score", "actors", "tags", "description", "detail_loaded")

_MAX_ENTRIES = 800
_TTL_SECONDS = 30 * 24 * 3600   # 条目最长保留 30 天，防无限增长


def _load() -> dict:
    try:
        return json.loads(_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict) -> None:
    try:
        _FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_FILE)
    except Exception as e:
        print(f"[intake] 写入失败: {e}", flush=True)


def _prune(data: dict) -> dict:
    now = int(time.time())
    # 过期清理
    for k in [k for k, v in data.items() if now - int(v.get("_ts", now)) > _TTL_SECONDS]:
        data.pop(k, None)
    # 总量上限：超出按时间裁掉最旧
    if len(data) > _MAX_ENTRIES:
        for k in sorted(data, key=lambda k: data[k].get("_ts", 0))[:len(data) - _MAX_ENTRIES]:
            data.pop(k, None)
    return data


# ── 推送时调用：记下这条种子的展示元数据 + 是否下完即删 ──
def register(infohash: str, meta: Optional[dict], autodelete: bool) -> None:
    ih = (infohash or "").strip().lower()
    if not ih:
        return
    entry = {k: meta.get(k) for k in _META_FIELDS
             if isinstance(meta, dict) and meta.get(k) not in (None, "", [], {})}
    entry["_ts"] = int(time.time())
    entry["_autodel"] = bool(autodelete)
    entry["_done"] = False
    entry["_deleted"] = False
    data = _load()
    data[ih] = entry
    _save(_prune(data))


def forget(infohash: str) -> None:
    ih = (infohash or "").strip().lower()
    if not ih:
        return
    data = _load()
    if ih in data:
        data.pop(ih, None)
        _save(data)


def _name_candidates(video_path: Path, watch_dir: str) -> set:
    """该视频文件可用于匹配的「名字」集合：文件名(去扩展)+相对监控目录的各级目录名。"""
    names = set()
    try:
        names.add(video_path.stem.lower())
        names.add(video_path.name.lower())
    except Exception:
        pass
    parts = []
    try:
        if watch_dir:
            rel = video_path.resolve().relative_to(Path(watch_dir).resolve())
            parts = list(rel.parts)
    except Exception:
        parts = list(video_path.parts)
    for p in parts:
        names.add(p.lower())
        names.add(Path(p).stem.lower())
    return {n for n in names if n}


def _entry_names(entry: dict) -> set:
    out = set()
    for key in ("_name", "_tname"):
        v = (entry.get(key) or "").strip().lower()
        if v:
            out.add(v)
            out.add(Path(v).stem.lower())
    return out


async def resolve_for_file(video_path: Path, watch_dir: str,
                           config: dict) -> tuple[Optional[str], Optional[dict]]:
    """为待刮削的视频文件找回推送时记下的元数据。
    先按已记录的下载内容名(_name)匹配（即便种子已被删也能命中）；
    未命中再实时查下载器：用种子名/内容路径与文件路径比对拿到 infohash 反查。
    返回 (infohash, meta) 或 (None, None)。"""
    data = _load()
    if not data:
        return None, None
    cand = _name_candidates(video_path, watch_dir)

    # ① 按已记录的下载内容名匹配（不依赖种子是否还在）
    for ih, e in data.items():
        if e.get("code") and (_entry_names(e) & cand):
            return ih, e

    # ② 实时查下载器：按 infohash 直配（种子仍在时）
    try:
        import downloader
        torrents = await downloader.list_torrents(config)
    except Exception:
        torrents = []
    for t in torrents:
        ih = (t.get("hash") or "").lower()
        e = data.get(ih)
        if not e or not e.get("code"):
            continue
        tnames = set()
        for v in (t.get("name", ""), os.path.basename(t.get("content_path", "") or "")):
            v = (v or "").strip().lower()
            if v:
                tnames.add(v)
                tnames.add(Path(v).stem.lower())
        if tnames & cand:
            return ih, e
    return None, None


async def poll(config: dict) -> None:
    """后台轮询一次：
    - 给在库种子补记下载内容名(_name/_tname)，供刮削按名匹配；
    - 对标记了「下完即删」且已完成的磁力种子，删除其种子记录（保留文件）。
    下载器不可达（列表为空）时跳过本轮，避免误判。"""
    data = _load()
    if not data:
        return
    try:
        import downloader
        torrents = await downloader.list_torrents(config)
    except Exception:
        return
    if not torrents:
        return
    by_hash = {(t.get("hash") or "").lower(): t for t in torrents}
    autodel_on = bool(config.get("magnet_delete_completed", False))
    changed = False
    to_delete = []

    for ih, e in list(data.items()):
        t = by_hash.get(ih)
        if t is None:
            continue  # 不在下载器（可能已被删/还没注册）——名字已记过的条目留给刮削按名匹配
        # 补记下载内容名，供刮削/删种后按名匹配
        nm = os.path.basename(t.get("content_path", "") or "") or (t.get("name", "") or "")
        if nm and e.get("_name") != nm:
            e["_name"] = nm
            e["_tname"] = t.get("name", "") or nm
            changed = True
        done = float(t.get("progress") or 0) >= 0.999
        if done and not e.get("_done"):
            e["_done"] = True
            changed = True
        # 下完即删（保留文件）：尊重当前开关 + 条目标记
        if done and e.get("_autodel") and not e.get("_deleted") and autodel_on:
            to_delete.append(ih)

    if to_delete:
        try:
            import downloader
            res = await downloader.delete_torrents(config, to_delete, delete_files=False)
            if res.get("success"):
                for ih in to_delete:
                    data[ih]["_deleted"] = True
                changed = True
                print(f"[磁力删种] 已删除 {len(to_delete)} 个下完的磁力种子（保留文件）", flush=True)
            else:
                print(f"[磁力删种] 删除失败：{res.get('error', '')}", flush=True)
        except Exception as e:
            print(f"[磁力删种] 删除异常：{e}", flush=True)

    if changed:
        _save(_prune(data))


_poller_started = False


def start_poller(load_config, interval: int = 60) -> None:
    """启动后台轮询任务（幂等）。load_config 为读取配置的可调用对象。"""
    global _poller_started
    if _poller_started:
        return
    _poller_started = True

    async def _runner():
        while True:
            await asyncio.sleep(interval)
            try:
                await poll(load_config())
            except Exception as e:
                print(f"[intake] 轮询异常：{e}", flush=True)

    try:
        asyncio.get_event_loop().create_task(_runner())
        print("[intake] 推送入库轮询已启动（磁力下完即删 / 刮削元数据补名）", flush=True)
    except Exception as e:
        print(f"[intake] 轮询启动失败：{e}", flush=True)
