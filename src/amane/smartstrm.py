"""SmartStrm 远端删除通知.

Amane 删除库内媒体文件后本地 STRM 消失, 网盘源文件仍在. SmartStrm 的 Emby 删除同步接口按 Emby
通知的格式读取 ``Item.Path``: 去掉 ``.strm`` 后缀取父目录, 映射到网盘存储路径后整个影片目录一起删
(契约见 https://smartstrm.github.io/settings/webhook ). Amane 按同一契约直接投递, 不必在 Emby 侧绕一圈:

    {"Event": "library.deleted", "Item": {"Path": "/strm/ym/ABC-123/ABC-123.strm"}}

路径按段替换: ``smartstrm.local_root`` (Amane 侧的挂载根) 整个换成 ``smartstrm.emby_root``
(SmartStrm 挂载到 Emby 的根). 同一父目录只投递一次 — 多集结号共用一个影片目录, 重复投递没有意义.
映射范围外的路径丢弃; 失败只记日志, 不改任务终态.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol

import structlog

if TYPE_CHECKING:
    from .config import SmartStrmConfig
    from .net.http import WebClient

logger = structlog.get_logger()

_DELETE_EVENT = "library.deleted"
"""SmartStrm 的 Emby 删除同步接受的事件名之一."""


class DeleteNotifier(Protocol):
    """DELETE handler 依赖的最小接口; 测试可用结构兼容的替代实现."""

    @property
    def enabled(self) -> bool: ...

    async def notify_deleted(self, paths: Sequence[Path]) -> None: ...


class SmartStrmNotifier:
    """把库内媒体文件的删除投递给 SmartStrm; 未启用时 ``notify_deleted`` 直接返回."""

    def __init__(self, web_client: WebClient, config: SmartStrmConfig) -> None:
        self._web_client = web_client
        self._config = config

    @property
    def enabled(self) -> bool:
        return self._config.enabled and bool(self._config.webhook_url.strip())

    async def notify_deleted(self, paths: Sequence[Path]) -> None:
        """按父目录去重后逐条投递; 调用方给出的是已删除的媒体文件路径."""
        if not self.enabled:
            return
        for emby_path in self._emby_paths(paths):
            await self._post(emby_path)

    def _emby_paths(self, paths: Iterable[Path]) -> list[str]:
        """映射成 Emby 侧路径并按父目录去重; 不在 ``local_root`` 内的路径丢弃."""
        local_root = PurePosixPath(self._config.local_root.strip() or "/")
        emby_root = PurePosixPath(self._config.emby_root.strip() or "/")
        seen: set[str] = set()
        mapped: list[str] = []
        for path in paths:
            posix = PurePosixPath(str(path).replace("\\", "/"))
            if not posix.is_relative_to(local_root):
                logger.debug("smartstrm notify skipped", path=str(path), reason="outside local_root")
                continue
            emby_path = emby_root / posix.relative_to(local_root)
            parent = str(emby_path.parent)
            if parent in seen:
                continue
            seen.add(parent)
            mapped.append(str(emby_path))
        return mapped

    async def _post(self, emby_path: str) -> None:
        url = self._config.webhook_url.strip()
        try:
            await self._web_client.request(
                "POST",
                url,
                json={"Event": _DELETE_EVENT, "Item": {"Path": emby_path}},
                use_proxy=False,
                timeout=self._config.timeout,
                max_attempts=1,
            )
        except Exception as e:
            logger.warning("smartstrm delete notify failed", path=emby_path, error=str(e))
            return
        logger.info("smartstrm delete notified", path=emby_path)
