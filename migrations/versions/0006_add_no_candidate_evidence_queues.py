"""Add immutable no-candidate evidence queues.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE evidence_evaluations (
            evaluation_id uuid PRIMARY KEY,
            operator_id uuid NOT NULL REFERENCES operators(operator_id),
            source_revision_id uuid NOT NULL
                REFERENCES cpsc_source_revisions(revision_id),
            evaluated_at timestamptz NOT NULL,
            normalization_version text NOT NULL,
            matcher_version text NOT NULL,
            outcome text NOT NULL CHECK (outcome = 'no_candidates'),
            released_at timestamptz NOT NULL,
            idempotency_key text NOT NULL,
            canonical_submission_hash character(64) NOT NULL,
            UNIQUE (operator_id, idempotency_key)
        );
        CREATE INDEX evidence_evaluations_operator_released
            ON evidence_evaluations (operator_id, released_at DESC);

        CREATE TABLE evidence_evaluation_inputs (
            evaluation_id uuid NOT NULL
                REFERENCES evidence_evaluations(evaluation_id),
            row_position integer NOT NULL CHECK (row_position >= 0),
            identifier_type text NOT NULL CHECK (
                identifier_type IN ('upc', 'model', 'brand')
            ),
            submitted_literal text NOT NULL,
            normalized_value text NOT NULL,
            normalization_version text NOT NULL,
            PRIMARY KEY (evaluation_id, row_position)
        );

        CREATE TRIGGER evidence_evaluations_are_immutable
            BEFORE UPDATE OR DELETE ON evidence_evaluations
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_cpsc_source_change();
        CREATE TRIGGER evidence_evaluation_inputs_are_immutable
            BEFORE UPDATE OR DELETE ON evidence_evaluation_inputs
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_cpsc_source_change();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE evidence_evaluation_inputs;
        DROP TABLE evidence_evaluations;
        """
    )
