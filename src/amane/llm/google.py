"""Google Cloud Translation Basic v2 翻译器.

一个字段一次请求: ``q`` 单元素数组, ``source=auto`` (省略), ``target`` 由 ``Language`` 映射.
返回文本可能含 HTML 实体 (``&#39;`` 等), 统一 ``html.unescape`` 后写入. 已是目标语言或结果与原文相同返回
``None``, 调用方保留原值. 译文写入 ``TranslationCache``, 缓存键以端点标识区分 provider.
"""

import html

import structlog
from aiolimiter import AsyncLimiter
from httpx2 import AsyncClient
from pydantic import BaseModel

from ..enums import Language, MetadataField
from .cache import TranslationCache

logger = structlog.get_logger()

_GOOGLE_TARGET: dict[Language, str] = {
    Language.ZH_CN: "zh-CN",
    Language.ZH_TW: "zh-TW",
    Language.JP: "ja",
    Language.EN: "en",
}


class _Translation(BaseModel):
    translatedText: str


class _TranslationData(BaseModel):
    translations: list[_Translation]


class _TranslationResponse(BaseModel):
    data: _TranslationData


class GoogleTranslator:
    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str,
        http_client: AsyncClient,
        cache: TranslationCache | None = None,
        rate_limit: float = 2.0,
    ) -> None:
        self._api_key = api_key
        self._endpoint = endpoint
        self._http_client = http_client
        self._cache = cache
        # Google 端点独立限速, 与站点 host 限速隔离: 桶容量 1, 严格平滑.
        self._limiter = AsyncLimiter(1, 1 / rate_limit)
        # 缓存键的 provider 指纹: 与 LLM 的 system 提示词不冲突, 换端点自然失效.
        self._cache_variant = f"google:{endpoint}"

    async def translate(
        self, text: str, target: Language, field: MetadataField, *, use_cache: bool = True
    ) -> str | None:
        text = text.strip()
        if not text:
            return None
        if self._cache is not None and use_cache:
            cached = await self._cache.get(text, target, field, self._cache_variant)
            if cached is not None:
                return cached
        translated = await self._ask(text, target)
        if not translated or translated == text:
            return None
        if self._cache is not None:
            await self._cache.put(text, target, field, self._cache_variant, translated)
        return translated

    async def _ask(self, text: str, target: Language) -> str | None:
        """单次请求; 失败或响应为空时返回 ``None``, 不抛异常."""
        payload = {"q": [text], "target": _GOOGLE_TARGET[target], "format": "text"}
        async with self._limiter:
            try:
                response = await self._http_client.post(self._endpoint, params={"key": self._api_key}, json=payload)
                response.raise_for_status()
                data = _TranslationResponse.model_validate(response.json())
            except Exception as e:
                logger.warning("Google translation request failed", error=str(e))
                return None
        if not data.data.translations:
            return None
        # Google 会转义引号 / 撇号等 HTML 实体, 入库前还原为纯文本.
        return html.unescape(data.data.translations[0].translatedText).strip() or None
