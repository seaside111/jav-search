"""
翻译模块 — 支持阿里云翻译 和 百度翻译 API
"""
import hashlib
import hmac
import time
import random
import json
import uuid
from datetime import datetime, timezone
from typing import Optional
import httpx


# ──────────────────────────────────────────────
# 百度翻译
# ──────────────────────────────────────────────
BAIDU_TRANSLATE_URL = "https://fanyi-api.baidu.com/api/trans/vip/translate"


async def translate_baidu(
    text: str,
    app_id: str,
    secret_key: str,
    from_lang: str = "ja",
    to_lang: str = "zh",
) -> dict:
    """
    百度翻译 API
    https://fanyi-api.baidu.com/api/trans/vip/translate
    """
    if not app_id or not secret_key:
        return {"success": False, "error": "未配置百度翻译 APP ID / Secret Key"}
    
    salt = str(random.randint(32768, 65536))
    sign_str = app_id + text + salt + secret_key
    sign = hashlib.md5(sign_str.encode("utf-8")).hexdigest()
    
    params = {
        "q": text,
        "from": from_lang,
        "to": to_lang,
        "appid": app_id,
        "salt": salt,
        "sign": sign,
    }
    
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(BAIDU_TRANSLATE_URL, params=params)
            data = resp.json()
            if "trans_result" in data:
                translated = "\n".join(item["dst"] for item in data["trans_result"])
                return {"success": True, "result": translated, "provider": "baidu"}
            else:
                error_code = data.get("error_code", "unknown")
                return {"success": False, "error": f"百度翻译错误码: {error_code}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ──────────────────────────────────────────────
# 阿里云机器翻译
# ──────────────────────────────────────────────
ALIYUN_TRANSLATE_URL = "https://mt.cn-hangzhou.aliyuncs.com/"


def _aliyun_sign(method: str, params: dict, secret: str) -> str:
    """阿里云 API 签名 v1（用于旧版通用版）"""
    import urllib.parse
    
    sorted_params = sorted(params.items())
    query_str = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(v), safe='')}"
        for k, v in sorted_params
    )
    string_to_sign = f"{method}&{urllib.parse.quote('/', safe='')}&{urllib.parse.quote(query_str, safe='')}"
    key = (secret + "&").encode("utf-8")
    hashed = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha1)
    import base64
    return base64.b64encode(hashed.digest()).decode("utf-8")


async def translate_aliyun(
    text: str,
    access_key_id: str,
    access_key_secret: str,
    from_lang: str = "ja",
    to_lang: str = "zh",
) -> dict:
    """
    阿里云机器翻译（通用版）
    文档：https://help.aliyun.com/document_detail/158244.html
    """
    if not access_key_id or not access_key_secret:
        return {"success": False, "error": "未配置阿里云 Access Key"}
    
    import urllib.parse
    import base64
    
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    nonce = str(uuid.uuid4())
    
    params = {
        "Action": "TranslateGeneral",
        "Version": "2018-10-12",
        "AccessKeyId": access_key_id,
        "Timestamp": timestamp,
        "Format": "JSON",
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": nonce,
        "SourceLanguage": from_lang,
        "TargetLanguage": to_lang,
        "SourceText": text,
        "Scene": "general",
    }
    
    # 签名
    sorted_params = sorted(params.items())
    query_str = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(v), safe='')}"
        for k, v in sorted_params
    )
    string_to_sign = f"GET&{urllib.parse.quote('/', safe='')}&{urllib.parse.quote(query_str, safe='')}"
    key = (access_key_secret + "&").encode("utf-8")
    hashed = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha1)
    signature = base64.b64encode(hashed.digest()).decode("utf-8")
    params["Signature"] = signature
    
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(ALIYUN_TRANSLATE_URL, params=params)
            data = resp.json()
            if data.get("Code") == "200" and "Data" in data:
                translated = data["Data"]["Translated"]
                return {"success": True, "result": translated, "provider": "aliyun"}
            else:
                code = data.get("Code") or data.get("code", "unknown")
                msg = data.get("Message") or data.get("message", "")
                return {"success": False, "error": f"阿里云翻译错误: [{code}] {msg}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def translate(
    text: str,
    provider: str,
    config: dict,
    from_lang: str = "auto",
    to_lang: str = "zh",
) -> dict:
    """统一翻译入口"""
    if not text or not text.strip():
        return {"success": False, "error": "翻译内容为空"}
    
    if provider == "baidu":
        return await translate_baidu(
            text,
            config.get("baidu_app_id", ""),
            config.get("baidu_secret_key", ""),
            from_lang,
            to_lang,
        )
    elif provider == "aliyun":
        return await translate_aliyun(
            text,
            config.get("aliyun_access_key_id", ""),
            config.get("aliyun_access_key_secret", ""),
            from_lang,
            to_lang,
        )
    else:
        return {"success": False, "error": f"不支持的翻译服务: {provider}"}
