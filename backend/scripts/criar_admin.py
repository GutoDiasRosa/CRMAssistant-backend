"""
Cria (ou redefine a senha de) um usuário ADMIN. Útil para o primeiro acesso em produção,
onde o seed de demonstração não deve ser usado.

Uso (dentro de backend/, ou no Console do componente "api" na DigitalOcean):
    python -m scripts.criar_admin email@empresa.com "Nome Completo"

A senha é pedida no terminal e não fica no histórico.
"""

import asyncio
import getpass
import sys

from sqlalchemy import select

from app.db.database import async_session_factory, engine
from app.db.models import Usuario
from app.security import hash_password


async def main(email: str, nome: str, senha: str) -> None:
    async with async_session_factory() as session:
        user = (await session.execute(select(Usuario).where(Usuario.email == email))).scalar_one_or_none()
        if user is None:
            session.add(
                Usuario(email=email, nome=nome, perfil="ADMIN", hashed_password=hash_password(senha))
            )
            print(f"Administrador criado: {email}")
        else:
            user.perfil = "ADMIN"
            user.hashed_password = hash_password(senha)
            print(f"Usuário existente promovido a ADMIN e com senha redefinida: {email}")
        await session.commit()
    await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit('Uso: python -m scripts.criar_admin email@empresa.com "Nome Completo"')
    senha = getpass.getpass("Senha (mínimo 6 caracteres): ")
    if len(senha) < 6 or senha != getpass.getpass("Repita a senha: "):
        sys.exit("Senha curta ou diferente da confirmação.")
    asyncio.run(main(sys.argv[1].strip(), sys.argv[2].strip(), senha))
