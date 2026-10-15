"""图片压缩: 长边超限时等比缩小, 再按质量重编码. 只做判定与编码, 库内记账由 ResourceStore 承担."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from PIL import Image

from ..utils.threads import in_thread

if TYPE_CHECKING:
    from ..config import CompressConfig

logger = structlog.get_logger()

#: 可压缩格式: 后缀 → PIL 保存格式. 其余后缀与未知格式一律跳过.
FORMAT_BY_SUFFIX: dict[str, str] = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".webp": "WEBP",
}
COMPRESSIBLE_FORMATS = frozenset(FORMAT_BY_SUFFIX.values())


def _resize_to_max_dim(image: Image.Image, max_dim: int) -> Image.Image:
    """长边超限才缩, 等比; 已在限内时原样返回 (重编码仍照做)."""
    width, height = image.size
    current = max(width, height)
    if current <= max_dim:
        return image
    scale = max_dim / current
    return image.resize(
        (max(round(width * scale), 1), max(round(height * scale), 1)),
        resample=Image.Resampling.LANCZOS,
    )


@in_thread
def compress_image(src: Path, dest: Path, cfg: CompressConfig) -> bool:
    """读 ``src`` 写 ``dest``, 由调用方接管落盘 (ResourceStore 先写临时文件再替换).

    动图与不支持的后缀返回假; 异常吞掉记日志, 由调用方计作失败.
    """
    if not cfg.enabled:
        return False

    resized: Image.Image | None = None
    converted: Image.Image | None = None
    try:
        with Image.open(src) as image:
            image.load()
            if getattr(image, "is_animated", False):
                return False
            save_format = FORMAT_BY_SUFFIX.get(src.suffix.lower()) or image.format
            if save_format not in COMPRESSIBLE_FORMATS:
                return False

            source = image
            # JPEG 没有 alpha 通道: 调色板图与带透明度的图直接保存会报错.
            if save_format == "JPEG" and source.mode not in {"RGB", "L"}:
                converted = source.convert("RGB")
                source = converted

            resized = _resize_to_max_dim(source, cfg.max_dim)
            if save_format == "JPEG":
                resized.save(dest, format=save_format, quality=cfg.quality, optimize=True)
            elif save_format == "WEBP":
                resized.save(dest, format=save_format, quality=cfg.quality)
            else:
                resized.save(dest, format=save_format, optimize=True, compress_level=9)
        return True
    except Exception as e:
        logger.warning("image compress failed", src=str(src), error=str(e))
        return False
    finally:
        # 未缩小时 resized 就是 Image.open 的那份, 关闭两次无害.
        if resized is not None:
            resized.close()
        if converted is not None:
            converted.close()
