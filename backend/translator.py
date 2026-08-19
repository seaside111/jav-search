"""统一翻译入口：传统翻译 API、原生大模型 API 与 OpenAI 兼容网关。"""
import base64
import hashlib
import hmac
import html
import random
import time
import urllib.parse
import uuid
from datetime import datetime, timezone

import httpx

BAIDU_TRANSLATE_URL = "https://fanyi-api.baidu.com/api/trans/vip/translate"
ALIYUN_TRANSLATE_URL = "https://mt.cn-hangzhou.aliyuncs.com/"

# 地址和模型均可在设置页覆盖。
PROVIDER_DEFAULTS = {
    "deepl": {"base_url": "https://api-free.deepl.com"},
    "youdao": {"base_url": "https://openapi.youdao.com/v2/api"},
    "microsoft": {"base_url": "https://api.cognitive.microsofttranslator.com"},
    "google": {"base_url": "https://translation.googleapis.com"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4.1-mini"},
    "deepseek": {"base_url": "https://api.deepseek.com", "model": "deepseek-v4-flash"},
    "grok": {"base_url": "https://api.x.ai/v1", "model": "grok-4.5"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "model": "google/gemini-2.5-flash"},
    "siliconflow": {"base_url": "https://api.siliconflow.cn/v1", "model": "deepseek-ai/DeepSeek-V3.2"},
    "custom_openai": {"base_url": "", "model": ""},
    "claude": {"base_url": "https://api.anthropic.com", "model": "claude-sonnet-4-5"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta", "model": "gemini-2.5-flash"},
}
OPENAI_COMPATIBLE = {"openai", "deepseek", "grok", "openrouter", "siliconflow", "custom_openai"}


def _provider_config(config: dict, provider: str) -> dict:
    result = dict(PROVIDER_DEFAULTS.get(provider, {}))
    nested = config.get("translate_provider_configs") or {}
    if isinstance(nested.get(provider), dict):
        result.update(nested[provider])
    # 兼容旧版百度、阿里顶层配置，升级后无需重新填写。
    if provider == "baidu":
        result.setdefault("app_id", config.get("baidu_app_id", ""))
        result.setdefault("secret_key", config.get("baidu_secret_key", ""))
    elif provider == "aliyun":
        result.setdefault("access_key_id", config.get("aliyun_access_key_id", ""))
        result.setdefault("access_key_secret", config.get("aliyun_access_key_secret", ""))
    return result


def _timeout(config: dict) -> float:
    try:
        return max(5.0, min(120.0, float(config.get("translate_timeout_seconds", 30))))
    except (TypeError, ValueError):
        return 30.0


def _error(provider: str, exc: Exception) -> dict:
    if isinstance(exc, httpx.HTTPStatusError):
        return {"success": False, "error": f"{provider} 请求失败 HTTP {exc.response.status_code}: {exc.response.text[:300]}"}
    return {"success": False, "error": f"{provider} 请求失败: {exc}"}


def _clean_llm_text(value: str) -> str:
    value = (value or "").strip()
    if value.startswith("```") and value.endswith("```"):
        lines = value.splitlines()[1:-1]
        if lines and lines[0].strip().lower() in {"text", "translation", "plaintext"}:
            lines = lines[1:]
        value = "\n".join(lines).strip()
    return value


def _language_name(code: str) -> str:
    return {"auto": "automatically detected source language", "ja": "Japanese", "zh": "Simplified Chinese",
            "zh-cn": "Simplified Chinese", "zh-tw": "Traditional Chinese", "en": "English",
            "ko": "Korean", "fr": "French", "de": "German", "es": "Spanish"}.get((code or "").lower(), code)


def _llm_messages(text: str, from_lang: str, to_lang: str) -> list[dict]:
    return [
        {"role": "system", "content":
            f"You are a professional translator. Translate from {_language_name(from_lang)} to {_language_name(to_lang)}. "
            "Preserve paragraph breaks, names, identifiers and formatting. Return only the translation; "
            "do not explain, label, quote, censor, or summarize it."},
        {"role": "user", "content": text},
    ]


async def translate_baidu(text, cfg, from_lang, to_lang, timeout):
    app_id, secret = cfg.get("app_id", ""), cfg.get("secret_key", "")
    if not app_id or not secret:
        return {"success": False, "error": "未配置百度翻译 APP ID / Secret Key"}
    salt = str(random.randint(32768, 65536))
    params = {"q": text, "from": from_lang, "to": to_lang, "appid": app_id, "salt": salt,
              "sign": hashlib.md5((app_id + text + salt + secret).encode()).hexdigest()}
    try:
        async with httpx.AsyncClient(timeout=timeout, proxy=cfg.get("_proxy") or None) as client:
            data = (await client.get(BAIDU_TRANSLATE_URL, params=params)).json()
        if "trans_result" in data:
            return {"success": True, "result": "\n".join(x["dst"] for x in data["trans_result"]), "provider": "baidu"}
        return {"success": False, "error": f"百度翻译错误码: {data.get('error_code', 'unknown')} {data.get('error_msg', '')}"}
    except Exception as exc:
        return _error("百度翻译", exc)


async def translate_aliyun(text, cfg, from_lang, to_lang, timeout):
    key_id, secret = cfg.get("access_key_id", ""), cfg.get("access_key_secret", "")
    if not key_id or not secret:
        return {"success": False, "error": "未配置阿里云 Access Key"}
    params = {"Action": "TranslateGeneral", "Version": "2018-10-12", "AccessKeyId": key_id,
              "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "Format": "JSON",
              "SignatureMethod": "HMAC-SHA1", "SignatureVersion": "1.0", "SignatureNonce": str(uuid.uuid4()),
              "SourceLanguage": from_lang, "TargetLanguage": to_lang, "SourceText": text, "Scene": "general"}
    query = "&".join(f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(v), safe='')}" for k, v in sorted(params.items()))
    sign_text = f"GET&%2F&{urllib.parse.quote(query, safe='')}"
    params["Signature"] = base64.b64encode(hmac.new((secret + "&").encode(), sign_text.encode(), hashlib.sha1).digest()).decode()
    try:
        async with httpx.AsyncClient(timeout=timeout, proxy=cfg.get("_proxy") or None) as client:
            data = (await client.get(ALIYUN_TRANSLATE_URL, params=params)).json()
        if data.get("Code") == "200" and data.get("Data"):
            return {"success": True, "result": data["Data"]["Translated"], "provider": "aliyun"}
        return {"success": False, "error": f"阿里云翻译错误: [{data.get('Code', 'unknown')}] {data.get('Message', '')}"}
    except Exception as exc:
        return _error("阿里云翻译", exc)


async def translate_deepl(text, cfg, from_lang, to_lang, timeout):
    key = cfg.get("api_key", "")
    if not key:
        return {"success": False, "error": "未配置 DeepL API Key"}
    body = {"text": [text], "target_lang": "ZH-HANS" if to_lang in {"zh", "zh-cn"} else to_lang.upper()}
    if from_lang != "auto": body["source_lang"] = from_lang.upper()
    url = cfg.get("base_url", PROVIDER_DEFAULTS["deepl"]["base_url"]).rstrip("/") + "/v2/translate"
    try:
        async with httpx.AsyncClient(timeout=timeout, proxy=cfg.get("_proxy") or None) as client:
            resp = await client.post(url, headers={"Authorization": f"DeepL-Auth-Key {key}"}, json=body)
            resp.raise_for_status(); data = resp.json()
        return {"success": True, "result": "\n".join(x["text"] for x in data["translations"]), "provider": "deepl"}
    except Exception as exc:
        return _error("DeepL", exc)


def _youdao_truncate(text: str) -> str:
    return text if len(text) <= 20 else text[:10] + str(len(text)) + text[-10:]


async def translate_youdao(text, cfg, from_lang, to_lang, timeout):
    app_key, secret = cfg.get("app_key", ""), cfg.get("app_secret", "")
    if not app_key or not secret:
        return {"success": False, "error": "未配置有道智云应用 ID / 应用密钥"}
    salt, curtime = str(uuid.uuid4()), str(int(time.time()))
    params = {"q": text, "from": from_lang, "to": "zh-CHS" if to_lang in {"zh", "zh-cn"} else to_lang,
              "appKey": app_key, "salt": salt, "signType": "v3", "curtime": curtime}
    params["sign"] = hashlib.sha256((app_key + _youdao_truncate(text) + salt + curtime + secret).encode()).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=timeout, proxy=cfg.get("_proxy") or None) as client:
            data = (await client.post(cfg.get("base_url", PROVIDER_DEFAULTS["youdao"]["base_url"]), data=params)).json()
        if data.get("errorCode") == "0" and data.get("translation"):
            return {"success": True, "result": "\n".join(data["translation"]), "provider": "youdao"}
        return {"success": False, "error": f"有道翻译错误码: {data.get('errorCode', 'unknown')}"}
    except Exception as exc:
        return _error("有道翻译", exc)


