"""reproject uncensored official numbers with site tags

Revision ID: b7e4c9a05d31
Revises: c4e17b2a90d5
Create Date: 2026-10-16 18:20:00.000000

站点名之外还挂着别的标记时 (``[10MU] 091926_01-10MU`` / ``091926_01-10MU【無修正】``), 迁移
c4e17b2a90d5 的解析还认不出来, 这些行仍停在「欧美 + 带站点名的原文」. 按当前 parse_file_info
重算番号 / 类型 / 马赛克; 番号没变的行不写, 其它形态的行保持原值.
"""

import re
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

from amane.parsing import ContentType, parse_file_info

# revision identifiers, used by Alembic.
revision: str = "b7e4c9a05d31"
down_revision: str | None = "c4e17b2a90d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 归一后的无码官网番号形态: 6 位日期 + 分隔符 + 2~4 位编号.
_DATE_ID = re.compile(r"^\d{6}[-_]\d{2,4}$")


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(text("SELECT id, path, number FROM media_files")).all()
    for row_id, path, number in rows:
        info = parse_file_info(path)
        if info.number is None or info.content_type != ContentType.UNCENSORED:
            continue
        if not _DATE_ID.fullmatch(info.number) or info.number == number:
            continue
        conn.execute(
            text(
                "UPDATE media_files SET number = :number, content_type = :content_type, mosaic = :mosaic WHERE id = :id"
            ),
            {
                "number": info.number,
                "content_type": info.content_type.name,
                "mosaic": info.mosaic.name if info.mosaic is not None else None,
                "id": row_id,
            },
        )


def downgrade() -> None:
    pass
