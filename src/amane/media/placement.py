"""上传图片立即落到库路径: 按库的 thumb / poster 模板解析并覆盖同名文件.

与整理 (``handlers/file.py`` 的 ``_copy_resources``) 的目标一致, 区别只是源是刚上传的字节:
整理是整批产物, 这里只换用户点名的那一张.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ..organize import resolve_paths
from ..parsing import parse_file_info

if TYPE_CHECKING:
    from ..db.models import Metadata
    from ..db.repository import Repository

logger = structlog.get_logger()


def _write_bytes(target: Path, data: bytes) -> None:
    """先写同目录临时文件再替换, 避免媒体服务器读到半张图."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.urandom(4).hex()}.tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)


async def write_library_images(
    repo: Repository,
    metadata: Metadata,
    *,
    thumb: bytes | None = None,
    poster: bytes | None = None,
    fanart: bytes | None = None,
) -> list[Path]:
    """该条目关联的每个文件: 把 thumb / poster / fanart 覆盖到模板解析出的路径, 返回写出的文件.

    路径解析失败 (例如链接模板非法) 只告警并跳过该文件, 不影响其余文件与已写库的元数据.
    """
    if metadata.id is None or (thumb is None and poster is None and fanart is None):
        return []
    written: list[Path] = []
    for media in await repo.get_media_by_metadata_id(metadata.id):
        library = await repo.get_library(media.library_id)
        if library is None:
            logger.warning("image placement skipped: library missing", library_id=media.library_id)
            continue
        try:
            paths = resolve_paths(
                library,
                metadata,
                ext=Path(media.path).suffix.lstrip("."),
                source_path=Path(media.path),
                file_info=parse_file_info(media.path),
                safe_dirs=None,
            )
        except Exception:
            logger.exception("image placement skipped: path resolve failed", media_id=media.id, path=media.path)
            continue
        for target, data in ((paths.thumb, thumb), (paths.poster, poster), (paths.fanart, fanart)):
            if data is None:
                continue
            _write_bytes(target, data)
            written.append(target)
    return written
