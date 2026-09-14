"""Create the initial Mini Agent tables.

说明：首个迁移直接复用 ``storage.metadata`` 建表，而不是手写一遍 DDL —— 初版表结构变动频繁，
手抄容易与代码里的表定义不一致。从第二个迁移开始改为显式 DDL，保证历史迁移的稳定性
（已发布的迁移不应再随代码变化）。
"""
from alembic import op

from mini_agent.storage import metadata

# Alembic 靠这三个变量串成迁移链：revision 是本脚本 ID，down_revision=None 表示这是起点。
revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

def upgrade() -> None:
    metadata.create_all(bind=op.get_bind())

def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind())
