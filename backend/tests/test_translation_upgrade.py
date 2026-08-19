import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import translator


class _Response:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.text = json.dumps(data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            request = mock.Mock()
            raise translator.httpx.HTTPStatusError("failed", request=request, response=self)


class _Client:
    response = _Response({})
    last = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        type(self).last = (url, kwargs, self.kwargs)
        return type(self).response

    async def get(self, url, **kwargs):
        type(self).last = (url, kwargs, self.kwargs)
        return type(self).response


class TranslationAdapterTests(unittest.TestCase):
    def test_default_provider_is_selected_on_backend(self):
        expected = {"success": True, "result": "中文", "provider": "google"}
        with mock.patch.object(translator, "translate_google", mock.AsyncMock(return_value=expected)) as call:
            result = asyncio.run(translator.translate(
                "日本語", None, {"default_translate_provider": "google",
                              "translate_provider_configs": {"google": {"api_key": "key"}}}))
        self.assertEqual(result, expected)
        call.assert_awaited_once()

    def test_openai_compatible_request_uses_saved_endpoint_model_and_proxy(self):
        _Client.response = _Response({"choices": [{"message": {"content": "中文标题"}}]})
        cfg = {"api_key": "secret", "base_url": "https://gateway.example/v1", "model": "model-x",
               "_proxy": "http://proxy:7890"}
        with mock.patch.object(translator.httpx, "AsyncClient", _Client):
            result = asyncio.run(translator.translate_openai_compatible("日本語", "custom_openai", cfg, "ja", "zh", 30))
        self.assertTrue(result["success"])
        url, request, client = _Client.last
        self.assertEqual(url, "https://gateway.example/v1/chat/completions")
        self.assertEqual(request["json"]["model"], "model-x")
        self.assertEqual(request["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(client["proxy"], "http://proxy:7890")

    def test_deepl_protocol_and_google_html_unescape(self):
        _Client.response = _Response({"translations": [{"text": "中文"}]})
        with mock.patch.object(translator.httpx, "AsyncClient", _Client):
            result = asyncio.run(translator.translate_deepl(
                "日本語", {"api_key": "k", "base_url": "https://api-free.deepl.com"}, "ja", "zh", 30))
        self.assertEqual(result["result"], "中文")
        self.assertEqual(_Client.last[0], "https://api-free.deepl.com/v2/translate")
        self.assertEqual(_Client.last[1]["headers"]["Authorization"], "DeepL-Auth-Key k")

        _Client.response = _Response({"data": {"translations": [{"translatedText": "A &amp; B"}]}})
        with mock.patch.object(translator.httpx, "AsyncClient", _Client):
            result = asyncio.run(translator.translate_google(
                "A and B", {"api_key": "k", "base_url": "https://translation.googleapis.com"}, "auto", "zh", 30))
        self.assertEqual(result["result"], "A & B")


class TranslationConfigTests(unittest.TestCase):
    def test_nested_translation_secrets_are_masked(self):
        config = {"translate_provider_configs": {
            "openai": {"api_key": "sk-12345678", "model": "m"},
            "youdao": {"app_secret": "abcdefgh"},
        }}
        with mock.patch.object(main, "load_config", return_value=config):
            response = asyncio.run(main.api_get_config())
        body = json.loads(response.body)
        self.assertEqual(body["translate_provider_configs"]["openai"]["api_key"], "***5678")
        self.assertEqual(body["translate_provider_configs"]["youdao"]["app_secret"], "***efgh")
        self.assertEqual(body["translate_provider_configs"]["openai"]["model"], "m")

    def test_masked_nested_secret_is_preserved_when_saving(self):
        original = {"translate_provider_configs": {"openai": {"api_key": "sk-original", "model": "old"}}}
        captured = {}

        def save(value):
            captured.update(value)
            return True

        request = main.ConfigUpdateRequest(translate_provider_configs={
            "openai": {"api_key": "***inal", "model": "new"}})
        with mock.patch.object(main, "load_config", return_value=original), \
                mock.patch.object(main, "save_config", side_effect=save), \
                mock.patch.object(main.library, "ensure_monitor"):
            result = asyncio.run(main.api_set_config(request))
        self.assertTrue(result["success"])
        self.assertEqual(captured["translate_provider_configs"]["openai"]["api_key"], "sk-original")
        self.assertEqual(captured["translate_provider_configs"]["openai"]["model"], "new")


if __name__ == "__main__":
    unittest.main()
