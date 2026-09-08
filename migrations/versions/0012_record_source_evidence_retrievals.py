"""Record bounded source-evidence retrieval events.

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE source_evidence_retrievals (
            retrieval_id uuid PRIMARY KEY,
            evaluation_id uuid NOT NULL
                REFERENCES evidence_evaluations(evaluation_id),
            operator_id uuid NOT NULL REFERENCES operators(operator_id),
            agent_key_id uuid REFERENCES agent_api_keys(agent_key_id),
            evidence_row_id uuid REFERENCES evidence_rows(evidence_row_id),
            retrieved_at timestamptz NOT NULL
        );
        CREATE INDEX source_evidence_retrievals_evaluation_history
            ON source_evidence_retrievals (evaluation_id, retrieved_at, retrieval_id);
        CREATE TRIGGER source_evidence_retrievals_are_immutable
            BEFORE UPDATE OR DELETE ON source_evidence_retrievals
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_cpsc_source_change();
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE source_evidence_retrievals;")
