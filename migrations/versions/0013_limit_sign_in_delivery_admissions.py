"""Record globally capped sign-in delivery admissions.

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE sign_in_delivery_admissions (
            admission_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            admitted_at timestamptz NOT NULL
        );
        CREATE INDEX sign_in_delivery_admissions_window
            ON sign_in_delivery_admissions (admitted_at);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE sign_in_delivery_admissions;")
