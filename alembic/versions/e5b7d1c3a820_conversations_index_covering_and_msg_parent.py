"""conversations index covers soft-delete + tiebreaker; messages parent index

Revision ID: e5b7d1c3a820
Revises: c4a1f7e2b903
Create Date: 2026-10-29 10:05:00.000000

两个索引改动都来自同一条查询路径 —— 会话列表与"重新生成"：

1. `idx_conv_user_recent` 原来只有 (user_id, last_message_at DESC)。
   真实查询是

       WHERE user_id = ? AND deleted_at IS NULL
       ORDER BY last_message_at DESC, id DESC

   * 少了 deleted_at：软删除的行要先回表取出来再丢弃。删得越多，
     翻一页要扫的行越多，直到"翻页越来越慢"变成用户可感知的问题。
   * 少了 id：同一毫秒产生的会话要靠 filesort 二次排序。

   加上这两列后，等值条件在前、排序键在后，排序可以完全由索引提供，
   且不需要回表判断软删除。

2. `idx_msg_parent`：`parent_message_id` 一直存在但没有索引。
   上一条迁移只建了字段，当时没有代码用它；现在"重新生成"会写入它，
   并且每次加载上下文都要问"这条回答有没有被替代"（相关 EXISTS 子查询）。
   没有索引就是每轮对话扫一次 messages。

降级会把索引还原成三列版本 —— 注意降级后重新升级是安全的，
两个 DDL 都只是索引重建，不涉及数据。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = "e5b7d1c3a820"
down_revision: str | Sequence[str] | None = "c4a1f7e2b903"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # 顺序不能是"先删后建"：fk_conv_user 依赖 user_id 上的索引，
    # MySQL 会拒绝删掉它（错误 1553: Cannot drop index ... needed in a foreign
    # key constraint）。所以先建一份同前缀的新索引、再删旧的、最后改名 ——
    # 任意时刻 user_id 上都有可用索引，外键约束始终成立。
    op.create_index(
        "idx_conv_user_recent_new",
        "conversations",
        ["user_id", "deleted_at", sa.text("last_message_at DESC"), sa.text("id DESC")],
        unique=False,
    )
    op.drop_index("idx_conv_user_recent", table_name="conversations")
    op.execute(
        "ALTER TABLE conversations RENAME INDEX idx_conv_user_recent_new TO idx_conv_user_recent"
    )
    op.create_index("idx_msg_parent", "messages", ["parent_message_id"], unique=False)
    # 这个字段的语义从"预留"变成"重新生成时写入被替代的回答"，
    # 注释跟着改：否则下一个人读表结构会以为它还没被用过
    op.alter_column(
        "messages",
        "parent_message_id",
        existing_type=mysql.BIGINT(unsigned=True),
        existing_nullable=True,
        comment="被本条替代的回答 ID（重新生成时写入）",
    )


def downgrade() -> None:
    """Downgrade schema。"""
    op.alter_column(
        "messages",
        "parent_message_id",
        existing_type=mysql.BIGINT(unsigned=True),
        existing_nullable=True,
        comment="重新生成/分支的来源消息",
    )
    op.drop_index("idx_msg_parent", table_name="messages")
    # 同样不能直接删：新索引正被外键使用着
    op.create_index(
        "idx_conv_user_recent_new",
        "conversations",
        ["user_id", sa.text("last_message_at DESC")],
        unique=False,
    )
    op.drop_index("idx_conv_user_recent", table_name="conversations")
    op.execute(
        "ALTER TABLE conversations RENAME INDEX idx_conv_user_recent_new TO idx_conv_user_recent"
    )
