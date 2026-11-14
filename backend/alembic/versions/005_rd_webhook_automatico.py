"""rd station: webhooks criados pelo sistema; remove os tokens público e privado

Revision ID: 005
Revises: 004
Create Date: 2026-11-14

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("rd_station_config", "token_privado")
    op.drop_column("rd_station_config", "token_publico")
    op.add_column("rd_station_config", sa.Column("webhooks", sa.JSON(), nullable=True))
    op.add_column("rd_station_config", sa.Column("webhook_erro", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("rd_station_config", "webhook_erro")
    op.drop_column("rd_station_config", "webhooks")
    op.add_column("rd_station_config", sa.Column("token_publico", sa.Text(), nullable=True))
    op.add_column("rd_station_config", sa.Column("token_privado", sa.Text(), nullable=True))
