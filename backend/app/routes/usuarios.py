from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_session
from app.db.models import Usuario
from app.dependencies import get_current_admin
from app.routes.auth import UsuarioResponse
from app.security import hash_password

router = APIRouter(prefix="/usuarios", tags=["usuarios"])

PERFIS = "^(SDR|CLOSER|GERENTE|DIRETOR|ANALISTA|ADMIN)$"


class CriarUsuarioBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)
    nome: str = Field(min_length=1, max_length=255)
    perfil: str = Field(default="SDR", pattern=PERFIS)


def _to_response(u: Usuario) -> UsuarioResponse:
    return UsuarioResponse(id=str(u.id), email=u.email, nome=u.nome, perfil=u.perfil)


@router.get("", response_model=list[UsuarioResponse], summary="Lista os usuários (admin)")
async def listar(
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[Usuario, Depends(get_current_admin)],
):
    rows = (await session.execute(select(Usuario).order_by(Usuario.nome))).scalars().all()
    return [_to_response(u) for u in rows]


@router.post(
    "",
    response_model=UsuarioResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Cria um usuário (admin)",
)
async def criar(
    body: CriarUsuarioBody,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[Usuario, Depends(get_current_admin)],
):
    exists = await session.execute(select(Usuario.id).where(Usuario.email == body.email))
    if exists.scalar_one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, "E-mail já cadastrado")
    user = Usuario(
        email=body.email,
        hashed_password=hash_password(body.password),
        nome=body.nome,
        perfil=body.perfil,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return _to_response(user)
