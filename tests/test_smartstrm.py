"""SmartStrm 远端删除通知: 路径映射, 同目录去重, 失败静默."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from amane.config import SmartStrmConfig
from amane.smartstrm import SmartStrmNotifier

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


def make_notifier(client: FakeWebClient, settings: SmartStrmConfig) -> SmartStrmNotifier:
    """结构替身只在类型检查处按生产类型收窄."""
    return SmartStrmNotifier(cast("WebClient", client), settings)


def config(**overrides: Any) -> SmartStrmConfig:
    values: dict[str, Any] = {
        "enabled": True,
        "webhook_url": "http://192.168.1.2:8024/webhook/token",
        **overrides,
    }
    return SmartStrmConfig(**values)


def notified(client: FakeWebClient) -> list[str]:
    return [call["json"]["Item"]["Path"] for call in client.calls]


@pytest.mark.asyncio(loop_scope="function")
async def test_disabled_sends_nothing() -> None:
    client = FakeWebClient()
    notifier = make_notifier(client, config(enabled=False))

    await notifier.notify_deleted([Path("/media/ym/ABC-123/ABC-123.strm")])

    assert notifier.enabled is False
    assert client.calls == []


@pytest.mark.asyncio(loop_scope="function")
async def test_blank_webhook_sends_nothing() -> None:
    client = FakeWebClient()
    notifier = make_notifier(client, config(webhook_url="  "))

    await notifier.notify_deleted([Path("/media/ym/ABC-123/ABC-123.strm")])

    assert notifier.enabled is False
    assert client.calls == []


@pytest.mark.asyncio(loop_scope="function")
async def test_payload_matches_emby_delete_contract() -> None:
    client = FakeWebClient()
    notifier = make_notifier(client, config())

    await notifier.notify_deleted([Path("/media/ym/ABC-123/ABC-123.strm")])

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://192.168.1.2:8024/webhook/token"
    assert call["json"] == {"Event": "library.deleted", "Item": {"Path": "/strm/ym/ABC-123/ABC-123.strm"}}
    assert call["use_proxy"] is False
    assert call["max_attempts"] == 1


@pytest.mark.parametrize(
    ("local_root", "emby_root", "path", "expected"),
    [
        ("/media", "/strm", "/media/ym/ABC-123/ABC-123.strm", ["/strm/ym/ABC-123/ABC-123.strm"]),
        # 末尾斜杠不产生空路径分量
        ("/media", "/strm/", "/media/ym/ABC-123/ABC-123.strm", ["/strm/ym/ABC-123/ABC-123.strm"]),
        # 库直接挂在挂载根下
        ("/media", "/strm", "/media/ABC-123.strm", ["/strm/ABC-123.strm"]),
        # 挂载根也可以更深, 两侧各自成段替换
        ("/media/ym", "/strm/ym", "/media/ym/x/y.strm", ["/strm/ym/x/y.strm"]),
        # 路径比较按分量: /media2 不是 /media 的子路径
        ("/media", "/strm", "/media2/ABC-123.strm", []),
        ("/media", "/strm", "/data/ym/ABC-123.strm", []),
    ],
)
@pytest.mark.asyncio(loop_scope="function")
async def test_maps_local_root_to_emby_root(local_root: str, emby_root: str, path: str, expected: list[str]) -> None:
    client = FakeWebClient()
    notifier = make_notifier(client, config(local_root=local_root, emby_root=emby_root))

    await notifier.notify_deleted([Path(path)])

    assert notified(client) == expected


@pytest.mark.asyncio(loop_scope="function")
async def test_same_directory_notified_once() -> None:
    """多集结号共用一个影片目录: 同目录只投递一次, 不同目录各自投递."""
    client = FakeWebClient()
    notifier = make_notifier(client, config())

    await notifier.notify_deleted(
        [
            Path("/media/ym/ABC-123/ABC-123-A.strm"),
            Path("/media/ym/ABC-123/ABC-123-B.strm"),
            Path("/media/ym/ABC-123/cover.jpg"),
            Path("/media/ym/DEF-456/DEF-456.strm"),
        ]
    )

    assert notified(client) == [
        "/strm/ym/ABC-123/ABC-123-A.strm",
        "/strm/ym/DEF-456/DEF-456.strm",
    ]


@pytest.mark.asyncio(loop_scope="function")
async def test_unmapped_paths_do_not_block_mapped_ones() -> None:
    client = FakeWebClient()
    notifier = make_notifier(client, config())

    await notifier.notify_deleted([Path("/data/ym/out.strm"), Path("/media/ym/in.strm")])

    assert notified(client) == ["/strm/ym/in.strm"]


@pytest.mark.asyncio(loop_scope="function")
async def test_request_failure_is_swallowed() -> None:
    client = FakeWebClient(error=RuntimeError("boom"))
    notifier = make_notifier(client, config())

    await notifier.notify_deleted([Path("/media/ym/ABC-123/ABC-123.strm")])

    assert len(client.calls) == 1


@pytest.mark.asyncio(loop_scope="function")
async def test_no_paths_sends_nothing() -> None:
    client = FakeWebClient()
    notifier = make_notifier(client, config())

    await notifier.notify_deleted([])

    assert client.calls == []