async def translate_microsoft(text, cfg, from_lang, to_lang, timeout):
    key = cfg.get("subscription_key", "")
    if not key:
        return {"success": False, "error": "未配置 Microsoft Translator 订阅密钥"}
    params = {"api-version": "3.0", "to": "zh-Hans" if to_lang in {"zh", "zh-cn"} else to_lang}
    if from_lang != "auto": params["from"] = from_lang
    headers = {"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/json"}
    if cfg.get("region"): headers["Ocp-Apim-Subscription-Region"] = cfg["region"]
    url = cfg.get("base_url", PROVIDER_DEFAULTS["microsoft"]["base_url"]).rstrip("/") + "/translate"
    try:
        async with httpx.AsyncClient(timeout=timeout, proxy=cfg.get("_proxy") or None) as client:
            resp = await client.post(url, params=params, headers=headers, json=[{"Text": text}])
            resp.raise_for_status(); data = resp.json()
        return {"success": True, "result": data[0]["translations"][0]["text"], "provider": "microsoft"}
    except Exception as exc:
        return _error("Microsoft Translator", exc)


async def translate_google(text, cfg, from_lang, to_lang, timeout):
    key = cfg.get("api_key", "")
    if not key:
        return {"success": False, "error": "未配置 Google Cloud Translation API Key"}
    body = {"q": text, "target": "zh-CN" if to_lang in {"zh", "zh-cn"} else to_lang, "format": "text"}
    if from_lang != "auto": body["source"] = from_lang
    url = cfg.get("base_url", PROVIDER_DEFAULTS["google"]["base_url"]).rstrip("/") + "/language/translate/v2"
    try:
        async with httpx.AsyncClient(timeout=timeout, proxy=cfg.get("_proxy") or None) as client:
            resp = await client.post(url, headers={"x-goog-api-key": key}, json=body)
            resp.raise_for_status(); data = resp.json()
        return {"success": True, "result": "\n".join(html.unescape(x["translatedText"]) for x in data["data"]["translations"]), "provider": "google"}
    except Exception as exc:
        return _error("Google 翻译", exc)


