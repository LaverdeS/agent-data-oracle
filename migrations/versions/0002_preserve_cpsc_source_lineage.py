"""Preserve immutable CPSC source lineage.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE cpsc_ingestion_runs (
            run_id uuid PRIMARY KEY,
            source_url text NOT NULL,
            observed_at timestamptz NOT NULL,
            raw_response bytea NOT NULL,
            raw_response_sha256 character(64) NOT NULL,
            state text NOT NULL CHECK (
                state IN ('pending', 'completed', 'failed', 'rejected')
            ),
            record_count integer NOT NULL DEFAULT 0 CHECK (record_count >= 0),
            error_code text,
            created_at timestamptz NOT NULL,
            finished_at timestamptz,
            CHECK (
                (state = 'pending' AND finished_at IS NULL AND error_code IS NULL)
                OR
                (state = 'completed' AND finished_at IS NOT NULL AND error_code IS NULL)
                OR
                (state IN ('failed', 'rejected') AND finished_at IS NOT NULL
                    AND error_code IS NOT NULL)
            )
        );

        CREATE TABLE cpsc_source_revisions (
            revision_id uuid PRIMARY KEY,
            run_id uuid NOT NULL UNIQUE REFERENCES cpsc_ingestion_runs(run_id),
            state text NOT NULL CHECK (
                state IN ('pending', 'completed', 'failed', 'rejected')
            ),
            completeness text NOT NULL CHECK (
                completeness IN ('unknown', 'complete', 'partial')
            ),
            record_count integer NOT NULL DEFAULT 0 CHECK (record_count >= 0),
            created_at timestamptz NOT NULL,
            completed_at timestamptz,
            CHECK (
                (state = 'completed' AND completeness = 'complete'
                    AND completed_at IS NOT NULL)
                OR
                (state <> 'completed' AND completed_at IS NULL)
            )
        );

        CREATE UNIQUE INDEX one_completed_revision_per_completion_time
            ON cpsc_source_revisions (completed_at)
            WHERE state = 'completed';

        CREATE TABLE cpsc_recalls (
            recall_id bigint PRIMARY KEY,
            first_observed_at timestamptz NOT NULL
        );

        CREATE TABLE cpsc_recall_versions (
            version_id uuid PRIMARY KEY,
            recall_id bigint NOT NULL REFERENCES cpsc_recalls(recall_id),
            content_hash character(64) NOT NULL,
            raw_record jsonb NOT NULL,
            recall_number text NOT NULL,
            recall_date_literal text,
            last_publish_date_literal text,
            official_url text NOT NULL,
            created_at timestamptz NOT NULL,
            UNIQUE (recall_id, content_hash),
            UNIQUE (version_id, recall_id)
        );

        CREATE TABLE cpsc_revision_records (
            revision_id uuid NOT NULL REFERENCES cpsc_source_revisions(revision_id),
            recall_id bigint NOT NULL REFERENCES cpsc_recalls(recall_id),
            version_id uuid NOT NULL,
            source_position integer NOT NULL CHECK (source_position >= 0),
            PRIMARY KEY (revision_id, recall_id),
            UNIQUE (revision_id, source_position),
            FOREIGN KEY (version_id, recall_id)
                REFERENCES cpsc_recall_versions(version_id, recall_id)
        );

        CREATE TABLE cpsc_source_observations (
            observation_id uuid PRIMARY KEY,
            run_id uuid NOT NULL REFERENCES cpsc_ingestion_runs(run_id),
            revision_id uuid NOT NULL,
            recall_id bigint NOT NULL,
            version_id uuid NOT NULL,
            observed_at timestamptz NOT NULL,
            UNIQUE (revision_id, recall_id),
            FOREIGN KEY (revision_id, recall_id)
                REFERENCES cpsc_revision_records(revision_id, recall_id),
            FOREIGN KEY (version_id, recall_id)
                REFERENCES cpsc_recall_versions(version_id, recall_id)
        );

        CREATE TABLE cpsc_current_records (
            recall_id bigint PRIMARY KEY REFERENCES cpsc_recalls(recall_id),
            revision_id uuid NOT NULL,
            version_id uuid NOT NULL,
            recall_number text NOT NULL,
            recall_date_literal text,
            last_publish_date_literal text,
            official_url text NOT NULL,
            normalized_record jsonb NOT NULL,
            projected_at timestamptz NOT NULL,
            FOREIGN KEY (revision_id, recall_id)
                REFERENCES cpsc_revision_records(revision_id, recall_id),
            FOREIGN KEY (version_id, recall_id)
                REFERENCES cpsc_recall_versions(version_id, recall_id)
        );

        CREATE OR REPLACE FUNCTION reject_immutable_cpsc_source_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION '% is immutable', TG_TABLE_NAME
                USING ERRCODE = 'integrity_constraint_violation';
        END;
        $$;

        CREATE TRIGGER cpsc_recall_versions_are_immutable
            BEFORE UPDATE OR DELETE ON cpsc_recall_versions
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_cpsc_source_change();

        CREATE TRIGGER cpsc_source_observations_are_immutable
            BEFORE UPDATE OR DELETE ON cpsc_source_observations
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_cpsc_source_change();

        CREATE TRIGGER cpsc_revision_records_are_immutable
            BEFORE UPDATE OR DELETE ON cpsc_revision_records
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_cpsc_source_change();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE cpsc_current_records;
        DROP TABLE cpsc_source_observations;
        DROP TABLE cpsc_revision_records;
        DROP TABLE cpsc_recall_versions;
        DROP TABLE cpsc_recalls;
        DROP TABLE cpsc_source_revisions;
        DROP TABLE cpsc_ingestion_runs;
        DROP FUNCTION reject_immutable_cpsc_source_change();
        """
    )
