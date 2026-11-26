"""Protect active publication schedules and retain cancellation history."""

import sqlalchemy as sa
from alembic import op

revision = "0006_schedule_controls"
down_revision = "0005_delivery"
branch_labels = None
depends_on = None

ACTIVE = "state IN ('SCHEDULED', 'RETRY_WAIT', 'PUBLISHING', 'UNCERTAIN')"


def upgrade() -> None:
    conn = op.get_bind()
    conflicts = conn.execute(
        sa.text(
            f"SELECT d.candidate_id, group_concat(p.id) FROM publications p "
            f"JOIN draft_revisions d ON d.id = p.draft_revision_id WHERE p.{ACTIVE} "
            "GROUP BY d.candidate_id HAVING count(*) > 1"
        )
    ).all()
    if conflicts:
        details = "; ".join(f"{candidate}: {ids}" for candidate, ids in conflicts)
        raise RuntimeError(f"Resolve active publication conflicts before upgrading: {details}")
    op.execute(
        "ALTER TABLE publications ADD COLUMN candidate_id CHAR(32) REFERENCES candidates(id)"
    )
    op.add_column("publications", sa.Column("cancelled_at", sa.Text()))
    op.execute(
        "UPDATE publications SET candidate_id = "
        "(SELECT candidate_id FROM draft_revisions WHERE id = publications.draft_revision_id)"
    )
    op.create_index(
        "uq_publications_active_candidate",
        "publications",
        ["candidate_id"],
        unique=True,
        sqlite_where=sa.text(f"cancelled_at IS NULL AND {ACTIVE}"),
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM publications WHERE cancelled_at IS NOT NULL LIMIT 1"))
        .first()
    ):
        raise RuntimeError("Downgrade would reactivate cancelled publications")
    op.drop_index("uq_publications_active_candidate", table_name="publications")
    op.drop_column("publications", "cancelled_at")
    op.drop_column("publications", "candidate_id")
