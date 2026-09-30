"""Initial durable evidence, research, candidate and job storage."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "research_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("checkpoint", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error", sa.Text()),
    )
    op.create_index("ix_research_runs_ticker", "research_runs", ["ticker"])
    op.create_table(
        "evidence",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("body", sa.JSON(), nullable=False),
    )
    op.create_index("ix_evidence_run_id", "evidence", ["run_id"])
    op.create_table(
        "tool_traces",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("body", sa.JSON(), nullable=False),
    )
    op.create_index("ix_tool_traces_run_id", "tool_traces", ["run_id"])
    op.create_table(
        "reports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("body", sa.JSON(), nullable=False),
        sa.Column("markdown", sa.Text(), nullable=False),
    )
    op.create_index("ix_reports_ticker", "reports", ["ticker"])
    op.create_table(
        "candidate_states",
        sa.Column("ticker", sa.String(20), primary_key=True),
        sa.Column("report_id", sa.String(36), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("body", sa.JSON(), nullable=False),
    )
    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("body", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False, unique=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_table(
        "outcomes",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("report_id", sa.String(36), nullable=False),
        sa.Column("body", sa.JSON(), nullable=False),
    )
    op.create_index("ix_outcomes_report_id", "outcomes", ["report_id"])
    op.create_table(
        "watch_schedules",
        sa.Column("ticker", sa.String(20), primary_key=True),
        sa.Column("next_research_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_condition_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("body", sa.JSON(), nullable=False),
    )


def downgrade():
    for table in [
        "watch_schedules",
        "outcomes",
        "jobs",
        "notification_outbox",
        "candidate_states",
        "reports",
        "tool_traces",
        "evidence",
        "research_runs",
    ]:
        op.drop_table(table)
