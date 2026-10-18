"""Resolve organized library images for metadata responses and image serving."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from fastapi import HTTPException
from fastapi.responses import FileResponse

from ..organize import resolve_paths
from ..parsing import parse_file_info

if TYPE_CHECKING:
    from ..db.models import Metadata
    from ..db.repository import Repository
    from .models import MetadataResponse

ImageKind = Literal["thumb", "poster", "fanart"]


async def find_library_image(repo: Repository, metadata: Metadata, kind: ImageKind) -> Path | None:
    if metadata.id is None:
        return None
    for media in await repo.get_media_by_metadata_id(metadata.id):
        library = await repo.get_library(media.library_id)
        if library is None:
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
            continue
        path = getattr(paths, kind)
        if path.is_file():
            return path
    return None


async def localize_metadata_images(
    repo: Repository, metadata: Metadata, response: MetadataResponse
) -> MetadataResponse:
    updates: dict[str, str] = {}
    image_fields: tuple[tuple[ImageKind, str], ...] = (("thumb", "thumb_url"), ("poster", "poster_url"))
    for kind, field in image_fields:
        if await find_library_image(repo, metadata, kind):
            updates[field] = f"/api/metadata/{metadata.id}/images/{kind}"
    return response.model_copy(update=updates) if updates else response


async def serve_library_image(repo: Repository, metadata_id: int, kind: ImageKind) -> FileResponse:
    metadata = await repo.get_metadata(metadata_id)
    if metadata is None:
        raise HTTPException(status_code=404, detail="影片不存在")
    path = await find_library_image(repo, metadata, kind)
    if path is None:
        raise HTTPException(status_code=404, detail="整理后的图片不存在")
    return FileResponse(path=path)
