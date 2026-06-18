"""
DMM 链接解析（V1.5 发种：自动填 dmmCode 框）

复刻 M-Team 上传页 DMM 框的联想流程：对【有码·字母+数字番号】（javbus/javdb 等来源），
调站点自带的 DMM 联想接口取候选，按番号精确匹配选中，得到要填进 dmmCode 框的值。

只处理有码字母+数字番号：FC2 / 无码厂牌 / 日期型番号 / avsox·avmoo 来源一律跳过
（沿用 mteam_enums.is_uncensored 判定，口径与发种分类一致）。

字段名站点未文档化，mteam.dmm_search 已防御式解析；这里只做「是否该查 + 选哪条」。
"""
import re
from typing import Optional

import mteam
import mteam_enums


def is_target_code(code: str, source: str = "") -> bool:
    """是否为应查 DMM 的番号。

    规则（V1.5.0-beta30 改）：判定为【无码】的一律跳过，判定为【有码】的一律查 DMM
    （口径与发种分类同源 mteam_enums.is_uncensored）。不再用「字母+数字」严格正则预筛——
    凡有码就查一下，真查不到候选才留空（由 resolve 返回 ok=False，发种端非阻断地留空）。"""
    c = (code or "").strip().replace("_", "-")
    if not c:
        return False
    # FC2 / 无码厂牌 / 日期型 / avsox·avmoo 来源 → 无码，跳过
    if mteam_enums.is_uncensored(c, source):
        return False
    return True


def _split_code(code: str):
    """拆成 (字母小写, 数字int)。失败返回 ('', None)。"""
    m = re.match(r"^([A-Za-z]+)-?(\d+)$", (code or "").strip().replace("_", "-"))
    if not m:
        return "", None
    return m.group(1).lower(), int(m.group(2))


def _matches(candidate: str, letters: str, num: Optional[int]) -> bool:
    """候选串（dmmCode 或 url）里是否含「字母 + 该数字（容忍补零/厂牌数字前缀）」。"""
    if not candidate or not letters or num is None:
        return False
    s = re.sub(r"[^a-z0-9]", "", candidate.lower())
    idx = s.find(letters)
    while idx != -1:
        # 字母段须在边界起始（行首，或前一字符非字母——DMM 厂牌前缀为数字/下划线，
        # 借此避免 sabp 误命中 abp 之类）
        if idx == 0 or not s[idx - 1].isalpha():
            rest = s[idx + len(letters):]
            mm = re.match(r"(\d+)", rest)
            if mm and int(mm.group(1)) == num:
                return True
        idx = s.find(letters, idx + 1)
    return False


def _cid_from_url(url: str) -> str:
    m = re.search(r"cid=([a-z0-9_]+)", (url or ""), re.I)
    return m.group(1) if m else ""


async def resolve(config: dict, code: str, source: str = "") -> dict:
    """
    查 DMM 并选出要填入 dmmCode 框的值。返回：
      {ok, dmm_code, url, title, matched, count, candidates, skipped, error}
    - skipped=True：非有码字母+数字番号 / 未启用，不算失败、不阻断发布。
    - matched=False：番号未精确匹配，兜底取站点排序首条（前端提示人工核对）。
    """
    out = {"ok": False, "dmm_code": "", "url": "", "title": "", "matched": False,
           "count": 0, "candidates": [], "skipped": False, "error": ""}

    if not config.get("dmm_lookup_enabled", True):
        out["skipped"] = True
        out["error"] = "未启用 DMM 查询"
        return out
    if not is_target_code(code, source):
        out["skipped"] = True
        out["error"] = "非有码字母+数字番号，跳过 DMM 查询"
        return out

    res = await mteam.dmm_search(config, code.strip())
    if not res["ok"]:
        out["error"] = res["error"]
        return out
    items = res["items"]
    out["count"] = len(items)
    out["candidates"] = [{"dmm_code": x.get("dmm_code"), "title": x.get("title"),
                          "url": x.get("url")} for x in items[:8]]
    if not items:
        out["error"] = "DMM 无候选结果"
        return out

    letters, num = _split_code(code)
    chosen = next((it for it in items
                   if _matches(it.get("dmm_code"), letters, num)
                   or _matches(it.get("url"), letters, num)), None)
    if chosen:
        out["matched"] = True
    else:
        chosen = items[0]   # 站点搜索已排序，兜底首条（标 matched=False 提示核对）

    dmm_code = chosen.get("dmm_code") or _cid_from_url(chosen.get("url") or "")
    out["ok"] = True
    out["dmm_code"] = dmm_code
    out["url"] = chosen.get("url") or ""
    out["title"] = chosen.get("title") or ""
    return out
