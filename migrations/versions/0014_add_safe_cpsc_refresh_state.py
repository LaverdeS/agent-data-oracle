"""Add local CPSC refresh state, modes, and reconciliation tombstones.

Revision ID: 0014
Revises: 0013
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE cpsc_ingestion_runs ADD COLUMN refresh_mode text NOT NULL
            DEFAULT 'fixture'
            CHECK (refresh_mode IN ('fixture', 'daily', 'full'));
        ALTER TABLE cpsc_ingestion_runs ADD COLUMN retrieval_attempts integer NOT NULL
            DEFAULT 0 CHECK (retrieval_attempts >= 0 AND retrieval_attempts <= 3);

        CREATE TABLE cpsc_source_tombstones (
            tombstone_id uuid PRIMARY KEY,
            revision_id uuid NOT NULL REFERENCES cpsc_source_revisions(revision_id),
            recall_id bigint NOT NULL REFERENCES cpsc_recalls(recall_id),
            last_seen_revision_id uuid NOT NULL
                REFERENCES cpsc_source_revisions(revision_id),
            recorded_at timestamptz NOT NULL,
            UNIQUE (revision_id, recall_id)
        );
        CREATE TRIGGER cpsc_source_tombstones_are_immutable
            BEFORE UPDATE OR DELETE ON cpsc_source_tombstones
            FOR EACH ROW EXECUTE FUNCTION reject_immutable_cpsc_source_change();

        CREATE TABLE cpsc_source_refresh_state (
            singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
            last_successful_observed_at timestamptz,
            last_failure_kind text,
            integrity_pause_reason text,
            updated_at timestamptz NOT NULL
        );
        INSERT INTO cpsc_source_refresh_state (singleton, updated_at)
            VALUES (true, CURRENT_TIMESTAMP);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE cpsc_source_refresh_state;
        DROP TABLE cpsc_source_tombstones;
        ALTER TABLE cpsc_ingestion_runs DROP COLUMN retrieval_attempts;
        ALTER TABLE cpsc_ingestion_runs DROP COLUMN refresh_mode;
        """
    )
