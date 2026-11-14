"""oportunidade: etapa do funil no RD Station CRM

Revision ID: 006
Revises: 005
Create Date: 2026-11-14

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("oportunidade", sa.Column("rd_stage_id", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("oportunidade", "rd_stage_id")
