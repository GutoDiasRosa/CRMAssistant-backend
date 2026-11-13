"""rd station config completa (ativação, usuário responsável, tokens da API e do webhook)

Revision ID: 004
Revises: 003
Create Date: 2026-11-13

"""

import secrets
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A configuração passa a existir antes das credenciais (ex.: só o usuário responsável).
    op.alter_column("rd_station_config", "client_id", existing_type=sa.String(255), nullable=True)
    op.alter_column("rd_station_config", "client_secret", existing_type=sa.Text(), nullable=True)
    op.add_column(
        "rd_station_config",
        sa.Column("ativo", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "rd_station_config",
        sa.Column(
            "usuario_integracao_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("usuario.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("rd_station_config", sa.Column("token_publico", sa.Text(), nullable=True))
    op.add_column("rd_station_config", sa.Column("token_privado", sa.Text(), nullable=True))
    op.add_column("rd_station_config", sa.Column("webhook_token", sa.String(64), nullable=True))
    # Configuração já existente ganha um token de webhook.
    conn = op.get_bind()
    ids = conn.execute(sa.text("SELECT id FROM rd_station_config")).scalars().all()
    for config_id in ids:
        conn.execute(
            sa.text("UPDATE rd_station_config SET webhook_token = :t WHERE id = :id"),
            {"t": secrets.token_urlsafe(32), "id": config_id},
        )


def downgrade() -> None:
    op.drop_column("rd_station_config", "webhook_token")
    op.drop_column("rd_station_config", "token_privado")
    op.drop_column("rd_station_config", "token_publico")
    op.drop_column("rd_station_config", "usuario_integracao_id")
    op.drop_column("rd_station_config", "ativo")
    op.execute("DELETE FROM rd_station_config WHERE client_id IS NULL OR client_secret IS NULL")
    op.alter_column("rd_station_config", "client_secret", existing_type=sa.Text(), nullable=False)
    op.alter_column("rd_station_config", "client_id", existing_type=sa.String(255), nullable=False)
