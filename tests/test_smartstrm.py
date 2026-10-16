"""SmartStrm 远端删除通知: 路径映射, 同目录去重, 失败静默."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from amane.config import SmartStrmConfig
from amane.smartstrm import SmartStrmNotifier, rejection_message

if TYPE_CHECKING:
    from amane.net.http import WebClient


class FakeResponse:
    """curl_cffi Response 的最小投影: 只用到 text."""

    def __init__(self, text: str) -> None:
        self.text = text


class FakeWebClient:
    """记录出站请求; 可注入异常或响应体."""

    def __init__(self, error: Exception | None = None, body: str = "ok") -> None:
        self.calls: list[dict[str, Any]] = []
        self._error = error
        self._body = body

    async def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if self._error is not None:
            raise self._error
        return FakeResponse(self._body)


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
    # Title 是实测的必要字段, 且在顶层: 缺了会被判「处理 Webhook 失败: 'Title'」.
    assert call["json"] == {
        "Event": "library.deleted",
        "Title": "已删除了 ABC-123",
        "Item": {"Path": "/strm/ym/ABC-123/ABC-123.strm"},
    }
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


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # 已接受: 成功回执与读不懂的响应体都不该记成失败
        ("ok", None),
        ("", None),
        ("   ", None),
        ('{"success": true}', None),
        ('{"success": true, "message": "done"}', None),
        ("<html>502</html>", None),
        ("[1, 2]", None),
        # 拒绝: 状态码同样是 200, 只有响应体能区分
        ('{"success": false, "message": "处理 Webhook 失败: \'Title\'"}', "处理 Webhook 失败: 'Title'"),
        ('{"success": false}', "success=false"),
        ("ignored: emby 删除同步未启用", "ignored: emby 删除同步未启用"),
        ("IGNORED: 未启用", "IGNORED: 未启用"),
    ],
)
def test_rejection_message(body: str, expected: str | None) -> None:
    assert rejection_message(body) == expected


@pytest.mark.asyncio(loop_scope="function")
async def test_rejected_notification_is_not_an_exception() -> None:
    """被拒绝不能改任务终态: DELETE 的磁盘动作已经完成, 这里只负责留下日志."""
    client = FakeWebClient(body='{"success": false, "message": "处理 Webhook 失败: \'Title\'"}')
    notifier = make_notifier(client, config())

    await notifier.notify_deleted([Path("/media/ym/ABC-123/ABC-123.strm")])

    assert len(client.calls) == 1
