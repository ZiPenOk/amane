"""Google 免费 gtx 翻译器.

请求 ``translate_a/single?client=gtx&sl=auto&dt=t&q=...``, 不依赖 API key. 默认端点为
``translate.google.com``, 可改为 ``translate.googleapis.com`` 或兼容镜像. 一个字段一次请求;
响应是嵌套数组, 取 ``response[0][*][0]`` 拼接为译文. 已是目标语言或结果与原文相同返回 ``None``,
调用方保留原值. 译文写入 ``TranslationCache``, 缓存键以端点标识区分 provider.
"""

import structlog
from aiolimiter import AsyncLimiter
from httpx2 import AsyncClient

from ..enums import Language, MetadataField
from .cache import TranslationCache

logger = structlog.get_logger()

_GOOGLE_TARGET: dict[Language, str] = {
    Language.ZH_CN: "zh-CN",
    Language.ZH_TW: "zh-TW",
    Language.JP: "ja",
    Language.EN: "en",
}


def _extract_translation(payload: object) -> str | None:
    """解析 gtx 嵌套数组: ``[[["译文", "原文", ...], ...], ...]``. 空或形状异常返回 ``None``."""
    if not isinstance(payload, list) or not payload:
        return None
    segments = payload[0]
    if not isinstance(segments, list):
        return None
    translated = (
        "".join(
            segment[0] for segment in segments if isinstance(segment, list) and segment and isinstance(segment[0], str)
        )
        .replace("＃", "#")
        .strip()
    )
    return translated or None


class GoogleTranslator:
    def __init__(
        self,
        *,
        endpoint: str,
        http_client: AsyncClient,
        cache: TranslationCache | None = None,
        rate_limit: float = 2.0,
    ) -> None:
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
        params = {"client": "gtx", "sl": "auto", "tl": _GOOGLE_TARGET[target], "dt": "t", "q": text}
        async with self._limiter:
            try:
                response = await self._http_client.get(self._endpoint, params=params)
                response.raise_for_status()
                payload = response.json()
            except Exception as e:
                logger.warning("Google translation request failed", error=str(e))
                return None
        return _extract_translation(payload)
