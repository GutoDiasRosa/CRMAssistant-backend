"""anotações (interacao): ligadas a lead e/ou oportunidade, com data e retorno

Revision ID: 007
Revises: 006
Create Date: 2026-11-14

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("interacao", "oportunidade_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)
    op.add_column(
        "interacao",
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("leads.id"), nullable=True),
    )
    op.add_column(
        "interacao",
        sa.Column("data", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.add_column("interacao", sa.Column("retorno_em", sa.Date(), nullable=True))
    op.create_index("ix_interacao_lead_id", "interacao", ["lead_id"])
    op.create_index("ix_interacao_data", "interacao", ["data"])
    op.create_index("ix_interacao_retorno_em", "interacao", ["retorno_em"])
    # Anotações antigas: data = criação; lead = o da oportunidade.
    op.execute("UPDATE interacao SET data = created_at WHERE created_at IS NOT NULL")
    op.execute(
        "UPDATE interacao i SET lead_id = o.lead_id FROM oportunidade o "
        "WHERE i.oportunidade_id = o.id AND i.lead_id IS NULL"
    )
    op.create_check_constraint(
        "ck_interacao_lead_ou_oportunidade", "interacao", "lead_id IS NOT NULL OR oportunidade_id IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_constraint("ck_interacao_lead_ou_oportunidade", "interacao", type_="check")
    op.execute("DELETE FROM interacao WHERE oportunidade_id IS NULL")
    op.drop_index("ix_interacao_retorno_em", table_name="interacao")
    op.drop_index("ix_interacao_data", table_name="interacao")
    op.drop_index("ix_interacao_lead_id", table_name="interacao")
    op.drop_column("interacao", "retorno_em")
    op.drop_column("interacao", "data")
    op.drop_column("interacao", "lead_id")
    op.alter_column("interacao", "oportunidade_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)
