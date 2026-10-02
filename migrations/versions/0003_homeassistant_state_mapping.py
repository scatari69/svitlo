"""Add configurable Home Assistant state mapping.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("homeassistant_device_configs") as batch:
        batch.add_column(sa.Column("on_state", sa.String(255), nullable=False, server_default="on"))
        batch.add_column(
            sa.Column("off_state", sa.String(255), nullable=False, server_default="off")
        )
        batch.create_check_constraint(
            "ck_ha_state_mapping",
            "length(trim(on_state)) > 0 AND length(trim(off_state)) > 0 "
            "AND on_state <> off_state AND on_state NOT IN ('unknown', 'unavailable') "
            "AND off_state NOT IN ('unknown', 'unavailable')",
        )


def downgrade() -> None:
    with op.batch_alter_table("homeassistant_device_configs") as batch:
        batch.drop_constraint("ck_ha_state_mapping", type_="check")
        batch.drop_column("off_state")
        batch.drop_column("on_state")
