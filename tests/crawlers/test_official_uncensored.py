"""无码官网 (Caribbeancom / HEYZO / 1Pondo / Pacopacomama / 10Musume) 路由与解析."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from amane.crawlers.http import HttpClient
from amane.crawlers.models import SearchQuery
from amane.crawlers.sites.official_uncensored import (
    OfficialUncensoredCrawler,
    detail_url,
    json_url,
    movie_id,
    route_site,
)


@pytest.mark.parametrize(
    ("number", "site", "expected_id"),
    [
        ("010115-001", "caribbeancom", "010115-001"),
        ("010115_001", "1pondo", "010115_001"),
        ("010115_02", "10musume", "010115_02"),
        ("010115_123", "pacopacomama", "010115_123"),
        ("HEYZO-1234", "heyzo", "1234"),
        ("HEYZO1234", "heyzo", "1234"),
        ("1pondo_010115_001", "1pondo", "010115_001"),
        ("10mu-010115-02", "10musume", "010115-02"),
        ("carib-010115-001", "caribbeancom", "010115-001"),
        ("paco_010115_123", "pacopacomama", "010115_123"),
        # 站点名写在番号后面 (存量索引里的形态) 也要认得
        ("101526_01-10MU", "10musume", "101526_01"),
        ("10MU-101526_01", "10musume", "101526_01"),
        ("101526_01-paco", "pacopacomama", "101526_01"),
        ("093024_001-1pondo", "1pondo", "093024_001"),
        ("121315-001-carib", "caribbeancom", "121315-001"),
        ("10MU-101526_01-10MU", "10musume", "101526_01"),
        ("SSIS-001", None, "ssis-001"),
        ("FC2-1234567", None, "fc2-1234567"),
    ],
)
def test_route_site_and_movie_id(number: str, site: str | None, expected_id: str) -> None:
    """无前缀时按分隔符归属 ('-' 加勒比, '_' 按尾号长度分三家), 前缀别名一并认."""
    assert route_site(number) == site
    assert movie_id(number) == expected_id


def test_urls() -> None:
    assert detail_url("caribbeancom", "010115-001") == "https://www.caribbeancom.com/moviepages/010115-001/index.html"
    assert detail_url("10musume", "010115_02") == "https://www.10musume.com/movies/010115_02/"
    assert json_url("1pondo", "010115_001") == (
        "https://www.1pondo.tv/dyn/phpauto/movie_details/movie_id/010115_001.json"
    )


_JSON_PAYLOAD: dict[str, Any] = {
    "Title": "テスト作品",
    "ActressesJa": "女優A, 女優B",
    "Release": "2026-07-01",
    "Duration": 5430,
    "ThumbHigh": "https://img/1.jpg",
    "Poster": "https://img/p.jpg",
    "Gallery": [{"High": "https://img/g1.jpg"}, {"High": "https://img/g2.jpg"}],
    "UCNAME": ["タグA", "タグB"],
    "Desc": "あらすじ",
    "SampleMovie": "https://smovie/sample.mp4",
}


@pytest.mark.asyncio
async def test_json_site_scrape_uses_ucname_as_tags() -> None:
    """新版 1Pondo 系把 UCNAME 用作题材列表: 不能当成厂商标量名, 也不能丢标签."""
    web = AsyncMock()
    web.get_json.return_value = _JSON_PAYLOAD
    crawler = OfficialUncensoredCrawler(client=HttpClient(web=web))

    meta = await crawler._scrape(detail_url("1pondo", "010115_001"))

    assert meta is not None
    assert meta.number == "010115_001"
    assert meta.title == "テスト作品"
    assert [actor.name for actor in meta.actors] == ["女優A", "女優B"]
    assert meta.tags == ["タグA", "タグB"]
    assert meta.studio == "1Pondo"
    assert meta.release == "2026-07-01"
    assert meta.runtime == 90
    assert meta.plot == "あらすじ"
    assert meta.thumb_urls == ["https://img/1.jpg"]
    assert meta.poster_urls == ["https://img/p.jpg"]
    assert meta.extrafanart == ["https://img/g1.jpg", "https://img/g2.jpg"]
    assert meta.trailer_urls == ["https://smovie/sample.mp4"]


@pytest.mark.asyncio
async def test_search_validates_through_parser() -> None:
    """搜索阶段要真的取回数据才算命中, 认不出的番号直接放弃."""
    web = AsyncMock()
    web.get_json.return_value = _JSON_PAYLOAD
    crawler = OfficialUncensoredCrawler(client=HttpClient(web=web))

    assert await crawler._search(SearchQuery(number="010115_001")) == detail_url("1pondo", "010115_001")
    assert await crawler._search(SearchQuery(number="SSIS-001")) is None
    assert web.get_json.await_count == 1


_CARIBBEAN_HTML = """
<html><body>
<h1 itemprop="name">カリビアン作品</h1>
<ul>
  <li class="movie-spec"><span class="spec-title">出演</span><span class="spec-content">女優A, 女優B</span></li>
  <li class="movie-spec"><span class="spec-title">配信日</span><span class="spec-content">2026/07/01</span></li>
  <li class="movie-spec"><span class="spec-title">再生時間</span><span class="spec-content">01:30:30</span></li>
  <li class="movie-spec"><span class="spec-title">タグ</span><span class="spec-content">タグA タグB</span></li>
  <li class="movie-spec"><span class="spec-title">メーカー</span><span class="spec-content">カリビアン</span></li>
