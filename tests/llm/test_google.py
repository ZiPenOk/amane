"""Google 免费 gtx 翻译器单元测试.

请求经 ``httpx2.MockTransport`` 驱动, 不触网. 覆盖: GET 参数与目标语言映射、嵌套数组解析、
全角井号还原、空 / 同文结果、失败降级、缓存去重与 ``build_translator`` 开关.
"""

import httpx2
import pytest

from amane.enums import ApiType, Language, MetadataField, TranslationProvider
from amane.llm import GoogleTranslator, TranslationCache, build_translator
from amane.llm import translator as translator_module


def _client(handler) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def _ok(text: str) -> httpx2.Response:
    return httpx2.Response(200, json=[[[text, "source", None, None, 10]], None, "en"])


def _translator(client: httpx2.AsyncClient, cache: TranslationCache | None = None) -> GoogleTranslator:
    return GoogleTranslator(
        endpoint="https://example.test/translate_a/single",
        http_client=client,
        cache=cache,
        rate_limit=100.0,
    )


@pytest.mark.asyncio
async def test_google_translate_returns_text_and_sends_params():
    seen: dict[str, str] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["url"] = str(request.url)
        return _ok("你好")

    client = _client(handler)
    t = _translator(client)
    assert await t.translate("Hello", Language.ZH_CN, MetadataField.TITLE) == "你好"
    assert "client=gtx" in seen["url"]
    assert "tl=zh-CN" in seen["url"]
    assert "q=Hello" in seen["url"]
    await client.aclose()


@pytest.mark.asyncio
async def test_google_translate_maps_language_and_restores_hash():
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        return _ok("＃1 作品")

    client = _client(handler)
    t = _translator(client)
    assert await t.translate("Hello", Language.ZH_TW, MetadataField.PLOT) == "#1 作品"
    assert "tl=zh-TW" in seen[0]
    await client.aclose()


@pytest.mark.asyncio
async def test_google_translate_skips_empty_and_unchanged():
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return _ok("Hello")

    client = _client(handler)
    t = _translator(client)
    assert await t.translate("", Language.ZH_CN, MetadataField.TITLE) is None
    assert await t.translate("   ", Language.ZH_CN, MetadataField.TITLE) is None
    assert await t.translate("Hello", Language.EN, MetadataField.TITLE) is None
    assert calls == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_google_translate_malformed_payload_returns_none():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"unexpected": True})

    client = _client(handler)
    t = _translator(client)
    assert await t.translate("Hello", Language.ZH_CN, MetadataField.TITLE) is None
    await client.aclose()


@pytest.mark.asyncio
async def test_google_translate_failure_returns_none():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(500, text="blocked")

    client = _client(handler)
    t = _translator(client)
    assert await t.translate("Hello", Language.ZH_CN, MetadataField.TITLE) is None
    await client.aclose()


@pytest.mark.asyncio
async def test_google_translator_uses_cache(tmp_path):
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return _ok("你好")

    client = _client(handler)
    cache = TranslationCache(tmp_path / "translations.db")
    t = _translator(client, cache)
    assert await t.translate("Hello", Language.ZH_CN, MetadataField.TITLE) == "你好"
    assert await t.translate("Hello", Language.ZH_CN, MetadataField.TITLE) == "你好"
    assert calls == 1
    await cache.close()
    await client.aclose()


@pytest.mark.parametrize(
    "enabled,endpoint,expected_none",
    [
        (False, "https://example.test/translate_a/single", True),  # 未启用
        (True, "", True),  # 端点为空
        (True, "https://example.test/translate_a/single", False),  # 正常装配
    ],
)
def test_build_translator_google_gating(monkeypatch, enabled, endpoint, expected_none):
    class DummyClient:
        pass

    monkeypatch.setattr(translator_module, "AsyncClient", lambda **kwargs: DummyClient())
    t = build_translator(
        enabled=enabled,
        provider=TranslationProvider.GOOGLE,
        api_type=ApiType.CHAT,
        api_key="llm-key",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
        rate_limit=2.0,
        google_endpoint=endpoint,
    )
    assert (t is None) == expected_none


def test_build_translator_google_carries_proxy(monkeypatch):
    captured: dict[str, object] = {}

    class DummyClient:
        pass

    def fake_client(**kwargs):
        captured.update(kwargs)
        return DummyClient()

    monkeypatch.setattr(translator_module, "AsyncClient", fake_client)
    t = build_translator(
        enabled=True,
        provider=TranslationProvider.GOOGLE,
        api_type=ApiType.CHAT,
        api_key="llm-key",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
        rate_limit=2.0,
        google_endpoint="https://mirror.test/translate_a/single",
        proxy="http://proxy.example:8080",
    )
    assert t is not None
    assert captured["proxy"] == "http://proxy.example:8080"
