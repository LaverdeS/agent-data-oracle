"""Link immutable evidence evaluations created by an explicit refresh.

Revision ID: 0015
Revises: 0014
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE evidence_evaluation_refreshes (
            successor_evaluation_id uuid PRIMARY KEY
                REFERENCES evidence_evaluations(evaluation_id),
            predecessor_evaluation_id uuid NOT NULL UNIQUE
                REFERENCES evidence_evaluations(evaluation_id),
            delta jsonb NOT NULL,
            CHECK (successor_evaluation_id <> predecessor_evaluation_id)
        );
        CREATE TRIGGER evidence_evaluation_refreshes_are_immutable
            BEFORE UPDATE OR DELETE ON evidence_evaluation_refreshes
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_cpsc_source_change();
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE evidence_evaluation_refreshes;")
