"""fc2cmadb (原 fc2ppvdb) 影片信息: Inertia 页面 JSON 为主, 表格 HTML 为兜底.

站点改域后正文放进 ``<script data-page="app" type="application/json">``, ``h1.title`` 那套选择器
已不存在, 所以这里读 JSON; 演员不在页面里, 是 Inertia 的延迟属性, 要带 ``X-Inertia-Version``
再请求一次详情页 (``_fetch_actresses``) 才有. 旧域名 fc2ppvdb.com 被 Cloudflare 拦成 403,
``base_url`` 因此指向新域名.
"""

from __future__ import annotations

import html as html_module
import json
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urljoin

from parsel import Selector

from ...enums import SiteName
from ...net.errors import RequestError
from ..base import Crawler, CrawlerProfile
from ..models import FetchOptions, MediaMetadata, SearchQuery, film_actors

BASE_URL = "https://fc2cmadb.com"
APP_NAME = "FC2CMADB"

_INERTIA_SCRIPT = re.compile(r'<script[^>]*data-page="app"[^>]*>(.*?)</script>', re.DOTALL)
_ARTICLE_NUMBER = re.compile(r"/articles/(\d+)")
_COMMENT_SUFFIX = re.compile(r"\s*(?:コメント|评论|評論)\s*\(\d+\)\s*$")
_NUMBER_PREFIX = re.compile(r"(?i)fc2[-_]?(?:ppv[-_]?)?")


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _clean_title(value: Any) -> str:
    """去掉标题尾部的评论计数 (站点会把「コメント(12)」拼在标题后)."""
    return _COMMENT_SUFFIX.sub("", _clean(value)).strip()


def _absolute(value: Any) -> str:
    text = _clean(value)
    if not text or "no-image" in text:
        return ""
    return urljoin(BASE_URL + "/", text)


