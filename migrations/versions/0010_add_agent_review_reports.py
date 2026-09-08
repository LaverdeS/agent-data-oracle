"""Persist separately typed external-agent review reports.

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE agent_review_reports (
            report_id uuid PRIMARY KEY,
            evaluation_id uuid NOT NULL
                REFERENCES evidence_evaluations(evaluation_id),
            operator_id uuid NOT NULL REFERENCES operators(operator_id),
            agent_key_id uuid NOT NULL REFERENCES agent_api_keys(agent_key_id),
            outcome text NOT NULL CHECK (outcome IN (
                'reviewed_same_product', 'reviewed_different_product',
                'need_more_identifiers', 'not_sure'
            )),
            reported_at timestamptz NOT NULL
        );
        CREATE INDEX agent_review_reports_evaluation_history
            ON agent_review_reports (evaluation_id, reported_at, report_id);
        CREATE TRIGGER agent_review_reports_are_immutable
            BEFORE UPDATE OR DELETE ON agent_review_reports
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_cpsc_source_change();
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE agent_review_reports;")
