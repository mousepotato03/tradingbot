"""Separate monitor observations from research evidence; record outcome maturity.

Existing reports stay loadable without a data rewrite: the application models coerce the older
string gaps and free-text actions on read (see PortfolioDecision.legacy_actions).
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "monitor_observations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("report_id", sa.String(36)),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("body", sa.JSON(), nullable=False),
    )
    op.create_index("ix_monitor_observations_ticker", "monitor_observations", ["ticker"])
    with op.batch_alter_table("outcomes") as batch:
        batch.add_column(
            sa.Column("mature", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade():
    with op.batch_alter_table("outcomes") as batch:
        batch.drop_column("mature")
    op.drop_index("ix_monitor_observations_ticker", table_name="monitor_observations")
    op.drop_table("monitor_observations")