async def translate_openai_compatible(text, provider, cfg, from_lang, to_lang, timeout):
    key, base_url, model = cfg.get("api_key", ""), cfg.get("base_url", "").rstrip("/"), cfg.get("model", "")
    if not key or not base_url or not model:
        return {"success": False, "error": f"未完整配置 {provider} 的 API Key、API 地址和模型"}
    try:
        request_body = {"model": model, "messages": _llm_messages(text, from_lang, to_lang), "temperature": 0.1}
        if provider == "deepseek": request_body["thinking"] = {"type": "disabled"}
        if provider == "siliconflow": request_body["enable_thinking"] = False
        async with httpx.AsyncClient(timeout=timeout, proxy=cfg.get("_proxy") or None) as client:
            resp = await client.post(base_url + "/chat/completions", headers={"Authorization": f"Bearer {key}"},
                json=request_body)
            resp.raise_for_status(); data = resp.json()
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, list): content = "".join(x.get("text", "") for x in content if isinstance(x, dict))
        return {"success": True, "result": _clean_llm_text(content), "provider": provider}
    except Exception as exc:
        return _error(provider, exc)


async def translate_claude(text, cfg, from_lang, to_lang, timeout):
    key, base_url, model = cfg.get("api_key", ""), cfg.get("base_url", "").rstrip("/"), cfg.get("model", "")
    if not key or not base_url or not model:
        return {"success": False, "error": "未完整配置 Claude 的 API Key、API 地址和模型"}
    messages = _llm_messages(text, from_lang, to_lang)
    try:
        async with httpx.AsyncClient(timeout=timeout, proxy=cfg.get("_proxy") or None) as client:
            resp = await client.post(base_url + "/v1/messages", headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                json={"model": model, "max_tokens": 4096, "temperature": 0.1,
                      "system": messages[0]["content"], "messages": messages[1:]})
            resp.raise_for_status(); data = resp.json()
        content = "".join(x.get("text", "") for x in data.get("content", []) if x.get("type") == "text")
        return {"success": True, "result": _clean_llm_text(content), "provider": "claude"}
    except Exception as exc:
        return _error("Claude", exc)


