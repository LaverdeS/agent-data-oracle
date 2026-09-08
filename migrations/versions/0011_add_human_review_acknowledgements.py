"""Persist immutable human review acknowledgement history.

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE human_review_acknowledgements (
            acknowledgement_id uuid PRIMARY KEY,
            evaluation_id uuid NOT NULL
                REFERENCES evidence_evaluations(evaluation_id),
            operator_id uuid NOT NULL REFERENCES operators(operator_id),
            outcome text NOT NULL CHECK (outcome IN (
                'reviewed_same_product', 'reviewed_different_product',
                'need_more_identifiers', 'not_sure'
            )),
            supersedes_acknowledgement_id uuid,
            acknowledged_at timestamptz NOT NULL,
            UNIQUE (acknowledgement_id, evaluation_id),
            FOREIGN KEY (supersedes_acknowledgement_id, evaluation_id)
                REFERENCES human_review_acknowledgements(
                    acknowledgement_id, evaluation_id
                ),
            CHECK (supersedes_acknowledgement_id IS NULL OR
                supersedes_acknowledgement_id <> acknowledgement_id)
        );
        CREATE INDEX human_review_acknowledgements_evaluation_history
            ON human_review_acknowledgements (
                evaluation_id, acknowledged_at, acknowledgement_id
            );
        CREATE TRIGGER human_review_acknowledgements_are_immutable
            BEFORE UPDATE OR DELETE ON human_review_acknowledgements
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_cpsc_source_change();
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE human_review_acknowledgements;")
