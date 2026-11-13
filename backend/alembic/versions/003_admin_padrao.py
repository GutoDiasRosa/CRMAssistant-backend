"""admin padrão

Cria o usuário administrador inicial (admin@crmassist.dev / admin) junto com o banco.
Se o e-mail já existir, nada é alterado.

Revision ID: 003
Revises: 002
Create Date: 2026-11-13

"""

import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.security import hash_password

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ADMIN_EMAIL = "admin@crmassist.dev"
ADMIN_SENHA = "admin"
ADMIN_NOME = "Administrador"


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            INSERT INTO usuario (id, email, hashed_password, nome, perfil, ativo)
            VALUES (:id, :email, :hashed_password, :nome, 'ADMIN', true)
            ON CONFLICT (email) DO NOTHING
            """
        ),
        {
            "id": uuid.uuid4(),
            "email": ADMIN_EMAIL,
            "hashed_password": hash_password(ADMIN_SENHA),
            "nome": ADMIN_NOME,
        },
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text("DELETE FROM usuario WHERE email = :email"), {"email": ADMIN_EMAIL}
    )
