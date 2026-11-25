"""Create candidate, draft revision and publication tables."""

import sqlalchemy as sa
from alembic import op

revision = "0001_workflow"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "candidates",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "state",
            sa.Enum(
                "DISCOVERED",
                "NORMALIZED",
                "ELIGIBLE",
                "SKIPPED",
                "SCORED",
                "SELECTED",
                "REJECTED",
                "DRAFTED",
                name="candidatestate",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
    )
    op.create_table(
        "draft_revisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("candidate_id", sa.Uuid(), sa.ForeignKey("candidates.id"), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "REVIEW_PENDING",
                "APPROVED",
                "REJECTED",
                name="draftstate",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
    )
    op.create_index("ix_draft_revisions_candidate_id", "draft_revisions", ["candidate_id"])
    op.create_table(
        "publications",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "draft_revision_id", sa.Uuid(), sa.ForeignKey("draft_revisions.id"), nullable=False
        ),
        sa.Column("due_at", sa.Text(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "SCHEDULED",
                "PUBLISHING",
                "PUBLISHED",
                "RETRY_WAIT",
                "FAILED",
                "UNCERTAIN",
                name="publicationstate",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
    )
    op.create_index("ix_publications_draft_revision_id", "publications", ["draft_revision_id"])
    op.create_table(
        "publication_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("publication_id", sa.Uuid(), sa.ForeignKey("publications.id"), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("finished_at", sa.Text()),
        sa.Column(
            "outcome",
            sa.Enum(
                "SCHEDULED",
                "PUBLISHING",
                "PUBLISHED",
                "RETRY_WAIT",
                "FAILED",
                "UNCERTAIN",
                name="publicationstate",
                native_enum=False,
                create_constraint=True,
            ),
        ),
    )
    op.create_index(
        "ix_publication_attempts_publication_id", "publication_attempts", ["publication_id"]
    )
    op.create_index(
        "uq_publication_open_attempt",
        "publication_attempts",
        ["publication_id"],
        unique=True,
        sqlite_where=sa.text("finished_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("publication_attempts")
    op.drop_table("publications")
    op.drop_table("draft_revisions")
    op.drop_table("candidates")
