"""Create the orchestrator trace tables."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261011_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_traces",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("review_run_id", sa.Text(), nullable=False),
        sa.Column("claim_id", sa.Text(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("from_agent", sa.Text(), nullable=False),
        sa.Column("to_agent", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("review_run_id", "seq", name="uq_agent_traces_run_seq"),
    )
    op.create_index("ix_agent_traces_claim_id", "agent_traces", ["claim_id"])
    op.create_table(
        "review_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("claim_id", sa.Text(), nullable=False),
        sa.Column("run_status", sa.Text(), nullable=False),
        sa.Column("package", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "uq_review_runs_one_running",
        "review_runs",
        ["claim_id"],
        unique=True,
        postgresql_where=sa.text("run_status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("uq_review_runs_one_running", table_name="review_runs")
    op.drop_table("review_runs")
    op.drop_index("ix_agent_traces_claim_id", table_name="agent_traces")
    op.drop_table("agent_traces")
