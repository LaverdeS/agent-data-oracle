"""Add passwordless human and founder access records.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE operators (
            operator_id uuid PRIMARY KEY,
            email_normalized text NOT NULL UNIQUE,
            operator_type text CHECK (
                operator_type IN ('business_operator', 'agent_operator')
            ),
            sells_into_us boolean,
            declaration_recorded_at timestamptz,
            is_founder boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL,
            CHECK (
                (operator_type IS NULL AND sells_into_us IS NULL
                    AND declaration_recorded_at IS NULL)
                OR
                (operator_type IS NOT NULL AND sells_into_us IS NOT NULL
                    AND declaration_recorded_at IS NOT NULL)
            )
        );

        CREATE TABLE auth_attempts (
            attempt_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            subject_kind text NOT NULL CHECK (subject_kind IN ('email', 'network')),
            subject_hash character(64) NOT NULL,
            attempted_at timestamptz NOT NULL
        );
        CREATE INDEX auth_attempts_window
            ON auth_attempts (subject_kind, subject_hash, attempted_at);

        CREATE TABLE login_tokens (
            token_id uuid PRIMARY KEY,
            email_normalized text NOT NULL,
            token_hash character(64) NOT NULL UNIQUE,
            requested_at timestamptz NOT NULL,
            expires_at timestamptz NOT NULL,
            consumed_at timestamptz,
            CHECK (expires_at > requested_at),
            CHECK (consumed_at IS NULL OR consumed_at >= requested_at)
        );

        CREATE TABLE browser_sessions (
            session_id uuid PRIMARY KEY,
            operator_id uuid NOT NULL REFERENCES operators(operator_id),
            token_hash character(64) NOT NULL UNIQUE,
            created_at timestamptz NOT NULL,
            expires_at timestamptz NOT NULL,
            reauthenticated_at timestamptz NOT NULL,
            founder_second_factor_at timestamptz,
            revoked_at timestamptz,
            CHECK (expires_at > created_at),
            CHECK (reauthenticated_at >= created_at),
            CHECK (revoked_at IS NULL OR revoked_at >= created_at)
        );

        CREATE TABLE founder_totp_factors (
            operator_id uuid PRIMARY KEY REFERENCES operators(operator_id),
            confirmed_at timestamptz NOT NULL
        );

        CREATE TABLE auth_recovery_codes (
            recovery_code_id uuid PRIMARY KEY,
            operator_id uuid NOT NULL REFERENCES founder_totp_factors(operator_id),
            salt bytea NOT NULL,
            code_hash bytea NOT NULL,
            created_at timestamptz NOT NULL,
            used_at timestamptz
        );
        CREATE INDEX auth_recovery_codes_operator
            ON auth_recovery_codes (operator_id) WHERE used_at IS NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE auth_recovery_codes;
        DROP TABLE founder_totp_factors;
        DROP TABLE browser_sessions;
        DROP TABLE login_tokens;
        DROP TABLE auth_attempts;
        DROP TABLE operators;
        """
    )
