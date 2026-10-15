from pathlib import Path
import tempfile
from amane.db.models import Library, MediaFile, Metadata, MediaFileStatus
from amane.library import build_footprint

def run(label, *, with_meta, include_work_dir):
    tmp = Path(tempfile.mkdtemp())
    root = tmp / "test"; d = root / "123"; d.mkdir(parents=True)
    vn = "START-460-4K.(mkv)"
    (d / f"{vn}.strm").write_text("x")
    (d / f"{vn}.nfo").write_text("nfo")
    (d / f"{vn}-thumb.jpg").write_bytes(b"x")
    (d / f"{vn}-poster.jpg").write_bytes(b"x")
    (d / f"{vn}-fanart.jpg").write_bytes(b"x")
    (d / f"{vn}.zh.srt").write_text("sub")
    (d / "extrafanart").mkdir(); (d / "extrafanart" / "1.jpg").write_bytes(b"x")
    (d / "其他文件.txt").write_text("other")
    lib = Library(id=1, name="t", path=str(root),
        video_template="{raw_dir}/{raw_name}.{ext}",
        thumb_template="{video_dir}/{video_name}-thumb.jpg",
        poster_template="{video_dir}/{video_name}-poster.jpg",
        fanart_template="{video_dir}/{video_name}-fanart.jpg",
        nfo_template="{video_dir}/{video_name}.nfo")
    item = MediaFile(id=1, library_id=1, path=str(d / f"{vn}.strm"), number="START-460",
                     status=MediaFileStatus.SCRAPED, metadata_id=7 if with_meta else None)
    metas = {7: Metadata(id=7, number="START-460")} if with_meta else {}
    out = build_footprint.sync(library=lib, items=[item], indexed=[item], metas=metas,
                               include_work_dir=include_work_dir)
    print(f"--- {label} ---")
    for e in sorted(out.inventory.entries, key=lambda x: str(x.path)):
        print("   ", e.path.relative_to(root), "(dir)" if e.kind.value == "dir" else "")
    print("   notices:", [n.kind.value for n in out.notices])

run("删除文件 + 有元数据", with_meta=True, include_work_dir=False)
run("删除文件 + 无元数据", with_meta=False, include_work_dir=False)
run("删除所在目录 + 无元数据", with_meta=False, include_work_dir=True)
