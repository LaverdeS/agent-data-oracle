"""Hold early candidate evaluations for founder audit and pause control.

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE evidence_evaluations
            ALTER COLUMN released_at DROP NOT NULL;

        CREATE TABLE audit_gate_state (
            singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
            committed_queue_count integer NOT NULL DEFAULT 0
                CHECK (committed_queue_count >= 0)
        );
        INSERT INTO audit_gate_state (singleton) VALUES (true);

        CREATE TABLE global_pause_state (
            singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
            is_paused boolean NOT NULL DEFAULT false,
            trigger_kind text,
            reason_category text,
            activated_at timestamptz,
            activated_by uuid REFERENCES operators(operator_id),
            resolution_note text,
            resolved_at timestamptz,
            resolved_by uuid REFERENCES operators(operator_id),
            CHECK (
                (is_paused AND trigger_kind IS NOT NULL AND reason_category IS NOT NULL
                    AND activated_at IS NOT NULL)
                OR NOT is_paused
            )
        );
        INSERT INTO global_pause_state (singleton) VALUES (true);

        CREATE TABLE global_pause_events (
            event_id uuid PRIMARY KEY,
            action text NOT NULL CHECK (action IN ('activated', 'resolved')),
            trigger_kind text NOT NULL,
            reason_category text NOT NULL,
            actor_id uuid NOT NULL REFERENCES operators(operator_id),
            occurred_at timestamptz NOT NULL,
            resolution_note text
        );
        CREATE TRIGGER global_pause_events_are_immutable
            BEFORE UPDATE OR DELETE ON global_pause_events
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_cpsc_source_change();

        CREATE TABLE evaluation_audits (
            audit_id uuid PRIMARY KEY,
            evaluation_id uuid NOT NULL UNIQUE
                REFERENCES evidence_evaluations(evaluation_id),
            founder_id uuid NOT NULL REFERENCES operators(operator_id),
            decision text NOT NULL CHECK (decision IN ('approved', 'rejected')),
            reason_category text,
            audited_at timestamptz NOT NULL,
            CHECK (
                (decision = 'approved' AND reason_category IS NULL)
                OR (decision = 'rejected' AND reason_category IS NOT NULL)
            )
        );
        CREATE TRIGGER evaluation_audits_are_immutable
            BEFORE UPDATE OR DELETE ON evaluation_audits
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_cpsc_source_change();

        CREATE TABLE evaluation_releases (
            evaluation_id uuid PRIMARY KEY
                REFERENCES evidence_evaluations(evaluation_id),
            released_at timestamptz NOT NULL
        );
        CREATE TRIGGER evaluation_releases_are_immutable
            BEFORE UPDATE OR DELETE ON evaluation_releases
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_cpsc_source_change();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE evaluation_releases;
        ALTER TABLE evidence_evaluations
            DISABLE TRIGGER evidence_evaluations_are_immutable;
        UPDATE evidence_evaluations SET released_at = evaluated_at
            WHERE released_at IS NULL;
        ALTER TABLE evidence_evaluations ALTER COLUMN released_at SET NOT NULL;
        ALTER TABLE evidence_evaluations
            ENABLE TRIGGER evidence_evaluations_are_immutable;
        DROP TABLE evaluation_audits;
        DROP TABLE global_pause_events;
        DROP TABLE global_pause_state;
        DROP TABLE audit_gate_state;
        """
    )
