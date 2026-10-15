"""无码官网 (Caribbeancom / HEYZO / 1Pondo / Pacopacomama / 10Musume) 刮削.

路由与解析规则移植自 mdcx 的 ``crawlers/official_uncensored.py``: 番号先路由到站点,
Caribbeancom 与 HEYZO 解析详情页 HTML, 其余三家读官方的
``/dyn/phpauto/movie_details/movie_id/<id>.json``. 番号形态 (``010115-001`` /
``010115_001`` / ``HEYZO-1234``) 在 ``parsing.file_info`` 里已判为无码, 这里只负责取回元数据.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

from parsel import Selector

from ...enums import SiteName
from ..base import Crawler, CrawlerProfile
from ..models import FetchOptions, MediaMetadata, SearchQuery, film_actors

UncensoredOfficialSite = Literal["caribbeancom", "heyzo", "1pondo", "pacopacomama", "10musume"]


@dataclass(frozen=True)
class UncensoredOfficialSpec:
    base_url: str
    studio: str
    sample_base_url: str = ""


SITES: dict[UncensoredOfficialSite, UncensoredOfficialSpec] = {
    "caribbeancom": UncensoredOfficialSpec(
        base_url="https://www.caribbeancom.com",
        studio="Caribbeancom",
        sample_base_url="https://smovie.caribbeancom.com",
    ),
    "heyzo": UncensoredOfficialSpec(base_url="https://www.heyzo.com", studio="HEYZO"),
    "1pondo": UncensoredOfficialSpec(
        base_url="https://www.1pondo.tv",
        studio="1Pondo",
        sample_base_url="https://smovie.1pondo.tv",
    ),
    "pacopacomama": UncensoredOfficialSpec(
        base_url="https://www.pacopacomama.com",
        studio="Pacopacomama",
        sample_base_url="https://smovie.pacopacomama.com",
    ),
    "10musume": UncensoredOfficialSpec(
        base_url="https://www.10musume.com",
        studio="10Musume",
        sample_base_url="https://smovie.10musume.com",
    ),
}

# 番号前缀别名 → 站点. Caribbeancom 与 1Pondo 系列也能靠后缀分隔符区分, 两种写法都认.
PREFIX_ALIASES: dict[str, UncensoredOfficialSite] = {
    "1pon": "1pondo",
    "1pondo": "1pondo",
    "10mu": "10musume",
    "10musume": "10musume",
    "carib": "caribbeancom",
    "caribbeancom": "caribbeancom",
    "caribbeancompr": "caribbeancom",
    "paco": "pacopacomama",
    "pacoma": "pacopacomama",
    "pacopacomama": "pacopacomama",
}

_SITE_ALIASES = "|".join(sorted(PREFIX_ALIASES, key=len, reverse=True))
DIGIT_NUMBER = re.compile(r"^(?P<head>\d{6})(?P<sep>[-_])(?P<tail>\d{2,4})$")
PREFIXED_DIGIT_NUMBER = re.compile(
    rf"^(?P<prefix>{_SITE_ALIASES})[-_ ]*(?P<head>\d{{6}})(?P<sep>[-_])(?P<tail>\d{{2,4}})$",
    re.IGNORECASE,
)
HEYZO_NUMBER = re.compile(r"^heyzo[-_ ]*(?P<id>\d{3,})$", re.IGNORECASE)
# 详情页 URL 里的 id: 三家 JSON 站是 ``/movies/<id>/``, Caribbeancom / HEYZO 是 ``/moviepages/<id>/``.
_MOVIE_ID_IN_URL = re.compile(r"/(?:moviepages|movies)/(?P<id>[^/]+)")


def _collapse(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def _split_names(value: object) -> list[str]:
    if isinstance(value, (list, tuple)):
        names: list[str] = []
        for item in value:
            names.extend(_split_names(item))
        return _dedupe(names)
    text = _collapse(value)
    if not text:
        return []
    return _dedupe([part for part in (_collapse(part) for part in re.split(r"[,/|、，\n\r]+", text)) if part])


def _split_tags(value: object) -> list[str]:
    if isinstance(value, list):
        return _dedupe([_collapse(item) for item in value])
    return _split_names(value)


def _normalize_release(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if match := re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text):
        year, month, day = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    return text[:10]


def _seconds_to_minutes(value: object) -> int | None:
    try:
        seconds = int(float(str(value).strip()))
    except TypeError, ValueError:
        return None
    return max(1, seconds // 60) if seconds > 0 else None


def _hms_to_minutes(value: object) -> int | None:
    match = re.search(r"(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?", str(value or "").strip())
    if match is None:
        return None
    parts = [int(part or 0) for part in match.groups()]
    hours, minutes, seconds = (0, parts[0], parts[1]) if match.group(3) is None else tuple(parts)
    total = hours * 60 + minutes
    return total or (1 if seconds > 0 else None)


def _iso8601_to_minutes(value: object) -> int | None:
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?",
        str(value or "").strip(),
        re.IGNORECASE,
    )
    if match is None:
        return None
    total = (
        int(match.group("days") or 0) * 24 * 60 + int(match.group("hours") or 0) * 60 + int(match.group("minutes") or 0)
    )
    return total or (1 if int(match.group("seconds") or 0) > 0 else None)


def split_edge_marker(value: str) -> tuple[str | None, str]:
    """剥掉写在番号前/后的站点名 (``10MU-101526_01`` / ``101526_01-10MU``).

    返回 (站点别名, 余下番号); 前后都写时以更靠近编号的那次为准.
    """
    out = value
    marker: str | None = None
    if m := re.match(rf"^({_SITE_ALIASES})[-_ ]+", out, re.IGNORECASE):
        marker, out = m.group(1).lower(), out[m.end() :]
    if m := re.search(rf"[-_ ]+({_SITE_ALIASES})$", out, re.IGNORECASE):
        marker, out = m.group(1).lower(), out[: m.start()]
    return marker, out


def route_site(number: str) -> UncensoredOfficialSite | None:
    """番号 → 站点; 认不出返回 ``None`` (交给别的来源)."""
    value = (number or "").strip().lower().replace(" ", "")
    if not value:
        return None
    if HEYZO_NUMBER.fullmatch(value):
        return "heyzo"
    # 写了站点名就以它为准: 只看余下番号的分隔符会把 ``101526_01-paco`` 判成 10Musume.
    marker, rest = split_edge_marker(value)
    if marker is not None and DIGIT_NUMBER.fullmatch(rest):
        return PREFIX_ALIASES[marker]
    if prefixed := PREFIXED_DIGIT_NUMBER.fullmatch(value):
        return PREFIX_ALIASES[prefixed.group("prefix").lower()]
    if bare := DIGIT_NUMBER.fullmatch(rest):
        # 没有站点名时按分隔符归属: Caribbeancom 用 '-', 1Pondo 系列用 '_'.
        if bare.group("sep") == "-":
            return "caribbeancom"
        tail = bare.group("tail")
        if len(tail) == 2:
            return "10musume"
        return "pacopacomama" if int(tail) >= 100 else "1pondo"
    return None


def movie_id(number: str) -> str:
    """番号 → 官网 movie_id (保留原分隔符); 认不出时退回去掉首尾符号的原文."""
    value = (number or "").strip().lower().replace(" ", "")
    _marker, value = split_edge_marker(value)
    for pattern in (HEYZO_NUMBER, DIGIT_NUMBER, PREFIXED_DIGIT_NUMBER):
        if match := pattern.fullmatch(value):
            if "id" in match.groupdict():
                return match.group("id")
            return f"{match.group('head')}{match.group('sep')}{match.group('tail')}"
    return value.strip("-_. ")


def detail_url(site: UncensoredOfficialSite, movie: str) -> str:
    base = SITES[site].base_url
    if site in {"caribbeancom", "heyzo"}:
        return f"{base}/moviepages/{movie}/index.html"
    return f"{base}/movies/{movie}/"


def json_url(site: UncensoredOfficialSite, movie: str) -> str:
    return f"{SITES[site].base_url}/dyn/phpauto/movie_details/movie_id/{movie}.json"


def _site_of_url(url: str) -> tuple[UncensoredOfficialSite, str] | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    for site, spec in SITES.items():
        if host == (urlparse(spec.base_url).hostname or "").lower():
            match = _MOVIE_ID_IN_URL.search(parsed.path)
            return site, match.group("id") if match else ""
    return None


class OfficialUncensoredCrawler(Crawler):
    """五家无码官网共用; 站点由番号形态决定, 解析分 HTML 与 JSON 两条路径."""

    @classmethod
    def profile(cls) -> CrawlerProfile:
        return CrawlerProfile(name=SiteName.OFFICIAL_UNCENSORED, base_url=SITES["1pondo"].base_url)

    async def _search(self, query: SearchQuery, options: FetchOptions | None = None) -> str | None:
        site = route_site(query.number)
        if site is None:
            return None
        movie = movie_id(query.number)
        if not movie:
            return None
        url = detail_url(site, movie)
        meta = await self._parse(site, movie, url)
        return url if meta is not None else None

    async def _scrape(self, url: str, options: FetchOptions | None = None) -> MediaMetadata | None:
        found = _site_of_url(url)
        if found is None:
            return None
        site, movie = found
        if not movie:
            return None
        return await self._parse(site, movie, url)

    async def _parse(self, site: UncensoredOfficialSite, movie: str, url: str) -> MediaMetadata | None:
        if site == "caribbeancom":
            return await self._parse_caribbeancom(movie, url)
        if site == "heyzo":
            return await self._parse_heyzo(movie, url)
        return await self._parse_json_site(site, movie, url)

    async def _parse_caribbeancom(self, movie: str, url: str) -> MediaMetadata | None:
        text = await self.client.get_text(url, encoding="euc-jp")
        if not text:
            return None
        html = Selector(text=text)
        spec = SITES["caribbeancom"]
        title = _first(html, '//*[@itemprop="name"]/text()', "//h1/text()")
        if not title:
            return None
        release = _normalize_release(_spec_value(html, {"配信日", "発売日"}))
        thumb = urljoin(spec.base_url, f"/moviepages/{movie}/images/l_l.jpg")
        extrafanart = [
            candidate
            for candidate in _absolute_all(
                spec.base_url,
                html.xpath("//a[contains(@href, '/moviepages/') and contains(@href, '/images/l/')]/@href").getall(),
            )
            if candidate != thumb and "/member/" not in candidate
        ]
        return MediaMetadata(
            number=movie,
            title=title,
            actors=film_actors(_split_names(_spec_value(html, {"出演", "女優"}))),
            studio=_spec_value(html, {"メーカー", "レーベル"}) or spec.studio,
            release=release or None,
            runtime=_hms_to_minutes(_spec_value(html, {"再生時間", "収録時間"})),
            tags=_split_tags(_spec_value(html, {"タグ"})),
            series=_spec_value(html, {"シリーズ"}) or None,
            plot=_first(
                html,
                '//*[@itemprop="description"]',
                '//meta[@name="description"]/@content',
                '//meta[@property="og:description"]/@content',
            )
            or None,
            thumb_urls=[thumb],
            trailer_urls=[f"{spec.sample_base_url}/sample/movies/{movie}/sample.mp4"],
            extrafanart=extrafanart,
            external_id=url,
        )

    async def _parse_heyzo(self, movie: str, url: str) -> MediaMetadata | None:
        text = await self.client.get_html(url)
        if not text:
            return None
        html = Selector(text=text)
        spec = SITES["heyzo"]
        data = _json_ld(html)
        title = _collapse(data.get("name")) or _first(html, '//meta[@property="og:title"]/@content', "//h1/text()")
        if not title:
            return None
        thumb = _protocol_url(data.get("image") or _first(html, '//meta[@property="og:image"]/@content'), spec.base_url)
        trailer = ""
        if isinstance(data.get("video"), dict):
            trailer = _protocol_url(data["video"].get("contentUrl"), spec.base_url)
        if not trailer:
            trailer = f"https://sample.heyzo.com/contents/{int(movie) // 1000 * 1000}/{movie}/sample.mp4"
        tags = _dedupe(
            [
                *(
                    _clean_text(value)
                    for value in html.xpath(
                        "//a[contains(@href, 'category_') and not(contains(@class, 'actor'))]/text()"
                    ).getall()
                ),
                *(_clean_text(value) for value in html.xpath("//span[contains(@class, 'tag')]/text()").getall()),
            ]
        )
        return MediaMetadata(
            number=f"HEYZO-{movie}",
            title=title,
            actors=film_actors(_json_ld_actors(data.get("actor"))),
            studio=spec.studio,
            release=_normalize_release(data.get("dateCreated")) or None,
            runtime=_iso8601_to_minutes(data.get("duration")),
            tags=[tag for tag in tags if tag],
            plot=_collapse(data.get("description")) or _first(html, '//meta[@name="description"]/@content') or None,
            thumb_urls=[thumb] if thumb else [],
            trailer_urls=[trailer] if trailer else [],
            external_id=url,
        )

    async def _parse_json_site(self, site: UncensoredOfficialSite, movie: str, url: str) -> MediaMetadata | None:
        spec = SITES[site]
        data = await self.client.get_json(json_url(site, movie))
        if not isinstance(data, dict):
            return None
        title = _json_scalar(data, "Title", "MovieTitle")
        if not title:
            return None
        thumb, poster, extrafanart = _json_images(data)
        series = _json_scalar(data, "Series", "UCNAME")
        studio = _json_scalar(data, "Maker", "Studio")
        # 新版 1Pondo / 10Musume / Paco 把 UCNAME 用作题材列表, 旧版是厂商标量名.
        if not studio:
            studio = _json_scalar(data, "UCNAME") or spec.studio
        trailer = _json_scalar(data, "SampleMovie", "SampleMovieHigh", "MovieSample")
        if not trailer and spec.sample_base_url:
            trailer = f"{spec.sample_base_url}/sample/movies/{movie}/sample.mp4"
        tags = _json_value(data, "Tag", "Tags", "Genre", "Genres")
        if not tags and isinstance(data.get("UCNAME"), list):
            tags = data["UCNAME"]
        return MediaMetadata(
            number=movie,
            title=title,
            actors=film_actors(_split_names(_json_value(data, "ActressesJa", "Actor", "Actors", "Actresses"))),
            studio=studio,
            release=_normalize_release(_json_value(data, "Release", "ReleaseDate", "Year")) or None,
            runtime=_seconds_to_minutes(_json_value(data, "Duration", "Runtime")),
            tags=_split_tags(tags),
            series=series or None,
            plot=_collapse(_json_value(data, "Desc", "Description")) or None,
            thumb_urls=[thumb] if thumb else [],
            poster_urls=[poster] if poster else [],
            trailer_urls=[trailer] if trailer else [],
            extrafanart=extrafanart,
            external_id=url,
        )


def _clean_text(value: object) -> str:
    return _collapse(value)


def _node_text(node: Selector) -> str:
    """取值文本: 属性选择 (/@href) 直接是字符串, 元素选择要取 string() 而非整个标签."""
    root = node.root
    if isinstance(root, str):
        return _clean_text(root)
    return _clean_text(node.xpath("string()").get() or "")


def _first(html: Selector, *xpaths: str) -> str:
    for xpath in xpaths:
        for node in html.xpath(xpath):
            if text := _node_text(node):
                return text
    return ""


def _spec_value(html: Selector, labels: set[str]) -> str:
    """Caribbeancom 详情页的 ``<li class="movie-spec">`` 表格取值."""
    for row in html.xpath("//li[contains(concat(' ', normalize-space(@class), ' '), ' movie-spec ')]"):
        label = _first(row, ".//*[contains(concat(' ', normalize-space(@class), ' '), ' spec-title ')]")
        if label.rstrip("：:").strip() in labels:
            return _first(row, ".//*[contains(concat(' ', normalize-space(@class), ' '), ' spec-content ')]")
    return ""


def _absolute_all(base: str, values: list[str]) -> list[str]:
    return _dedupe([urljoin(base, value) for value in values if value])


def _json_ld(html: Selector) -> dict[str, Any]:
    for raw in html.xpath('//script[@type="application/ld+json"]/text()').getall():
        try:
            data = json.loads(raw)
        except TypeError, json.JSONDecodeError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == "Movie":
                return item
    return {}


def _json_ld_actors(value: object) -> list[str]:
    if isinstance(value, dict):
        return _split_names(value.get("name"))
    if isinstance(value, list):
        names: list[str] = []
        for item in value:
            names.extend(_split_names(item.get("name") if isinstance(item, dict) else item))
        return _dedupe(names)
    return _split_names(value)


def _protocol_url(value: object, base: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return "https:" + text if text.startswith("//") else urljoin(base, text)


def _json_value(data: dict[str, Any], *keys: str) -> object:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return ""


def _json_scalar(data: dict[str, Any], *keys: str) -> str:
    """取标量文本; 结构化值不当字符串用 (新版 UCNAME 是列表)."""
    value = _json_value(data, *keys)
    if isinstance(value, (dict, list, tuple, set)):
        return ""
    return _clean_text(value)


def _json_images(data: dict[str, Any]) -> tuple[str, str, list[str]]:
    thumb = str(_json_value(data, "ThumbHigh", "MovieThumb", "Thumb", "SampleThumb")).strip()
    poster = str(_json_value(data, "Poster", "Jacket", "Thumb")).strip()
    raw = _json_value(data, "Gallery", "GalleryImages", "SampleImages")
    extrafanart: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                extrafanart.extend(str(value).strip() for value in item.values() if str(value).strip())
            elif str(item).strip():
                extrafanart.append(str(item).strip())
    return thumb, poster, _dedupe(extrafanart)