</ul>
<meta itemprop="description" content="あらすじ">
<a href="/moviepages/010115-001/images/l/1.jpg">g</a>
<a href="/member/only.jpg">member</a>
</body></html>
"""


@pytest.mark.asyncio
async def test_caribbeancom_scrape_reads_spec_table() -> None:
    web = AsyncMock()
    web.get_text.return_value = _CARIBBEAN_HTML
    crawler = OfficialUncensoredCrawler(client=HttpClient(web=web))

    meta = await crawler._scrape(detail_url("caribbeancom", "010115-001"))

    assert meta is not None
    assert meta.title == "カリビアン作品"
    assert [actor.name for actor in meta.actors] == ["女優A", "女優B"]
    assert meta.release == "2026-07-01"
    assert meta.runtime == 90
    assert meta.studio == "カリビアン"
    assert meta.thumb_urls == ["https://www.caribbeancom.com/moviepages/010115-001/images/l_l.jpg"]
    assert meta.extrafanart == ["https://www.caribbeancom.com/moviepages/010115-001/images/l/1.jpg"]
    assert meta.trailer_urls == ["https://smovie.caribbeancom.com/sample/movies/010115-001/sample.mp4"]


def _heyzo_html(ld: dict[str, Any]) -> str:
    return (
        "<html><head>"
        f'<script type="application/ld+json">{json.dumps(ld)}</script>'
        '</head><body><span class="tag">タグA</span></body></html>'
    )


@pytest.mark.asyncio
async def test_heyzo_scrape_reads_json_ld_and_falls_back_to_sample_url() -> None:
    """JSON-LD 里没有 video.contentUrl 时, 预告片按官方 sample 路径拼."""
    web = AsyncMock()
    web.get_text.return_value = _heyzo_html(
        {
            "@type": "Movie",
            "name": "HEYZO 作品",
            "actor": [{"name": "女優A"}],
            "dateCreated": "2026-07-01T00:00:00",
            "duration": "PT1H30M",
            "image": "//img.heyzo.com/1.jpg",
            "description": "あらすじ",
        }
    )
    crawler = OfficialUncensoredCrawler(client=HttpClient(web=web))

    meta = await crawler._scrape(detail_url("heyzo", "1234"))

    assert meta is not None
    assert meta.number == "HEYZO-1234"
    assert meta.title == "HEYZO 作品"
    assert [actor.name for actor in meta.actors] == ["女優A"]
    assert meta.runtime == 90
    assert meta.thumb_urls == ["https://img.heyzo.com/1.jpg"]
    assert meta.tags == ["タグA"]
    assert meta.trailer_urls == ["https://sample.heyzo.com/contents/1000/1234/sample.mp4"]


@pytest.mark.asyncio
async def test_scrape_unknown_host_is_ignored() -> None:
    crawler = OfficialUncensoredCrawler(client=HttpClient(web=AsyncMock()))
    assert await crawler._scrape("https://example.com/movies/010115_001/") is None
