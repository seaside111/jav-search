"""
推送番号标记（V1.4 修复）

问题：刮削时仅靠文件名用「字母+数字」正则猜番号，遇到带站点前缀/数字前缀的
文件名（如 hhd800.com@390JAC-234.mp4）会误判为 HHD-800。

思路：用户在前端搜索结果里点「推送下载」时，那条影片的番号是已知且准确的
（就是搜索结果卡片上的番号）。推送时把「番号 ↔ 该磁力/种子的可识别特征」
持久化到 config/push_hints.json；下载完成后刮削时，先用文件名（及其上级目录名）
去比对这些特征，命中则直接采用准确番号，未命中再回退到文件名正则。

匹配特征（鲁棒、无需访问 qB API）：
  - 番号本身归一化串（如 390jac234）——下载文件名通常包含番号
  - 磁力链 dn（显示名）/ 资源标题归一化串——qB 落地目录/文件名常等于 dn
"""
import json
import os
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

_STORE_PATH = Path(os.getenv("CONFIG_DIR", "/config")) / "push_hints.json"

# 保留的最大条目数 / 过期天数（避免无限增长）
_MAX_ENTRIES = 500
_MAX_AGE_DAYS = 90


def _norm(s: str) -> str:
    """归一化：转小写、仅保留字母数字（去掉分隔符/符号/空格）。"""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _magnet_dn(url: str) -> str:
    """从磁力链解析显示名 dn。"""
    if not url or not url.lower().startswith("magnet:"):
        return ""
    try:
        qs = parse_qs(urlparse(url).query)
        dn = qs.get("dn", [""])[0]
        return unquote(dn)
    except Exception:
        return ""


def _magnet_hash(url: str) -> str:
    """从磁力链解析 btih 信息哈希（小写），便于去重。"""
    if not url:
        return ""
    m = re.search(r"btih:([0-9a-zA-Z]{32,40})", url, re.IGNORECASE)
    return m.group(1).lower() if m else ""


def _load() -> list:
    try:
        if _STORE_PATH.exists():
            with open(_STORE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception as e:
        print(f"[PushHints] load error: {e}", flush=True)
    return []


def _save(entries: list) -> bool:
    try:
        _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_STORE_PATH, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"[PushHints] save error: {e}", flush=True)
        return False


def _prune(entries: list) -> list:
    """按时间倒序保留，丢弃过期/超量条目。"""
    cutoff = time.time() - _MAX_AGE_DAYS * 86400
    entries = [e for e in entries if float(e.get("ts", 0)) >= cutoff]
    entries.sort(key=lambda e: float(e.get("ts", 0)), reverse=True)
    return entries[:_MAX_ENTRIES]


def record(code: str, download_url: str, title: str = "") -> bool:
    """
    记录一次推送的「番号 ↔ 资源特征」。在推送成功后调用。
    code 为空则不记录（无可信番号可标记）。
    """
    code = (code or "").strip()
    if not code:
        return False
    code_norm = _norm(code)
    if len(code_norm) < 4:        # 番号太短，匹配风险大，放弃
        return False

    dn = _magnet_dn(download_url)
    info_hash = _magnet_hash(download_url)
    entry = {
        "code": code,
        "code_norm": code_norm,
        "dn_norm": _norm(dn),
        "title_norm": _norm(title),
        "hash": info_hash,
        "ts": time.time(),
    }

    entries = _load()
    # 去重策略：
    #   - 有 infohash（磁力）：仅移除「完全相同 infohash」的旧记录，
    #     保留同一番号的不同磁力（用户可能为一部影片推送多个版本，
    #     按 infohash 反查时每个都要能命中）。
    #   - 无 infohash（.torrent 直链）：移除同番号且同样无 hash 的旧记录。
    def _dup(e):
        if info_hash:
            return (e.get("hash") or "") == info_hash
        return e.get("code_norm") == code_norm and not e.get("hash")
    entries = [e for e in entries if not _dup(e)]
    entries.insert(0, entry)
    return _save(_prune(entries))


def resolve_by_hash(info_hash: str) -> str:
    """
    按磁力 infohash 精确反查推送时记录的番号（最可靠，不依赖文件名）。
    命中返回番号，否则返回 ""。
    """
    h = (info_hash or "").lower().strip()
    if not h:
        return ""
    for e in _load():
        eh = (e.get("hash") or "").lower()
        if eh and eh == h:
            return e.get("code", "")
    return ""


def _candidate_keys(video_path: Path, watch_dir: str) -> list:
    """收集文件可用于匹配的归一化名：文件名(去扩展) + 各级父目录名（截至监控根）。"""
    names = [video_path.stem]
    try:
        watch = Path(watch_dir).resolve() if watch_dir else None
    except Exception:
        watch = None
    parent = video_path.parent
    for _ in range(6):  # 最多上溯 6 级，足够覆盖 qB 单种子单目录
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
    return [_norm(n) for n in names if n]


def resolve(video_path: Path, watch_dir: str = "") -> str:
    """
    根据已推送的标记反查准确番号。命中返回番号，否则返回 ""。
    匹配优先级：番号归一化串被文件/目录名包含（最可信） > dn/标题串互相包含。
    """
    entries = _load()
    if not entries:
        return ""
    keys = _candidate_keys(video_path, watch_dir)
    if not keys:
        return ""

    # 第一轮：番号串作为子串命中（最精确）。取命中番号串最长者（最具体）。
    best_code, best_len = "", 0
    for e in entries:
        cn = e.get("code_norm", "")
        if cn and len(cn) >= 4:
            for k in keys:
                if cn in k and len(cn) > best_len:
                    best_code, best_len = e.get("code", ""), len(cn)
    if best_code:
        return best_code

    # 第二轮：dn / 标题归一化串与候选名互相包含（兜底，要求足够长以降低误判）。
    for e in entries:
        for field in ("dn_norm", "title_norm"):
            v = e.get(field, "")
            if v and len(v) >= 8:
                for k in keys:
                    if len(k) >= 8 and (v in k or k in v):
                        return e.get("code", "")
    return ""
