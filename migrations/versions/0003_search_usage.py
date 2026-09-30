"""Rolling live web-search usage for the local provider quota."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "search_usage",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("run_id", sa.String(36)),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_search_usage_used_at", "search_usage", ["used_at"])


def downgrade():
    op.drop_index("ix_search_usage_used_at", table_name="search_usage")
    op.drop_table("search_usage")
