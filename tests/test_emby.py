"""Emby 媒体库刷新通知: 链级去重, 跨链合并窗口, 失败静默."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import pytest

from amane.config import EmbyConfig
from amane.emby import EmbyNotifier

if TYPE_CHECKING:
    from amane.net.http import WebClient


class FakeWebClient:
    """记录出站请求; 可注入异常."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._error = error

    async def request(self, method: str, url: str, **kwargs: Any) -> None:
        self.calls.append({"method": method, "url": url, **kwargs})
        if self._error is not None:
            raise self._error


def make_notifier(client: FakeWebClient, settings: EmbyConfig) -> EmbyNotifier:
    """结构替身只在类型检查处按生产类型收窄."""
    return EmbyNotifier(cast("WebClient", client), settings)


def config(**overrides: Any) -> EmbyConfig:
    """默认关掉合并窗口, 让每条链的请求立即发生."""
    values: dict[str, Any] = {"enabled": True, "debounce_seconds": 0, **overrides}
    return EmbyConfig(**values)


@pytest.mark.asyncio(loop_scope="function")
async def test_disabled_sends_nothing() -> None:
    client = FakeWebClient()
    notifier = make_notifier(client, EmbyConfig(enabled=False, debounce_seconds=0))

    await notifier.refresh(root_task_id=1)

    assert notifier.enabled is False
    assert client.calls == []


@pytest.mark.asyncio(loop_scope="function")
async def test_blank_url_sends_nothing() -> None:
    client = FakeWebClient()
    notifier = make_notifier(client, config(url="   "))

    await notifier.refresh(root_task_id=1)

    assert notifier.enabled is False
    assert client.calls == []


@pytest.mark.asyncio(loop_scope="function")
async def test_same_chain_notified_once() -> None:
    client = FakeWebClient()
    notifier = make_notifier(client, config(api_key="secret"))

    await notifier.refresh(root_task_id=7)
    await notifier.refresh(root_task_id=7)
    await notifier.refresh(root_task_id=8)

    assert [call["url"] for call in client.calls] == ["http://127.0.0.1:8096/Library/Refresh"] * 2
    first = client.calls[0]
    assert first["method"] == "POST"
    assert first["headers"] == {"X-Emby-Token": "secret"}
    assert first["use_proxy"] is False
    assert first["max_attempts"] == 1


@pytest.mark.asyncio(loop_scope="function")
async def test_trailing_slash_and_missing_key() -> None:
    client = FakeWebClient()
    notifier = make_notifier(client, config(url="http://emby.local:8096/"))

    await notifier.refresh(root_task_id=1)

    assert client.calls[0]["url"] == "http://emby.local:8096/Library/Refresh"
    assert client.calls[0]["headers"] == {}


@pytest.mark.asyncio(loop_scope="function")
async def test_request_failure_is_swallowed() -> None:
    client = FakeWebClient(error=RuntimeError("boom"))
    notifier = make_notifier(client, config())

    await notifier.refresh(root_task_id=1)

    assert len(client.calls) == 1
    assert notifier.enabled is True


@pytest.mark.asyncio(loop_scope="function")
async def test_debounce_merges_chains() -> None:
    client = FakeWebClient()
    notifier = make_notifier(client, config(debounce_seconds=0.05))

    for root_task_id in (1, 2, 3):
        await notifier.refresh(root_task_id=root_task_id)
    assert client.calls == []

    async with asyncio.timeout(5):
        while not client.calls:
            await asyncio.sleep(0.01)

    assert len(client.calls) == 1
    await notifier.aclose()


@pytest.mark.asyncio(loop_scope="function")
async def test_debounce_window_restarts_on_new_chain() -> None:
    """新链到来时重新计时: 窗口内持续有链完成则一直不刷新."""
    client = FakeWebClient()
    notifier = make_notifier(client, config(debounce_seconds=0.3))

    await notifier.refresh(root_task_id=1)
    await asyncio.sleep(0.2)
    await notifier.refresh(root_task_id=2)
    await asyncio.sleep(0.2)

    assert client.calls == []

    async with asyncio.timeout(5):
        while not client.calls:
            await asyncio.sleep(0.01)

    assert len(client.calls) == 1
    await notifier.aclose()


@pytest.mark.asyncio(loop_scope="function")
async def test_aclose_drops_pending_flush() -> None:
    client = FakeWebClient()
    notifier = make_notifier(client, config(debounce_seconds=60))

    await notifier.refresh(root_task_id=1)
    await notifier.aclose()
    await asyncio.sleep(0)

    assert client.calls == []


@pytest.mark.asyncio(loop_scope="function")
async def test_set_context_swaps_channel_and_keeps_dedupe() -> None:
    first = FakeWebClient()
    second = FakeWebClient()
    notifier = make_notifier(first, config())

    await notifier.refresh(root_task_id=1)

    notifier.set_context(web_client=cast("WebClient", second), config=config(enabled=False))
    await notifier.refresh(root_task_id=2)
    assert len(first.calls) == 1
    assert second.calls == []

    # 去重集合跨 set_context 保留: 换回可用配置也不重复通知同一条链.
    notifier.set_context(web_client=cast("WebClient", second), config=config())
    await notifier.refresh(root_task_id=1)
    await notifier.refresh(root_task_id=3)

    assert [len(first.calls), len(second.calls)] == [1, 1]