def _names(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for item in values:
        name = _clean(item.get("name")) if isinstance(item, dict) else _clean(item)
        if name:
            out.append(name)
    return out


def _runtime_minutes(value: Any) -> int | None:
    """``01:30:49`` → 91 分钟; 非法值返回 None (MediaMetadata.runtime 以分钟计)."""
    parts = _clean(value).split(":")
    if not parts or not all(part.isdigit() for part in parts):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return round(seconds / 60) if seconds else None


def extract_inertia_page(text: str) -> dict[str, Any] | None:
    """取 ``data-page="app"`` 的 JSON; 解析失败或结构不是对象时返回 None."""
    for match in _INERTIA_SCRIPT.finditer(text):
        try:
            page = json.loads(html_module.unescape(match.group(1)))
        except ValueError:
            continue
        if isinstance(page, dict):
            return page
    return None


def parse_inertia_article(page: Mapping[str, Any]) -> dict[str, Any] | None:
    """Inertia ``props.article`` → 与表格兜底同形的中间结构; 取不到标题时 None."""
    props = page.get("props")
    article = props.get("article") if isinstance(props, Mapping) else None
    if not isinstance(article, Mapping):
        return None
    title = _clean_title(article.get("title"))
    if not title or title == APP_NAME:
        return None
    writer = article.get("writer")
    return {
        "title": title,
        "image_url": _absolute(article.get("image_url")),
        "release": _clean(article.get("release_date")),
        "actors": _names(article.get("actresses")),
        "tags": _names(article.get("tags")),
        "studio": _clean(writer.get("name")) if isinstance(writer, Mapping) else "",
        "duration": _clean(article.get("duration")),
        "video_id": _clean(article.get("video_id")),
    }


def parse_table_article(text: str) -> dict[str, Any] | None:
    """兜底: 表格式 HTML (女優 / タグ / 販売日 / 収録時間 / 販売者)."""
    if APP_NAME not in text:
        return None
    doc = Selector(text=text)
    title = _clean_title(" ".join(doc.xpath("//h1[1]//text()").getall()))
    if not title:
        page_title = _clean(" ".join(doc.xpath("//title//text()").getall()))
        stripped = re.sub(r"^\s*\d+\s*", "", page_title)
        title = _clean_title(re.sub(r"\s*作品\s*-\s*FC2CMADB\s*$", "", stripped))
    if not title or title == APP_NAME:
        return None

    table: dict[str, str] = {}
    for row in doc.xpath("//tr"):
        cells = [_clean(" ".join(cell.xpath(".//text()").getall())) for cell in row.xpath("./th|./td")]
        cells = [cell for cell in cells if cell]
        if len(cells) >= 2:
            table[cells[0]] = cells[1]

    image = ""
    for src in doc.xpath("//img/@src").getall():
        candidate = _absolute(src)
        if candidate and "/storage/images/actress/" not in candidate:
            image = candidate
            break

    return {
        "title": title,
        "image_url": image,
        "release": table.get("販売日", ""),
        "actors": [name for name in re.split(r"[,、/，]\s*|\s+", table.get("女優", "")) if name],
        "tags": [tag for tag in table.get("タグ", "").split() if tag],
        "studio": table.get("販売者") or table.get("卖家") or table.get("販売者名") or "",
        "duration": table.get("収録時間", ""),
        "video_id": "",
    }


class FC2PPVDBCrawler(Crawler):
    @classmethod
    def profile(cls) -> CrawlerProfile:
        return CrawlerProfile(name=SiteName.FC2PPVDB, base_url=BASE_URL)

    async def _search(self, query: SearchQuery, options: FetchOptions | None = None) -> str | None:
        num = _NUMBER_PREFIX.sub("", query.number).strip()
        if not num.isdigit():
            return None
        url = f"{self.base_url}/articles/{num}"
        text = await self.client.get_html(url, cookies=self.cookies)
        if not text:
            return None
        page = extract_inertia_page(text)
        if (page is not None and parse_inertia_article(page) is not None) or parse_table_article(text) is not None:
            return url
        return None

    async def _scrape(self, url: str, options: FetchOptions | None = None) -> MediaMetadata | None:
        text = await self.client.get_html(url, cookies=self.cookies)
        if not text:
            return None

        page = extract_inertia_page(text)
        info = parse_inertia_article(page) if page else None
        if info is None:
            # 表格兜底没有 Inertia 页面可用来补演员.
            info = parse_table_article(text)
            page = None
        if info is None:
            return None

        actors = info["actors"]
        if not actors and page is not None:
            actors = await self._fetch_actresses(url, page)

        match = _ARTICLE_NUMBER.search(url)
        number = info["video_id"] or (match.group(1) if match else "")
        if not number:
            return None

        return MediaMetadata(
            number=f"FC2-{number}",
            title=info["title"],
            actors=film_actors(actors),
            studio=info["studio"] or None,
            release=info["release"] or None,
            runtime=_runtime_minutes(info["duration"]),
            tags=info["tags"],
            thumb_urls=[info["image_url"]] if info["image_url"] else [],
            source_url=url,
        )

    async def _fetch_actresses(self, url: str, page: Mapping[str, Any]) -> list[str]:
        """演员是延迟属性: 带版本号再请求一次详情页, 只要 ``actresses``.

        这一步失败只降级为没有演员, 不让整个来源失败 (主要字段页面里已经有了).
        """
        headers = {
            "Accept": "text/html, application/xhtml+xml",
            "X-Inertia": "true",
            "X-Inertia-Partial-Data": "actresses",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": url,
        }
        component = _clean(page.get("component"))
        version = _clean(page.get("version"))
        if component:
            headers["X-Inertia-Partial-Component"] = component
        if version:
            headers["X-Inertia-Version"] = version

        try:
            data = await self.client.get_json(url, headers=headers, cookies=self.cookies)
        except RequestError as exc:
            self.logger.warning("actress lookup failed", url=url, error=str(exc))
            return []
        props = data.get("props") if isinstance(data, dict) else None
        return _names(props.get("actresses")) if isinstance(props, dict) else []
