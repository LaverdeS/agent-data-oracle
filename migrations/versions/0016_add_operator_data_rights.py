"""Add bounded operator data export, deletion, and retention records.

Revision ID: 0016
Revises: 0015
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE operator_deletion_work (
            operator_id uuid PRIMARY KEY REFERENCES operators(operator_id),
            requested_at timestamptz NOT NULL,
            due_at timestamptz NOT NULL,
            status text NOT NULL CHECK (status = 'pending'),
            categories text[] NOT NULL,
            CHECK (due_at = requested_at + INTERVAL '7 days')
        );
        CREATE TABLE deletion_completion_receipts (
            receipt_id uuid PRIMARY KEY,
            completed_at timestamptz NOT NULL,
            outcome text NOT NULL CHECK (outcome = 'completed'),
            categories text[] NOT NULL
        );
        CREATE TABLE operator_retention_holds (
            hold_id uuid PRIMARY KEY,
            operator_id uuid NOT NULL REFERENCES operators(operator_id),
            reason_category text NOT NULL CHECK (length(reason_category) > 0),
            recorded_at timestamptz NOT NULL,
            released_at timestamptz,
            CHECK (released_at IS NULL OR released_at >= recorded_at)
        );
        CREATE INDEX operator_retention_holds_active
            ON operator_retention_holds (operator_id) WHERE released_at IS NULL;

        ALTER TABLE global_pause_events ALTER COLUMN actor_id DROP NOT NULL;

        CREATE OR REPLACE FUNCTION reject_immutable_cpsc_source_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF current_setting('agent_data_oracle.operator_erasure', true) = 'on'
                AND TG_TABLE_NAME = ANY (ARRAY[
                    'agent_review_reports', 'evidence_evaluation_inputs',
                    'evidence_evaluation_refreshes', 'evidence_evaluations',
                    'evidence_rows', 'evaluation_audits', 'evaluation_releases',
                    'global_pause_events', 'human_review_acknowledgements',
                    'source_evidence_retrievals'
                ]) THEN
                IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION '% is immutable', TG_TABLE_NAME
                USING ERRCODE = 'integrity_constraint_violation';
        END;
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE global_pause_events ALTER COLUMN actor_id DROP NOT NULL;

        CREATE OR REPLACE FUNCTION reject_immutable_cpsc_source_change()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is immutable', TG_TABLE_NAME
                USING ERRCODE = 'integrity_constraint_violation';
        END;
        $$;
        DROP TABLE operator_retention_holds;
        DROP TABLE deletion_completion_receipts;
        DROP TABLE operator_deletion_work;
        """
    )
