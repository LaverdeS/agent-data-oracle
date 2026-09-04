"""Enforce internally consistent source and projection lineage.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DROP INDEX one_completed_revision_per_completion_time;

        ALTER TABLE cpsc_ingestion_runs
            ADD COLUMN run_sequence bigint GENERATED ALWAYS AS IDENTITY UNIQUE;

        ALTER TABLE cpsc_source_revisions
            ADD CONSTRAINT cpsc_revision_owns_run
            UNIQUE (revision_id, run_id);

        ALTER TABLE cpsc_revision_records
            ADD CONSTRAINT cpsc_revision_record_selects_version
            UNIQUE (revision_id, recall_id, version_id);

        ALTER TABLE cpsc_source_observations
            ADD CONSTRAINT cpsc_observation_revision_owns_run
            FOREIGN KEY (revision_id, run_id)
            REFERENCES cpsc_source_revisions(revision_id, run_id),
            ADD CONSTRAINT cpsc_observation_matches_revision_record
            FOREIGN KEY (revision_id, recall_id, version_id)
            REFERENCES cpsc_revision_records(revision_id, recall_id, version_id);

        CREATE OR REPLACE FUNCTION require_pending_revision_row()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM cpsc_source_revisions
                WHERE revision_id = NEW.revision_id AND state = 'pending'
            ) THEN
                RAISE EXCEPTION 'source rows require a pending CPSC revision'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$;

        CREATE TRIGGER cpsc_revision_records_require_pending_revision
            BEFORE INSERT ON cpsc_revision_records
            FOR EACH ROW EXECUTE FUNCTION require_pending_revision_row();

        CREATE TRIGGER cpsc_observations_require_pending_revision
            BEFORE INSERT ON cpsc_source_observations
            FOR EACH ROW EXECUTE FUNCTION require_pending_revision_row();

        CREATE OR REPLACE FUNCTION validate_current_cpsc_projection()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            selected_revision uuid;
            selected_count integer;
        BEGIN
            SELECT current.revision_id, revisions.record_count
            INTO selected_revision, selected_count
            FROM cpsc_current_source_revision AS current
            JOIN cpsc_source_revisions AS revisions
                ON revisions.revision_id = current.revision_id
            WHERE current.singleton = true AND revisions.state = 'completed';

            IF selected_revision IS NULL THEN
                IF EXISTS (SELECT 1 FROM cpsc_current_records) THEN
                    RAISE EXCEPTION 'current CPSC rows require a selected revision'
                        USING ERRCODE = 'integrity_constraint_violation';
                END IF;
                RETURN NULL;
            END IF;

            IF (SELECT count(*) FROM cpsc_current_records) <> selected_count
                OR EXISTS (
                    SELECT 1
                    FROM cpsc_current_records AS current_record
                    LEFT JOIN cpsc_revision_records AS revision_record
                        ON revision_record.revision_id = current_record.revision_id
                        AND revision_record.recall_id = current_record.recall_id
                        AND revision_record.version_id = current_record.version_id
                    LEFT JOIN cpsc_recall_versions AS version
                        ON version.version_id = current_record.version_id
                        AND version.recall_id = current_record.recall_id
                    WHERE current_record.revision_id <> selected_revision
                        OR revision_record.revision_id IS NULL
                        OR version.version_id IS NULL
                        OR current_record.recall_number
                            IS DISTINCT FROM version.recall_number
                        OR current_record.recall_date_literal
                            IS DISTINCT FROM version.recall_date_literal
                        OR current_record.last_publish_date_literal
                            IS DISTINCT FROM version.last_publish_date_literal
                        OR current_record.official_url
                            IS DISTINCT FROM version.official_url
                        OR current_record.normalized_record
                            IS DISTINCT FROM version.raw_record
                )
            THEN
                RAISE EXCEPTION 'current CPSC projection has inconsistent lineage'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NULL;
        END;
        $$;

        CREATE CONSTRAINT TRIGGER cpsc_current_selection_is_consistent
            AFTER INSERT OR UPDATE OR DELETE ON cpsc_current_source_revision
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION validate_current_cpsc_projection();

        CREATE CONSTRAINT TRIGGER cpsc_current_records_are_consistent
            AFTER INSERT OR UPDATE OR DELETE ON cpsc_current_records
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION validate_current_cpsc_projection();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER cpsc_current_records_are_consistent ON cpsc_current_records;
        DROP TRIGGER cpsc_current_selection_is_consistent
            ON cpsc_current_source_revision;
        DROP FUNCTION validate_current_cpsc_projection();
        DROP TRIGGER cpsc_observations_require_pending_revision
            ON cpsc_source_observations;
        DROP TRIGGER cpsc_revision_records_require_pending_revision
            ON cpsc_revision_records;
        DROP FUNCTION require_pending_revision_row();
        ALTER TABLE cpsc_source_observations
            DROP CONSTRAINT cpsc_observation_matches_revision_record,
            DROP CONSTRAINT cpsc_observation_revision_owns_run;
        ALTER TABLE cpsc_revision_records
            DROP CONSTRAINT cpsc_revision_record_selects_version;
        ALTER TABLE cpsc_source_revisions
            DROP CONSTRAINT cpsc_revision_owns_run;
        ALTER TABLE cpsc_ingestion_runs DROP COLUMN run_sequence;
        CREATE UNIQUE INDEX one_completed_revision_per_completion_time
            ON cpsc_source_revisions (completed_at)
            WHERE state = 'completed';
        """
    )
