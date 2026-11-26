"""Separate the editor's scheduled time from retry timing."""

import sqlalchemy as sa
from alembic import op

revision = "0007_delivery_timing"
down_revision = "0006_schedule_controls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("publications", sa.Column("scheduled_for", sa.Text()))
    op.add_column("publications", sa.Column("retry_not_before", sa.Text()))
    op.execute(
        "UPDATE publications SET scheduled_for = due_at "
        "WHERE state != 'RETRY_WAIT' AND NOT EXISTS ("
        "SELECT 1 FROM publication_attempts AS a WHERE a.publication_id = publications.id "
        "AND a.outcome IN ('RETRY_WAIT', 'UNCERTAIN'))"
    )
    op.execute("UPDATE publications SET retry_not_before = due_at WHERE state = 'RETRY_WAIT'")


def downgrade() -> None:
    op.drop_column("publications", "retry_not_before")
    op.drop_column("publications", "scheduled_for")
