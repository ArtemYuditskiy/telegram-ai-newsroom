"""Persist approved RSS sources and article ingestion decisions."""

import sqlalchemy as sa
from alembic import op

revision = "0002_ingestion"
down_revision = "0001_workflow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feed_sources",
        sa.Column("url", sa.Text(), primary_key=True),
        sa.Column("hosts", sa.Text(), nullable=False),
        sa.Column("etag", sa.Text()),
        sa.Column("last_modified", sa.Text()),
        sa.Column("last_checked_at", sa.Text()),
        sa.Column("last_success_at", sa.Text()),
        sa.Column("last_error", sa.String(100)),
    )
    op.create_table(
        "feed_items",
        sa.Column("source_url", sa.Text(), sa.ForeignKey("feed_sources.url"), primary_key=True),
        sa.Column("entry_key", sa.Text(), primary_key=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("published_at", sa.Text()),
        sa.Column("image_url", sa.Text()),
        sa.Column("reason", sa.String(100), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), sa.ForeignKey("candidates.id")),
    )
    op.create_table(
        "candidate_articles",
        sa.Column("candidate_id", sa.Uuid(), sa.ForeignKey("candidates.id"), primary_key=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("title_key", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("published_at", sa.Text()),
        sa.Column("image_url", sa.Text()),
        sa.UniqueConstraint("url", name="uq_candidate_article_url"),
        sa.UniqueConstraint("fingerprint", name="uq_candidate_article_fingerprint"),
    )


def downgrade() -> None:
    op.drop_table("candidate_articles")
    op.drop_table("feed_items")
    op.drop_table("feed_sources")
