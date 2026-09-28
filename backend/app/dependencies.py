import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_session
from app.db.models import Usuario
from app.security import decode_token_safe

security = HTTPBearer(auto_error=False)


async def get_current_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Usuario:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token ausente")
    payload = decode_token_safe(creds.credentials)
    if not payload or "sub" not in payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido")
    try:
        uid = uuid.UUID(payload["sub"])
    except (ValueError, TypeError) as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido") from e
    row = await session.get(Usuario, uid)
    if row is None or not row.ativo:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Usuário inválido")
    return row


def pode_ver_todos_oportunidades(usuario: Usuario) -> bool:
    return usuario.perfil in ("GERENTE", "DIRETOR", "ANALISTA", "ADMIN")


async def get_current_admin(
    user: Annotated[Usuario, Depends(get_current_user)],
) -> Usuario:
    """Restringe a rota a administradores (gestão de usuários)."""
    if user.perfil != "ADMIN":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Acesso restrito a administradores")
    return user
