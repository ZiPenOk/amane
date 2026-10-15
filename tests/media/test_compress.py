"""测试图片压缩的纯函数与就地压缩的标记语义."""

from typing import TYPE_CHECKING, Any

import pytest
from PIL import Image

from amane.config import CompressConfig
from amane.media import CompressOutcome, ResourceStore, compress_image

if TYPE_CHECKING:
    from pathlib import Path


def _cfg(**overrides: Any) -> CompressConfig:
    return CompressConfig(enabled=True, **overrides)


class TestCompressImage:
    def test_shrinks_oversized_to_max_dim(self, tmp_path: Path):
        src = tmp_path / "big.jpg"
        Image.new("RGB", (2200, 1100), color="blue").save(src)
        dest = tmp_path / "out.jpg"

        assert compress_image.sync(src, dest, _cfg(max_dim=1100)) is True
        assert Image.open(dest).size == (1100, 550)

    def test_keeps_dimensions_within_limit(self, tmp_path: Path):
        src = tmp_path / "small.jpg"
        Image.new("RGB", (800, 538), color="blue").save(src)
        dest = tmp_path / "out.jpg"

        assert compress_image.sync(src, dest, _cfg(max_dim=1100)) is True
        assert Image.open(dest).size == (800, 538)

    def test_keeps_transparency_for_png(self, tmp_path: Path):
        src = tmp_path / "a.png"
        Image.new("RGBA", (64, 64), (255, 0, 0, 0)).save(src)
        dest = tmp_path / "out.png"

        assert compress_image.sync(src, dest, _cfg()) is True
        assert Image.open(dest).mode in {"RGBA", "LA", "P"}

    def test_converts_non_rgb_source_for_jpeg(self, tmp_path: Path):
        """JPEG 没有 alpha 通道: 带透明度的源图不转 RGB 会保存失败."""
        rgba = tmp_path / "source.png"
        Image.new("RGBA", (32, 32), (255, 0, 0, 128)).save(rgba)
        # 后缀决定保存格式, 因此这里造一个「真身是 PNG、名字是 .jpg」的源图.
        src = tmp_path / "p.jpg"
        src.write_bytes(rgba.read_bytes())
        dest = tmp_path / "out.jpg"

        assert compress_image.sync(src, dest, _cfg()) is True
        assert Image.open(dest).mode == "RGB"

    def test_rejects_unsupported_format(self, tmp_path: Path):
        src = tmp_path / "a.gif"
        Image.new("RGB", (32, 32), color="red").save(src)

        assert compress_image.sync(src, tmp_path / "out.gif", _cfg()) is False

    def test_disabled_returns_false(self, tmp_path: Path):
        src = tmp_path / "a.jpg"
        Image.new("RGB", (32, 32), color="red").save(src)

        assert compress_image.sync(src, tmp_path / "out.jpg", CompressConfig()) is False

    def test_broken_file_returns_false(self, tmp_path: Path):
        src = tmp_path / "broken.jpg"
        src.write_bytes(b"not an image")

        assert compress_image.sync(src, tmp_path / "out.jpg", _cfg()) is False

    def test_skips_animated_image(self, tmp_path: Path):
        frames = [Image.new("RGB", (16, 16), color=(index * 40, 0, 0)) for index in range(2)]
        src = tmp_path / "anim.webp"
        try:
            frames[0].save(src, save_all=True, append_images=frames[1:], duration=100, loop=0)
        except Exception:  # 缺 webp 动图支持时不算失败
            pytest.skip("当前 Pillow 写不出动图 webp")

        assert compress_image.sync(src, tmp_path / "out.webp", _cfg()) is False


_ARGS = {"max_dim": 1100, "quality": 80}


class TestCompressInPlace:
    @pytest.mark.asyncio
    async def test_overwrites_and_marks_meta(self, resource_store: ResourceStore):
        res = await _seed(resource_store)
        old_hash = res.content_hash

        assert await resource_store.compress_in_place(res, _ARGS, _tiny_producer) is CompressOutcome.COMPRESSED

        updated = await resource_store.get_by_url(res.url)
        assert updated is not None
        assert updated.meta is not None and "compress" in updated.meta
        assert updated.meta["op"] == "crop"  # 保留原派生信息
        assert updated.content_hash != old_hash
        assert updated.size == len(b"tiny")

    @pytest.mark.asyncio
    async def test_reruns_only_when_args_change(self, resource_store: ResourceStore):
        res = await _seed(resource_store)
        assert await resource_store.compress_in_place(res, _ARGS, _tiny_producer) is CompressOutcome.COMPRESSED

        updated = await resource_store.get_by_url(res.url)
        assert updated is not None
        # 参数与盘上字节都没变 → 跳过
        assert await resource_store.compress_in_place(updated, _ARGS, _tiny_producer) is CompressOutcome.SKIPPED
        # 改了规则 → 按新参数重跑 (与超分「有标记就跳过」不同)
        new_args = {"max_dim": 800, "quality": 70}
        assert await resource_store.compress_in_place(updated, new_args, _tiny_producer) is CompressOutcome.COMPRESSED

    @pytest.mark.asyncio
    async def test_failure_keeps_original(self, resource_store: ResourceStore):
        res = await _seed(resource_store)

        async def failing(src: Path, out: Path) -> bool:
            return False

        assert await resource_store.compress_in_place(res, _ARGS, failing) is CompressOutcome.FAILED
        after = await resource_store.get_by_url(res.url)
        assert after is not None
        assert after.meta is not None and "compress" not in after.meta  # 失败不标记
        assert resource_store.full_path(after).read_bytes() == b"original-bytes"

    @pytest.mark.asyncio
    async def test_keeps_sr_marker(self, resource_store: ResourceStore):
        """两个标记各记各的: 压缩不覆盖超分留下的标记."""
        res = await _seed(resource_store)

        async def sr_producer(src: Path, out: Path) -> bool:
            out.write_bytes(b"upscaled")
            return True

        assert await resource_store.upscale_in_place(res, {"scale": 2}, sr_producer) is not None
        marked = await resource_store.get_by_url(res.url)
        assert marked is not None
        assert await resource_store.compress_in_place(marked, _ARGS, _tiny_producer) is CompressOutcome.COMPRESSED

        final = await resource_store.get_by_url(res.url)
        assert final is not None and final.meta is not None
        assert "sr" in final.meta and "compress" in final.meta


async def _seed(store: ResourceStore):
    async def producer(dest: Path) -> bool:
        dest.write_bytes(b"original-bytes")
        return True

    res = await store.acquire_derived("https://s/p.jpg", "crop", "0.7", producer)
    assert res is not None
    return res


async def _tiny_producer(src: Path, out: Path) -> bool:
    out.write_bytes(b"tiny")
    return True
