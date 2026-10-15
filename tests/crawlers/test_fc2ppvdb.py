"""fc2ppvdb (fc2cmadb) 的 Inertia 页面解析与延迟演员请求."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from amane.crawlers.http import HttpClient
from amane.crawlers.models import SearchQuery
from amane.crawlers.sites.fc2ppvdb import (
    FC2PPVDBCrawler,
    extract_inertia_page,
    parse_inertia_article,
    parse_table_article,
)
from amane.net.errors import RequestError

URL = "https://fc2cmadb.com/articles/4930958"

_ARTICLE = {
    "title": "テスト作品 コメント(12)",
    "image_url": "https://contents-thumbnail2.fc2.com/w276/example.jpg",
    "release_date": "2026-07-01",
    "duration": "01:30:49",
    "video_id": "4930958",
    "actresses": [],
    "writer": {"name": "販売者"},
    "tags": [{"name": "タグA"}, {"name": "タグB"}],
}
_PAGE = {
    "component": "Articles/Show",
    "props": {"article": _ARTICLE},
    "version": "fcb3b524d4c7f8f3d2c38e437b35b7a9",
}


def _html(page: dict[str, Any]) -> str:
    return (
        "<!doctype html><html><body><h1>FC2CMADB</h1>"
        f'<script data-page="app" type="application/json">{json.dumps(page)}</script>'
        "</body></html>"
    )


def test_extract_and_parse_inertia_article() -> None:
    """标题尾部的评论计数要丢掉, 字段按 Inertia 结构取出."""
    page = extract_inertia_page(_html(_PAGE))
    assert page is not None

    info = parse_inertia_article(page)

    assert info is not None
    assert info["title"] == "テスト作品"
    assert info["video_id"] == "4930958"
    assert info["release"] == "2026-07-01"
    assert info["tags"] == ["タグA", "タグB"]
    assert info["studio"] == "販売者"
    assert info["image_url"] == _ARTICLE["image_url"]


def test_parse_inertia_article_without_title() -> None:
    """只有站名的页面不算命中."""
    assert parse_inertia_article({"props": {"article": {"title": "FC2CMADB"}}}) is None


def test_parse_table_article_fallback() -> None:
    """没有 Inertia 数据时退回表格解析, 且跳过演员头像图."""
    html = (
        "<html><head><title>1234 テスト 作品 - FC2CMADB</title></head><body>"
        "<h1>テスト 作品</h1><table>"
        "<tr><th>販売日</th><td>2026-07-01</td></tr>"
        "<tr><th>女優</th><td>高瀬佳澄, 別名</td></tr>"
        "<tr><th>タグ</th><td>タグA タグB</td></tr>"
        "<tr><th>収録時間</th><td>01:30:49</td></tr>"
        "<tr><th>販売者</th><td>販売者名</td></tr></table>"
        '<img src="/storage/images/actress/x.jpg"><img src="/storage/images/article/y.jpg">'
        "</body></html>"
    )

    info = parse_table_article(html)

    assert info is not None
    assert info["title"] == "テスト 作品"
    assert info["actors"] == ["高瀬佳澄", "別名"]
    assert info["tags"] == ["タグA", "タグB"]
    assert info["release"] == "2026-07-01"
    assert info["studio"] == "販売者名"
    assert info["image_url"] == "https://fc2cmadb.com/storage/images/article/y.jpg"


@pytest.mark.asyncio
async def test_scrape_fetches_deferred_actresses() -> None:
    """页面里没有演员时补一次带版本号的请求; 时长换算成分钟."""
    web = AsyncMock()
    web.get_text.return_value = _html(_PAGE)
    web.get_json.return_value = {"props": {"actresses": [{"name": "高瀬佳澄"}]}}
    crawler = FC2PPVDBCrawler(client=HttpClient(web=web))

    meta = await crawler._scrape(URL)

    assert meta is not None
    assert meta.number == "FC2-4930958"
    assert [actor.name for actor in meta.actors] == ["高瀬佳澄"]
    assert meta.runtime == 91
    assert meta.tags == ["タグA", "タグB"]
    assert meta.thumb_urls == [_ARTICLE["image_url"]]
    assert meta.release == "2026-07-01"
    headers = web.get_json.await_args.kwargs["headers"]
    assert headers["X-Inertia-Version"] == _PAGE["version"]
    assert headers["X-Inertia-Partial-Data"] == "actresses"
    assert headers["X-Inertia-Partial-Component"] == "Articles/Show"


@pytest.mark.asyncio
async def test_actress_failure_keeps_other_fields() -> None:
    """演员那一步失败只降级为空, 不影响已拿到的字段."""
    web = AsyncMock()
    web.get_text.return_value = _html(_PAGE)
    web.get_json.side_effect = RequestError(URL, "boom")
    crawler = FC2PPVDBCrawler(client=HttpClient(web=web))

    meta = await crawler._scrape(URL)

    assert meta is not None
    assert meta.title == "テスト作品"
    assert meta.actors == []


@pytest.mark.asyncio
async def test_search_requires_numeric_number() -> None:
    web = AsyncMock()
    crawler = FC2PPVDBCrawler(client=HttpClient(web=web))

    assert await crawler._search(SearchQuery("ABC-123")) is None
    web.get_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_hits_article_url() -> None:
    web = AsyncMock()
    web.get_text.return_value = _html(_PAGE)
    crawler = FC2PPVDBCrawler(client=HttpClient(web=web))

    assert await crawler._search(SearchQuery("FC2-PPV-4930958")) == URL
