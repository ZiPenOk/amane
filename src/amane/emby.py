"""Emby 媒体库刷新通知.

任务链里的 ORGANIZE / DELETE 全部进入终态后, 调用 ``POST {url}/Library/Refresh`` 让 Emby 立即重扫.
API Key 经 ``X-Emby-Token`` 头发送; Emby 在本机 / 内网, 直连不走网络代理; 失败只记日志, 不改任务终态.

两层合并:

- **链级**: 同一任务链只通知一次. ``REFRESH`` 扇出的批量整理有几十个子任务, 全部结束才刷新一次;
- **跨链**: 文件监控一次拖入多个影片会派生多条互相独立的链, 在 ``emby.debounce_seconds`` 窗口内合并
  成一次刷新 (窗口内每来一条链就重新计时).

实例跨热重载复用 (``set_context`` 换 HTTP 通道与配置), 去重与待刷新状态随进程存活.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import suppress
from typing import TYPE_CHECKING, Protocol

import structlog

if TYPE_CHECKING:
    from .config import EmbyConfig
    from .net.http import WebClient

logger = structlog.get_logger()

_MAX_NOTIFIED_ROOTS = 4096
"""链级去重的上界; 超出按最早通知的链淘汰, 淘汰后重复通知最多多发一次刷新."""


class EmbyRefresher(Protocol):
    """Worker 依赖的最小接口; 测试可用结构兼容的替代实现."""

    @property
    def enabled(self) -> bool: ...

    async def refresh(self, *, root_task_id: int) -> None: ...


class EmbyNotifier:
    """媒体库刷新通知器. 未启用时 ``refresh`` 直接返回, 调用方不必判断配置."""

    def __init__(self, web_client: WebClient, config: EmbyConfig) -> None:
        self._web_client = web_client
        self._config = config
        # 多个 worker 可能同时看到各自所属链的链尾完成, 去重集合由锁保护.
        self._notified_roots: OrderedDict[int, None] = OrderedDict()
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task[None] | None = None
        self._merged = 0
        """当前窗口内合并进来的链数, 只用于日志."""

    def set_context(self, *, web_client: WebClient, config: EmbyConfig) -> None:
        """热重载换新 HTTP 通道与配置; 去重与待刷新状态继续复用."""
        self._web_client = web_client
        self._config = config

    @property
    def enabled(self) -> bool:
        return self._config.enabled and bool(self._config.url.strip())

    async def refresh(self, *, root_task_id: int) -> None:
        """链完成后请求刷新一次; 未启用与该链已通知过时静默返回."""
        if not self.enabled:
            return
        delay = 0.0
        async with self._lock:
            if root_task_id in self._notified_roots:
                return
            self._notified_roots[root_task_id] = None
            while len(self._notified_roots) > _MAX_NOTIFIED_ROOTS:
                self._notified_roots.popitem(last=False)
            delay = self._config.debounce_seconds
            if delay > 0:
                self._merged += 1
                if self._flush_task is not None:
                    self._flush_task.cancel()
                self._flush_task = asyncio.create_task(self._flush_after(delay))
        if delay <= 0:
            await self._send()

    async def aclose(self) -> None:
        """取消还没发出的刷新; 进程关闭与测试收尾都要调用."""
        async with self._lock:
            task = self._flush_task
            self._flush_task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _flush_after(self, delay: float) -> None:
        """窗口内没有新链时发出. 被新链取消时已发出的请求不受影响 (见 ``_send`` 的 shield)."""
        await asyncio.sleep(delay)
        async with self._lock:
            self._flush_task = None
        await self._send()

    async def _send(self) -> None:
        url = self._config.url.strip().rstrip("/") + "/Library/Refresh"
        headers: dict[str, str] = {}
        if self._config.api_key:
            headers["X-Emby-Token"] = self._config.api_key
        merged = self._merged
        self._merged = 0
        try:
            # shield: 正在发出的刷新不因合并窗口重排而被取消一半.
            await asyncio.shield(
                self._web_client.request(
                    "POST",
                    url,
                    headers=headers,
                    use_proxy=False,
                    timeout=self._config.timeout,
                    max_attempts=1,
                )
            )
        except Exception as e:
            logger.warning("emby library refresh failed", url=url, merged_chains=merged, error=str(e))
            return
        logger.info("emby library refresh requested", url=url, merged_chains=merged)
