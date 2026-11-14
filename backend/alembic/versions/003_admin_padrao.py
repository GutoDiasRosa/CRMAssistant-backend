"""admin padrão (desativada)

Criava o administrador admin@crmassist.dev com a senha "admin". O administrador agora vem
das variáveis ADMIN_EMAIL e ADMIN_PASSWORD, aplicadas a cada inicialização da API
(app/services/admin_inicial.py). A revisão continua existindo para manter a cadeia de
migrações dos bancos que já a aplicaram.

Revision ID: 003
Revises: 002
Create Date: 2026-11-13

"""

from typing import Sequence, Union

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