async def translate_gemini(text, cfg, from_lang, to_lang, timeout):
    key, base_url, model = cfg.get("api_key", ""), cfg.get("base_url", "").rstrip("/"), cfg.get("model", "")
    if not key or not base_url or not model:
        return {"success": False, "error": "未完整配置 Gemini 的 API Key、API 地址和模型"}
    system = _llm_messages(text, from_lang, to_lang)[0]["content"]
    url = f"{base_url}/models/{urllib.parse.quote(model, safe='-_.')}:generateContent"
    try:
        async with httpx.AsyncClient(timeout=timeout, proxy=cfg.get("_proxy") or None) as client:
            resp = await client.post(url, headers={"x-goog-api-key": key}, json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": text}]}],
                "generationConfig": {"temperature": 0.1}})
            resp.raise_for_status(); data = resp.json()
        parts = data["candidates"][0]["content"]["parts"]
        return {"success": True, "result": _clean_llm_text("".join(x.get("text", "") for x in parts)), "provider": "gemini"}
    except Exception as exc:
        return _error("Gemini", exc)


async def translate(text: str, provider: str, config: dict, from_lang: str = "auto", to_lang: str = "zh") -> dict:
    """统一翻译入口；provider 为空时使用已保存的默认渠道。"""
    if not text or not text.strip(): return {"success": False, "error": "翻译内容为空"}
    provider = (provider or config.get("default_translate_provider") or "baidu").strip().lower()
    cfg, timeout = _provider_config(config, provider), _timeout(config)
    cfg["_proxy"] = config.get("proxy", "")
    if provider == "baidu": return await translate_baidu(text, cfg, from_lang, to_lang, timeout)
    if provider == "aliyun": return await translate_aliyun(text, cfg, from_lang, to_lang, timeout)
    if provider == "deepl": return await translate_deepl(text, cfg, from_lang, to_lang, timeout)
    if provider == "youdao": return await translate_youdao(text, cfg, from_lang, to_lang, timeout)
    if provider == "microsoft": return await translate_microsoft(text, cfg, from_lang, to_lang, timeout)
    if provider == "google": return await translate_google(text, cfg, from_lang, to_lang, timeout)
    if provider in OPENAI_COMPATIBLE: return await translate_openai_compatible(text, provider, cfg, from_lang, to_lang, timeout)
    if provider == "claude": return await translate_claude(text, cfg, from_lang, to_lang, timeout)
    if provider == "gemini": return await translate_gemini(text, cfg, from_lang, to_lang, timeout)
    return {"success": False, "error": f"不支持的翻译服务: {provider}"}
