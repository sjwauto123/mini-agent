"""Create the initial Mini Agent tables."""
from alembic import op

from mini_agent.storage import metadata

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

def upgrade() -> None:
    metadata.create_all(bind=op.get_bind())

def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind())
