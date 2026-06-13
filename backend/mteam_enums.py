"""
M-Team 枚举智能映射（V1.5 发种类型智能识别）

发种时 createOredit 需要 category/standard/videoCodec/audioCodec 等整数 id。
本模块从 /system/getConf 拉取枚举（id↔中文名）并缓存，再按：
  - mediainfo 解析的分辨率/编码 → standard / videoCodec / audioCodec
  - 番号/来源启发式 → 有码/无码 category
自动匹配出 id，免去手填。结构未文档化，全部走「关键词匹配名称」的防御式实现，
匹配不到就跳过该字段（category 兜底用配置里的 publish_category）。
"""
import json
import re
import time
from typing import Optional

import mteam
import config_manager

_conf_cache = {"ts": 0.0, "data": None}
_CONF_TTL = 1800  # 内存缓存 30 分钟


def _conf_file():
    return config_manager.CONFIG_PATH.parent / "mteam_conf.json"


def _load_file() -> Optional[dict]:
    try:
        p = _conf_file()
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _save_file(data: dict):
    try:
        p = _conf_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


async def refresh_conf(config: dict) -> dict:
    """主动抓取并持久化枚举（保存 API 密钥后/启动时调用）。失败时回退旧缓存/文件。"""
    res = await mteam.get_conf(config)
    if res.get("ok") and res.get("data"):
        _conf_cache.update({"ts": time.time(), "data": res["data"]})
        _save_file(res["data"])
        return res["data"]
    return _conf_cache["data"] or _load_file() or {}


async def load_conf(config: dict, force: bool = False) -> dict:
    now = time.time()
    if not force and _conf_cache["data"] and (now - _conf_cache["ts"] < _CONF_TTL):
        return _conf_cache["data"]
    # 优先用持久化文件（秒回、免网络）；无文件才抓取
    if not force:
        fc = _load_file()
        if fc:
            _conf_cache.update({"ts": now, "data": fc})
            return fc
    return await refresh_conf(config)


_NAME_KEYS = ("nameChs", "cname", "nameChi", "name", "label", "title")


def _norm_list(raw) -> list:
    """把任意形态的枚举值规整成 [{id, name}]。"""
    out = []
    if isinstance(raw, dict):
        # 可能是 {id: name} 映射，或 {list:[...]}
        if isinstance(raw.get("list"), list):
            raw = raw["list"]
        else:
            for k, v in raw.items():
                if isinstance(v, str):
                    out.append({"id": str(k), "name": v})
            return out
    if isinstance(raw, list):
        for it in raw:
            if not isinstance(it, dict):
                continue
            iid = it.get("id", it.get("value"))
            name = next((it[k] for k in _NAME_KEYS if it.get(k)), "")
            if iid is not None:
                out.append({"id": str(iid), "name": str(name)})
    return out


def _get_enum(conf: dict, *key_candidates) -> list:
    if not isinstance(conf, dict):
        return []
    for k in key_candidates:
        if k in conf and conf[k]:
            lst = _norm_list(conf[k])
            if lst:
                return lst
    return []


def _match(items: list, keywords: list) -> str:
    """返回首个名称包含任一关键词的 id；无则空串。"""
    for it in items:
        name = (it.get("name") or "").lower()
        for kw in keywords:
            if kw.lower() in name:
                return it.get("id", "")
    return ""


def _standard_keywords(height: int) -> list:
    if height >= 2000:
        return ["4k", "2160", "uhd"]
    if height >= 1000:
        return ["1080"]
    if height >= 700:
        return ["720"]
    if height > 0:
        return ["sd", "480", "标清", "標清"]
    return []


def _vcodec_keywords(codec: str) -> list:
    c = (codec or "").upper()
    if "265" in c or "HEVC" in c:
        return ["h265", "hevc", "x265"]
    if "264" in c or "AVC" in c:
        return ["h264", "avc", "x264"]
    if "AV1" in c:
        return ["av1"]
    if "VP9" in c:
        return ["vp9"]
    return []


