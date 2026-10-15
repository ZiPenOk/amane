"""显式来源的清单展开: 由选中项求出该作品的完整足迹.

删除文件 = 媒体文件自身 + 按库路径模板反解出的刮削产物 + 同目录同名字幕;
模板产物之外, 再按视频名前缀补收同目录的 nfo / 图 / 预告片 (见 _add_named_siblings);
删除文件与作品文件夹 = 该作品文件夹下的全部内容, 只在该目录仅含这一条媒体索引且不是库根时提供.
展开只收库根内的路径: 配置链接模板时产物落在库外的链接树, 那些不归本功能管;
工作目录里的目录链接 (Windows 的 junction 与挂载点) 同样不展开, 它由用户布置.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ...organize.path_templates import resolve_paths
from ...parsing import parse_file_info
from ...utils.path import existing_disk_path, is_name_surrogate_dir, path_is_under, path_key
from ...utils.threads import in_thread
from ..rules import DEFAULT_SUBTITLE_EXTENSIONS
from .inventory import (
    MAX_INVENTORY_ENTRIES,
    CleanupInventory,
    InventoryEntry,
    InventoryEntryKind,
    InventoryReason,
    InventorySource,
    new_inventory_id,
)

if TYPE_CHECKING:
    from ...db.models import Library, MediaFile, Metadata

logger = structlog.get_logger()

_Add = Callable[[Path], bool]
"""往清单里加一条; 返回它是否真的进了清单 (不存在 / 库外 / 触顶都是 False)."""

# 兜底匹配用的产物标记 (``<视频名>-<标记>.<扩展名>``) 与扩展名白名单.
# 只认这些, 是为了不把 ``X-CD2.mkv`` 这类同名前缀的正片一起收进删除清单.
_PRODUCT_MARKERS: frozenset[str] = frozenset(
    {"thumb", "poster", "fanart", "extrafanart", "trailer", "banner", "landscape", "logo", "clearlogo", "backdrop"}
)
_IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", ".bmp", ".tif", ".tiff"}
)
_MEDIA_EXTENSIONS: frozenset[str] = _IMAGE_EXTENSIONS | frozenset(
    {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".ts", ".m2ts", ".webm", ".flv"}
)


class FootprintNoticeKind(StrEnum):
    """未能纳入清单的项. 只给码与参数, 文案由面板按界面语言给出."""

    MISSING = "missing"
    OUTSIDE_ROOT = "outside_root"
    WORK_DIR_IS_ROOT = "work_dir_is_root"
    WORK_DIR_MULTIPLE = "work_dir_multiple"
    TEMPLATE_ERROR = "template_error"


@dataclass(frozen=True, slots=True)
class FootprintNotice:
    kind: FootprintNoticeKind
    path: Path | None = None
    count: int | None = None
    detail: str | None = None
    """解析器给出的原因; 只有模板解析失败带出."""


@dataclass(frozen=True, slots=True)
class FootprintOutcome:
    """展开结果与未能纳入的部分 (面板据此提示用户)."""

    inventory: CleanupInventory
    notices: list[FootprintNotice]


@in_thread
def build_footprint(
    *,
    library: Library,
    items: Sequence[MediaFile],
    indexed: Sequence[MediaFile],
    metas: Mapping[int, Metadata],
    include_work_dir: bool,
    limit: int = MAX_INVENTORY_ENTRIES,
) -> FootprintOutcome:
    """由选中的媒体文件展开显式来源清单; 只读磁盘, 不访问数据库.

    展开含整目录递归与逐项 stat, 因此与其他库内 I/O 一样经 ``in_thread``.
    """
    assert library.id is not None
    root = Path(library.path)
    entries: list[InventoryEntry] = []
    notices: list[FootprintNotice] = []
    seen: set[str] = set()
    dropped = 0
    # 整目录删除要数每个目录的索引条数; 逐项扫整份索引在选中项多时是平方, 因此先归并一次.
    indexed_per_dir: Mapping[str, int] = (
        Counter(path_key(Path(item.path).parent) for item in indexed) if include_work_dir else {}
    )

    def add(path: Path, *, indexed_file: bool = False) -> bool:
        nonlocal dropped
        # 去重按比较键: 逐条目两两比较在万级规模下是平方.
        key = path_key(path)
        if key in seen:
            return False
        seen.add(key)
        disk = existing_disk_path(path, follow_symlinks=False)
        if disk is None:
            notices.append(FootprintNotice(FootprintNoticeKind.MISSING, path=path))
            return False
        if not path_is_under(disk, root):
            if indexed_file:
                # 索引里的文件本该在库根内: 落在这里说明库路径与索引写法不一致 (旧库的符号链接别名).
                notices.append(FootprintNotice(FootprintNoticeKind.OUTSIDE_ROOT, path=disk))
            # 模板产物落在库外链接树是正常的, 那些不归本功能管.
            return False
        try:
            entry = _entry(disk)
        except OSError:
            # 存在性检查与 stat 之间文件消失, 或挂载盘掉线: 与不在磁盘上同样是这次展开拿不到.
            notices.append(FootprintNotice(FootprintNoticeKind.MISSING, path=path))
            return False
        # 上限判断放在这里: 只有真会进清单的路径才算「未纳入」, 库外产物与不存在的路径不计入.
        if len(entries) >= limit:
            dropped += 1
            return False
        entries.append(entry)
        return True

    for item in items:
        add(Path(item.path), indexed_file=True)
        _add_products(library, item, metas, add=add, notices=notices)
        if not include_work_dir:
            _add_subtitles(library, Path(item.path), add=add)
            continue
        work_dir = Path(item.path).parent
        refusal = _work_dir_refusal(work_dir, root=root, indexed_per_dir=indexed_per_dir)
        if refusal is not None:
            notices.append(refusal)
            _add_subtitles(library, Path(item.path), add=add)
            continue
        _add_tree_contents(work_dir, add=add)

    inventory = CleanupInventory(
        inventory_id=new_inventory_id(),
        library_id=library.id,
        root=root,
        scope_path=None,
        recursive=True,
        patterns=(),
        source=InventorySource.EXPLICIT,
        created_at=datetime.now(UTC),
        entries=entries,
        truncated=bool(dropped),
        dropped=dropped,
    )
    logger.info(
        "selection expanded",
        library_id=library.id,
        entries=len(inventory.entries),
        dropped=inventory.dropped,
        notices=len(notices),
    )
    return FootprintOutcome(inventory=inventory, notices=notices)


def _entry(disk: Path) -> InventoryEntry:
    st = disk.lstat()
    if disk.is_symlink():
        kind = InventoryEntryKind.SYMLINK
    elif disk.is_dir():
        kind = InventoryEntryKind.DIR
    else:
        kind = InventoryEntryKind.FILE
    is_file = kind is not InventoryEntryKind.DIR
    return InventoryEntry(
        path=disk,
        kind=kind,
        reason=InventoryReason.EXPLICIT,
        size=st.st_size if is_file else None,
        dev=st.st_dev if is_file else None,
        ino=st.st_ino if is_file else None,
        nlink=st.st_nlink if is_file else None,
    )


def _add_products(
    library: Library,
    item: MediaFile,
    metas: Mapping[int, Metadata],
    *,
    add: _Add,
    notices: list[FootprintNotice],
) -> None:
    """按路径模板反解刮削产物: 产物位置由模板决定, 与视频文件名没有对应关系.

    模板算出来的产物之外, 再按同名前缀补一层 ``_add_named_siblings``.
    """
    source = Path(item.path)
    metadata = metas.get(item.metadata_id) if item.metadata_id is not None else None
    products: tuple[Path, ...] = ()
    if metadata is not None:
        try:
            paths = resolve_paths(
                library,
                metadata,
                ext=source.suffix.lstrip("."),
                source_path=source,
                file_info=parse_file_info(item.path),
                safe_dirs=None,
            )
        except ValueError as exc:
            notices.append(FootprintNotice(FootprintNoticeKind.TEMPLATE_ERROR, path=source, detail=str(exc)))
        else:
            products = (paths.nfo, paths.thumb, paths.poster, paths.fanart, paths.trailer, paths.extrafanart_dir)
    # 模板产物本来就可能没写过, 只收磁盘上已有的, 缺失不值得提示.
    for product in products:
        if existing_disk_path(product, follow_symlinks=False) is not None:
            add(product)
    # 模板之外再补一层同名匹配 (去重交给 add): 没刮过 / 元数据被删 / 模板与实际命名不一致时,
    # 少了这层清单里就只剩正片, 用户会以为产物不归这个功能管.
    _add_named_siblings(source, add=add)


def _add_named_siblings(video: Path, *, add: _Add) -> None:
    """补收同目录里按 ``<视频名>`` 命名的刮削产物 (模板之外的第二条路).

    命名约定与 ``_add_subtitles`` 一致 (逐字符前缀), 但只认 ``<视频名>-<标记>`` 且标记在
    白名单里的形式 —— 只按前缀收会把 ``X-CD2.mkv`` 这类同名前缀正片一起收进来. 命中的
    条目仍会逐项列给用户确认, 不会静默删除.
    """
    stem = video.stem
    try:
        children = list(video.parent.iterdir())
    except OSError:
        return
    for child in children:
        if child.name == f"{stem}.nfo":
            add(child)
            continue
        if not child.name.startswith(f"{stem}-"):
            continue
        rest = child.name[len(stem) + 1 :]
        if rest.split(".", 1)[0].lower() not in _PRODUCT_MARKERS:
            continue
        if child.is_dir() or child.suffix.lower() in _MEDIA_EXTENSIONS:
            add(child)


def _add_subtitles(library: Library, video: Path, *, add: _Add) -> None:
    """同目录内与视频同名或 `视频名.语言` 形式的字幕; 扩展名取库设置."""
    extensions = {ext.lower() for ext in (library.subtitle_extensions or DEFAULT_SUBTITLE_EXTENSIONS)}
    try:
        children = list(video.parent.iterdir())
    except OSError:
        return
    for child in children:
        if child.suffix.lower() not in extensions:
            continue
        stem = child.stem
        if stem == video.stem or stem.startswith(f"{video.stem}."):
            add(child)


def _work_dir_refusal(work_dir: Path, *, root: Path, indexed_per_dir: Mapping[str, int]) -> FootprintNotice | None:
    if path_key(work_dir) == path_key(root):
        return FootprintNotice(FootprintNoticeKind.WORK_DIR_IS_ROOT)
    siblings = indexed_per_dir.get(path_key(work_dir), 0)
    if siblings > 1:
        return FootprintNotice(FootprintNoticeKind.WORK_DIR_MULTIPLE, path=work_dir, count=siblings)
    return None


def _add_tree_contents(directory: Path, *, add: _Add) -> None:
    """整目录删除按内容逐条展开: 面板给用户看的就是将被删除的全部路径.

    空目录自身也是一个条目: 它没有子项, 只按内容展开就会漏掉它, 而剪枝只处理本次删除项的祖先,
    于是「删除所在目录」会因为残留的空目录而不成立.
    """
    try:
        children = list(directory.iterdir())
    except OSError:
        return
    if not children:
        add(directory)
        return
    for child in children:
        if child.is_dir() and not child.is_symlink():
            if is_name_surrogate_dir(child):
                # 目录链接 (Windows 的 junction 与挂载点): 不展开也不删除 — 它由用户布置,
                # 而展开会把库外内容列进删除集合.
                continue
            _add_tree_contents(child, add=add)
            continue
        add(child)
