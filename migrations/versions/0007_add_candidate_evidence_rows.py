"""Persist immutable deterministic candidate evidence rows.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE evidence_evaluations
            DROP CONSTRAINT evidence_evaluations_outcome_check;
        ALTER TABLE evidence_evaluations
            ADD CONSTRAINT evidence_evaluations_outcome_check CHECK (
                outcome IN ('no_candidates', 'candidates')
            );

        CREATE TABLE evidence_rows (
            evidence_row_id uuid PRIMARY KEY,
            evaluation_id uuid NOT NULL
                REFERENCES evidence_evaluations(evaluation_id),
            input_position integer NOT NULL CHECK (input_position >= 0),
            recall_id bigint NOT NULL REFERENCES cpsc_recalls(recall_id),
            candidate_class text NOT NULL CHECK (
                candidate_class IN (
                    'exact_identifier_candidate',
                    'possible_identifier_candidate'
                )
            ),
            match_bases jsonb NOT NULL,
            affected_product_evidence jsonb NOT NULL,
            constraints jsonb NOT NULL,
            recall_number text NOT NULL,
            official_url text NOT NULL,
            recall_date_literal text,
            last_publish_date_literal text,
            source_observed_at timestamptz NOT NULL,
            source_revision_completed_at timestamptz NOT NULL,
            UNIQUE (evaluation_id, input_position, recall_id)
        );
        CREATE INDEX evidence_rows_evaluation_order
            ON evidence_rows (evaluation_id, candidate_class, last_publish_date_literal,
                recall_number, evidence_row_id);
        CREATE TRIGGER evidence_rows_are_immutable
            BEFORE UPDATE OR DELETE ON evidence_rows
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_cpsc_source_change();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE evidence_rows;
        ALTER TABLE evidence_evaluations
            DROP CONSTRAINT evidence_evaluations_outcome_check;
        ALTER TABLE evidence_evaluations
            ADD CONSTRAINT evidence_evaluations_outcome_check CHECK (
                outcome = 'no_candidates'
            );
        """
    )