def _acodec_keywords(codec: str) -> list:
    c = (codec or "").upper()
    table = {
        "AAC": ["aac"], "AC-3": ["ac3", "ac-3"], "AC3": ["ac3", "ac-3"],
        "EAC3": ["eac3", "e-ac-3", "ddp"], "FLAC": ["flac"], "DTS": ["dts"],
        "PCM": ["pcm", "lpcm"], "OPUS": ["opus"], "MP3": ["mp3"], "VORBIS": ["vorbis"],
    }
    for key, kws in table.items():
        if key in c:
            return kws
    return []


# 无码厂牌/番号前缀（命中即判无码）
_UNCENSORED_PREFIXES = (
    "FC2", "HEYZO", "CARIB", "1PONDO", "10MUSUME", "PACOPACOMAMA", "PACO",
    "MURAMURA", "GACHINCO", "KIN8", "TOKYO-HOT", "HEYDOUGA", "0NDO", "1PON",
)


def is_uncensored(code: str, source: str) -> bool:
    src = (source or "").lower()
    if src in ("avsox", "avmoo"):
        return True
    c = (code or "").upper().replace("_", "-")
    if any(c.startswith(p) or p in c for p in _UNCENSORED_PREFIXES):
        return True
    # 日期型无码番号：060226-001 / 060226_01
    if re.match(r"^\d{6}[-_]\d{2,4}$", c):
        return True
    return False


def _match_category(cats: list, uncensored: bool, height: int) -> tuple:
    """
    按「有码/无码 + HD/SD」匹配分类 id。返回 (id, 描述)。
    先精确匹配审查×清晰度（如 有码HD=410）；匹配不到退回只按审查取第一个。
    DVDiSo/Blu-Ray 不自动判（默认 HD/SD），需要时手填覆盖。
    """
    censor = ["无码", "無碼", "無修正"] if uncensored else ["有码", "有碼"]
    quality = "sd" if (0 < height < 700) else "hd"
    label = ("无码" if uncensored else "有码") + ("/SD" if quality == "sd" else "/HD")
    for c in cats:
        n = (c.get("name") or "")
        if any(z in n for z in censor) and quality in n.lower():
            return c.get("id", ""), label
    for c in cats:
        n = (c.get("name") or "")
        if any(z in n for z in censor):
            return c.get("id", ""), ("无码" if uncensored else "有码")
    return "", ""


async def smart_fields(config: dict, summary: dict, code: str, source: str) -> dict:
    """
    返回可自动填充的字段 id 字典（只含匹配到的）：
      {category, standard, videoCodec, audioCodec} + 调试用 _detected。
    """
    conf = await load_conf(config)
    summary = summary or {}
    fields = {}
    detected = {}

    cats = _get_enum(conf, "category", "categories", "categoryList")
    std = _get_enum(conf, "standard", "standards", "standardList")
    vc = _get_enum(conf, "videoCodec", "videoCodecs", "videoCodecList")
    ac = _get_enum(conf, "audioCodec", "audioCodecs", "audioCodecList")
    countries = _get_enum(conf, "country", "countries", "countryList")

    h = int(summary.get("height") or 0)

    # category：有码/无码 + HD/SD 精确匹配（如 410 有码HD / 429 无码HD / 424 有码SD / 430 无码SD）
    uncensored = is_uncensored(code, source)
    cid, cdesc = _match_category(cats, uncensored, h)
    detected["censorship"] = cdesc or ("无码" if uncensored else "有码")
    if cid:
        fields["category"] = cid

    # country：JAV 自动选「日本」
    country_id = _match(countries, ["日本", "Japan", "JP", "JAP"])
    detected["country"] = "日本" if country_id else ""
    if country_id:
        fields["countries"] = country_id

    # standard：清晰度
    h = int(summary.get("height") or 0)
    sk = _standard_keywords(h)
    detected["standard"] = sk[0] if sk else ""
    sid = _match(std, sk) if sk else ""
    if sid:
        fields["standard"] = sid

    # videoCodec
    vk = _vcodec_keywords(summary.get("video_codec", ""))
    detected["videoCodec"] = vk[0] if vk else ""
    vid = _match(vc, vk) if vk else ""
    if vid:
        fields["videoCodec"] = vid

    # audioCodec
    ak = _acodec_keywords(summary.get("audio_codec", ""))
    detected["audioCodec"] = ak[0] if ak else ""
    aid = _match(ac, ak) if ak else ""
    if aid:
        fields["audioCodec"] = aid

    fields["_detected"] = detected
    return fields
