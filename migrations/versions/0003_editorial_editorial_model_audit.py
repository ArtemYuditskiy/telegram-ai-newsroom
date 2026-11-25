"""editorial model audit"""

import sqlalchemy as sa
from alembic import op

revision = "0003_editorial"
down_revision = "0002_ingestion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("backend", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Text(), nullable=True),
        sa.Column("recorded_at", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["candidates.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_llm_calls_candidate_id"), "llm_calls", ["candidate_id"], unique=False)
    op.create_table(
        "editorial_results",
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=True),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("threshold", sa.Integer(), nullable=False),
        sa.Column("score_details", sa.Text(), nullable=False),
        sa.Column("draft_details", sa.Text(), nullable=True),
        sa.Column("review_details", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["candidates.id"],
        ),
        sa.ForeignKeyConstraint(
            ["draft_id"],
            ["draft_revisions.id"],
        ),
        sa.PrimaryKeyConstraint("candidate_id"),
    )


def downgrade() -> None:
    op.drop_table("editorial_results")
    op.drop_index(op.f("ix_llm_calls_candidate_id"), table_name="llm_calls")
    op.drop_table("llm_calls")
