"""Administrador definido pelas variáveis ADMIN_EMAIL e ADMIN_PASSWORD.

Roda a cada inicialização da API (ou seja, a cada publicação): cria o administrador se
ele não existir ou, se já existir, garante o perfil ADMIN, a conta ativa e a senha da
variável. Para trocar e-mail ou senha, basta alterar as variáveis e publicar de novo.
"""

import logging

from sqlalchemy import func, select

from app.config import get_settings
from app.db.database import async_session_factory
from app.db.models import PerfilUsuario, Usuario
from app.security import hash_password, verify_password

logger = logging.getLogger(__name__)

SENHA_MINIMA = 6  # mesma regra do cadastro de usuários
NOME_PADRAO = "Administrador"

# Criado pela antiga migração 003 com uma senha que está no repositório público.
ADMIN_LEGADO_EMAIL = "admin@crmassist.dev"
ADMIN_LEGADO_SENHA = "admin"


async def garantir_admin() -> None:
    s = get_settings()
    email = s.admin_email.strip()
    senha = s.admin_password
    if not email or not senha:
        logger.warning("ADMIN_EMAIL/ADMIN_PASSWORD não definidas: administrador não criado nem atualizado")
        return
    if len(senha) < SENHA_MINIMA:
        logger.error(
            "ADMIN_PASSWORD precisa ter pelo menos %d caracteres: administrador não atualizado", SENHA_MINIMA
        )
        return

    async with async_session_factory() as session, session.begin():
        admin = (
            await session.execute(select(Usuario).where(func.lower(Usuario.email) == email.lower()))
        ).scalar_one_or_none()
        if admin is None:
            session.add(
                Usuario(
                    email=email,
                    nome=NOME_PADRAO,
                    perfil=PerfilUsuario.ADMIN.value,
                    hashed_password=hash_password(senha),
                    ativo=True,
                )
            )
            logger.info("Administrador %s criado a partir de ADMIN_EMAIL", email)
        else:
            admin.perfil = PerfilUsuario.ADMIN.value
            admin.ativo = True
            if not verify_password(senha, admin.hashed_password):
                admin.hashed_password = hash_password(senha)
                logger.info("Senha do administrador %s redefinida a partir de ADMIN_PASSWORD", email)

        # O admin antigo, com senha pública, sai de cena quando outro administrador assume.
        if email.lower() != ADMIN_LEGADO_EMAIL:
            legado = (
                await session.execute(
                    select(Usuario).where(Usuario.email == ADMIN_LEGADO_EMAIL, Usuario.ativo.is_(True))
                )
            ).scalar_one_or_none()
            if legado and verify_password(ADMIN_LEGADO_SENHA, legado.hashed_password):
                legado.ativo = False
                logger.warning("Administrador padrão %s (senha pública) desativado", ADMIN_LEGADO_EMAIL)
