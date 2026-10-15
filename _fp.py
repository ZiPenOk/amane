from pathlib import Path
import tempfile, os
from amane.db.models import Library, MediaFile, Metadata, MediaFileStatus
from amane.library import build_footprint

def run(label, *, with_meta=True, video_name="START-460-4K.(mkv)"):
    tmp = Path(tempfile.mkdtemp())
    root = tmp / "test"
    d = root / "123"
    d.mkdir(parents=True)
    (d / f"{video_name}.strm").write_text("/some/where.mkv")
    for suffix in ("-thumb.jpg", "-poster.jpg", "-fanart.jpg"):
        (d / f"{video_name}{suffix}").write_bytes(b"x")
    (d / f"{video_name}.nfo").write_text("nfo")

    lib = Library(
        id=1, name="t", path=str(root),
        video_template="{raw_dir}/{raw_name}.{ext}",
        thumb_template="{video_dir}/{video_name}-thumb.jpg",
        poster_template="{video_dir}/{video_name}-poster.jpg",
        fanart_template="{video_dir}/{video_name}-fanart.jpg",
        nfo_template="{video_dir}/{video_name}.nfo",
        subtitle_extensions=[".srt", ".ass"],
    )
    meta = Metadata(id=7, number="START-460")
    item = MediaFile(
        id=1, library_id=1, path=str(d / f"{video_name}.strm"), number="START-460",
        status=MediaFileStatus.SCRAPED, metadata_id=7 if with_meta else None,
    )
    metas = {7: meta} if with_meta else {}
    out = build_footprint.sync(library=lib, items=[item], indexed=[item], metas=metas, include_work_dir=False)
    print(f"--- {label} (metadata_id={'set' if with_meta else 'NULL'}) ---")
    for e in out.inventory.entries:
        print("   ", e.path.relative_to(root))
    print("   notices:", [(n.kind, str(n.path)) for n in out.notices])

run("user templates", with_meta=True)
run("user templates, no metadata", with_meta=False)
