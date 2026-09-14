"""Add title column to sessions for topic-based identification.

title 由会话第一条用户消息截取而来，用于侧栏会话列表的辨识。
这里先 inspect 再决定是否 add_column：老库可能已经手工加过这一列，
不加判断会让迁移在重复执行时直接报错。
"""
from alembic import op
import sqlalchemy as sa


revision = "0002_session_title"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {column["name"] for column in inspector.get_columns("sessions")}
    if "title" not in existing:
        # 允许为空：已有会话没有标题，创建时也不一定立刻写入。
        op.add_column("sessions", sa.Column("title", sa.String(length=200), nullable=True))


def downgrade() -> None:
    # 同样做存在性判断，保证 downgrade 可以安全重复执行。
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {column["name"] for column in inspector.get_columns("sessions")}
    if "title" in existing:
        op.drop_column("sessions", "title")
