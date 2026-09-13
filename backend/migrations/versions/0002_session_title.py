"""Add title column to sessions for topic-based identification."""
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
        op.add_column("sessions", sa.Column("title", sa.String(length=200), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {column["name"] for column in inspector.get_columns("sessions")}
    if "title" in existing:
        op.drop_column("sessions", "title")
