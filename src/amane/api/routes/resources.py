"""本地文件按 url hash 直出 (无选源/裁剪/超分).
SR 可能就地覆盖 (URL 不变, 字节变), 须 ETag + no-cache 再验证.
``/proxy`` 由 ``scraping.passthrough_preview_images`` 控制; 开启时只透传不落盘.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated
from urllib.parse import urlparse

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse

from ...net.errors import RequestError
from ..deps import RuntimeDep
from ..support.http_cache import RESOURCE_CACHE_HEADERS, format_etag, maybe_not_modified

if TYPE_CHECKING:
    from ...app.proxy_failure_cache import ProxyFailureCache
    from ...app.runtime import AppRuntime

logger = structlog.get_logger()

router = APIRouter(prefix="/resources", tags=["resources"])

_EXT_TO_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".avif": "image/avif",
    ".mp4": "video/mp4",
}


def _mime_from_url(url: str) -> str:
    """按 URL 路径后缀猜 MIME; 猜不到按图片处理."""
    path = urlparse(url).path
    tail = path.rsplit("/", 1)[-1]
    ext = path[path.rfind(".") :].lower() if "." in tail else ""
    return _EXT_TO_MIME.get(ext, "image/jpeg")


async def _passthrough_image(runtime: AppRuntime, failures: ProxyFailureCache, url: str) -> Response:
    """不落盘: 拉一次字节直接回给前端, 复用同一个负缓存避免反复打上游."""

    async def _fetch() -> bytes | None:
        try:
            return await runtime.web_client.get_bytes(url)
        except RequestError:
            return None

    content = await failures.coalesce(url, _fetch)
    if content is None:
        failures.remember(url)
        logger.warning("proxy image passthrough failed", url=url)
        raise HTTPException(status_code=502, detail="上游图片获取失败")

    failures.forget(url)
    return Response(content=content, media_type=_mime_from_url(url), headers=dict(RESOURCE_CACHE_HEADERS))


@router.get("/proxy")
async def proxy_image(
    runtime: RuntimeDep, request: Request, url: Annotated[str, Query(description="External image URL to proxy")]
) -> Response:
    """上游失败记入进程内负缓存 (15 分钟), 窗口内同 URL 直接 502.
    同 URL 进行中请求合并为一次 acquire.
    ``scraping.passthrough_preview_images`` 开启时只透传字节, 不写资源目录.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=400, detail="仅支持 http/https URL")

    # 负缓存命中则直接 502
    failures = runtime.proxy_failure_cache
    if failures.is_blocked(url):
        raise HTTPException(status_code=502, detail="上游图片获取失败")

    if runtime.config.hot.scraping.passthrough_preview_images:
        return await _passthrough_image(runtime, failures, url)

    # 同 URL 进行中的 acquire 合并为一次
    store = runtime.resource_store
    local_path = await failures.coalesce(url, lambda: store.acquire(url, runtime.web_client))
    if local_path is None:
        failures.remember(url)
        logger.warning("proxy image acquire failed", url=url)
        raise HTTPException(status_code=502, detail="上游图片获取失败")

    # 成功后清除负缓存
    failures.forget(url)

    record = await store.get_by_url(url)
    content_hash = record.content_hash if record is not None else None
    not_modified = maybe_not_modified(request, content_hash)
    if not_modified is not None:
        return not_modified

    headers = dict(RESOURCE_CACHE_HEADERS)
    if content_hash:
        headers["ETag"] = format_etag(content_hash)
    return FileResponse(
        path=local_path, media_type=_EXT_TO_MIME.get(local_path.suffix.lower(), "image/jpeg"), headers=headers
    )


@router.get("/{url_hash}")
async def serve_resource(url_hash: str, request: Request, runtime: RuntimeDep) -> Response:
    found = await runtime.resource_store.get_by_url_hash(url_hash)
    if found is None:
        raise HTTPException(status_code=404, detail="资源不存在")
    record, path = found

    not_modified = maybe_not_modified(request, record.content_hash)
    if not_modified is not None:
        return not_modified

    headers = dict(RESOURCE_CACHE_HEADERS)
    if record.content_hash:
        headers["ETag"] = format_etag(record.content_hash)
    media_type = record.mime_type or _EXT_TO_MIME.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path=path, media_type=media_type, headers=headers)
