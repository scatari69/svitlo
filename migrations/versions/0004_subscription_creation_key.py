"""Make subscription setup saves idempotent.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("schedule_subscriptions") as batch:
        batch.add_column(sa.Column("creation_key", sa.String(32), nullable=True))
        batch.create_unique_constraint("uq_subscription_creation", ["user_id", "creation_key"])
        batch.create_check_constraint(
            "ck_subscription_creation", "creation_key IS NULL OR length(creation_key) = 32"
        )


def downgrade() -> None:
    with op.batch_alter_table("schedule_subscriptions") as batch:
        batch.drop_constraint("ck_subscription_creation", type_="check")
        batch.drop_constraint("uq_subscription_creation", type_="unique")
        batch.drop_column("creation_key")
