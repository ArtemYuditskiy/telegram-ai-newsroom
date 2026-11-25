"""Add editor sessions, revision context, and mutable source status."""

import sqlalchemy as sa
from alembic import op

revision = "0004_editor_workspace"
down_revision = "0003_editorial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "feed_sources", sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true())
    )
    op.create_table(
        "editor_sessions",
        sa.Column("token_hash", sa.Text(), primary_key=True),
        sa.Column("csrf", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
    )
    op.create_index("ix_editor_sessions_expires_at", "editor_sessions", ["expires_at"])
    op.create_table(
        "editor_revision_context",
        sa.Column("id", sa.Uuid(), sa.ForeignKey("draft_revisions.id"), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), sa.ForeignKey("draft_revisions.id"), nullable=False),
        sa.Column("run_id", sa.Uuid()),
        sa.Column("review_details", sa.Text()),
    )


def downgrade() -> None:
    op.drop_table("editor_revision_context")
    op.drop_table("editor_sessions")
    op.drop_column("feed_sources", "enabled")
