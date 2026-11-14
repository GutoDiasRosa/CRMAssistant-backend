"""oportunidade: data de fechamento (ganha ou perdida)

Revision ID: 008
Revises: 007
Create Date: 2026-11-14

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("oportunidade", sa.Column("fechada_em", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_oportunidade_fechada_em", "oportunidade", ["fechada_em"])
    # Oportunidades já encerradas: a última alteração é a melhor estimativa do fechamento.
    op.execute(
        "UPDATE oportunidade SET fechada_em = updated_at "
        "WHERE lower(status) IN ('won', 'ganho', 'ganha', 'lost', 'perdido', 'perdida')"
    )


def downgrade() -> None:
    op.drop_index("ix_oportunidade_fechada_em", table_name="oportunidade")
    op.drop_column("oportunidade", "fechada_em")
