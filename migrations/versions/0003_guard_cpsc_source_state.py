"""Guard terminal source state and current revision selection.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE cpsc_current_source_revision (
            singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
            revision_id uuid NOT NULL UNIQUE
                REFERENCES cpsc_source_revisions(revision_id),
            projected_at timestamptz NOT NULL
        );

        CREATE OR REPLACE FUNCTION guard_cpsc_ingestion_run_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' OR OLD.state <> 'pending' THEN
                RAISE EXCEPTION 'terminal CPSC ingestion runs are immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.state = 'pending'
                OR NEW.run_id IS DISTINCT FROM OLD.run_id
                OR NEW.source_url IS DISTINCT FROM OLD.source_url
                OR NEW.observed_at IS DISTINCT FROM OLD.observed_at
                OR NEW.raw_response IS DISTINCT FROM OLD.raw_response
                OR NEW.raw_response_sha256 IS DISTINCT FROM OLD.raw_response_sha256
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
            THEN
                RAISE EXCEPTION 'CPSC ingestion run evidence is immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$;

        CREATE OR REPLACE FUNCTION guard_cpsc_source_revision_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' OR OLD.state <> 'pending' THEN
                RAISE EXCEPTION 'terminal CPSC source revisions are immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF NEW.state = 'pending'
                OR NEW.revision_id IS DISTINCT FROM OLD.revision_id
                OR NEW.run_id IS DISTINCT FROM OLD.run_id
                OR NEW.created_at IS DISTINCT FROM OLD.created_at
            THEN
                RAISE EXCEPTION 'CPSC source revision identity is immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$;

        CREATE OR REPLACE FUNCTION require_completed_current_revision()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM cpsc_source_revisions
                WHERE revision_id = NEW.revision_id AND state = 'completed'
            ) THEN
                RAISE EXCEPTION 'current CPSC projection requires a completed revision'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$;

        CREATE TRIGGER cpsc_ingestion_runs_guard_terminal_state
            BEFORE UPDATE OR DELETE ON cpsc_ingestion_runs
            FOR EACH ROW EXECUTE FUNCTION guard_cpsc_ingestion_run_change();

        CREATE TRIGGER cpsc_source_revisions_guard_terminal_state
            BEFORE UPDATE OR DELETE ON cpsc_source_revisions
            FOR EACH ROW EXECUTE FUNCTION guard_cpsc_source_revision_change();

        CREATE TRIGGER cpsc_current_source_must_be_completed
            BEFORE INSERT OR UPDATE ON cpsc_current_source_revision
            FOR EACH ROW EXECUTE FUNCTION require_completed_current_revision();

        CREATE TRIGGER cpsc_current_records_must_be_completed
            BEFORE INSERT OR UPDATE ON cpsc_current_records
            FOR EACH ROW EXECUTE FUNCTION require_completed_current_revision();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER cpsc_current_records_must_be_completed
            ON cpsc_current_records;
        DROP TRIGGER cpsc_current_source_must_be_completed
            ON cpsc_current_source_revision;
        DROP TRIGGER cpsc_source_revisions_guard_terminal_state
            ON cpsc_source_revisions;
        DROP TRIGGER cpsc_ingestion_runs_guard_terminal_state
            ON cpsc_ingestion_runs;
        DROP FUNCTION require_completed_current_revision();
        DROP FUNCTION guard_cpsc_source_revision_change();
        DROP FUNCTION guard_cpsc_ingestion_run_change();
        DROP TABLE cpsc_current_source_revision;
        """
    )
