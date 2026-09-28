from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_session
from app.db.models import (
    ConversaChatbot,
    Interacao,
    Lead,
    Mensagem,
    Oportunidade,
    Relatorio,
    Usuario,
)
from app.dependencies import get_current_user
from app.security import create_access_token, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginBody(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UsuarioResponse(BaseModel):
    id: str
    email: str
    nome: str
    perfil: str


@router.get("/me", response_model=UsuarioResponse, summary="Dados do usuário autenticado")
async def me(current: Annotated[Usuario, Depends(get_current_user)]):
    return UsuarioResponse(
        id=str(current.id), email=current.email, nome=current.nome, perfil=current.perfil
    )


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginBody, session: Annotated[AsyncSession, Depends(get_session)]):
    r = await session.execute(select(Usuario).where(Usuario.email == body.email))
    user = r.scalar_one_or_none()
    if user is None or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Credenciais inválidas")
    if not user.ativo:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Usuário inativo")
    token = create_access_token(str(user.id), extra={"perfil": user.perfil})
    return TokenResponse(access_token=token)


@router.delete(
    "/me/lgpd",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="LGPD — remove o usuário autenticado, conversas e desvincula leads/oportunidades",
)
async def lgpd_delete_me(
    session: Annotated[AsyncSession, Depends(get_session)],
    current: Annotated[Usuario, Depends(get_current_user)],
):
    uid = current.id
    conv_subq = select(ConversaChatbot.id).where(ConversaChatbot.usuario_id == uid)
    await session.execute(delete(Mensagem).where(Mensagem.conversa_id.in_(conv_subq)))
    await session.execute(delete(ConversaChatbot).where(ConversaChatbot.usuario_id == uid))
    await session.execute(delete(Relatorio).where(Relatorio.usuario_id == uid))
    await session.execute(update(Lead).where(Lead.usuario_id == uid).values(usuario_id=None))
    await session.execute(update(Oportunidade).where(Oportunidade.usuario_id == uid).values(usuario_id=None))
    await session.execute(delete(Interacao).where(Interacao.usuario_id == uid))
    await session.execute(delete(Usuario).where(Usuario.id == uid))
    await session.commit()
