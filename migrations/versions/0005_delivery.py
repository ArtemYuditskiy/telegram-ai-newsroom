"""Persist delivery receipt, destination and attempt diagnostics."""

import sqlalchemy as sa
from alembic import op

revision = "0005_delivery"
down_revision = "0004_editor_workspace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("publications", sa.Column("destination", sa.Text()))
    op.add_column("publications", sa.Column("message_id", sa.Integer()))
    op.add_column("publication_attempts", sa.Column("detail", sa.Text()))
    op.add_column("publication_attempts", sa.Column("retry_after_at", sa.Text()))
    op.create_index("ix_publications_state_due_at", "publications", ["state", "due_at"])


def downgrade() -> None:
    op.drop_index("ix_publications_state_due_at", table_name="publications")
    op.drop_column("publication_attempts", "retry_after_at")
    op.drop_column("publication_attempts", "detail")
    op.drop_column("publications", "message_id")
    op.drop_column("publications", "destination")
