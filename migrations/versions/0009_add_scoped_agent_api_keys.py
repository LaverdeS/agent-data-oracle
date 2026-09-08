"""Persist scoped, revocable delegated API credentials.

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE agent_api_keys (
            agent_key_id uuid PRIMARY KEY,
            operator_id uuid NOT NULL REFERENCES operators(operator_id),
            secret_prefix text NOT NULL UNIQUE,
            secret_salt bytea NOT NULL,
            secret_hash bytea NOT NULL,
            scopes text[] NOT NULL,
            created_at timestamptz NOT NULL,
            last_used_at timestamptz,
            revoked_at timestamptz,
            CHECK (cardinality(scopes) BETWEEN 1 AND 5),
            CHECK (scopes <@ ARRAY[
                'queues:submit', 'queues:read', 'queues:refresh',
                'evidence:read', 'reviews:report-agent'
            ]::text[])
        );
        CREATE INDEX agent_api_keys_active_operator
            ON agent_api_keys (operator_id) WHERE revoked_at IS NULL;

        CREATE TABLE agent_api_request_attempts (
            attempt_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            agent_key_id uuid NOT NULL REFERENCES agent_api_keys(agent_key_id),
            attempted_at timestamptz NOT NULL
        );
        CREATE INDEX agent_api_request_attempts_window
            ON agent_api_request_attempts (agent_key_id, attempted_at);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE agent_api_request_attempts;
        DROP TABLE agent_api_keys;
        """
    )
