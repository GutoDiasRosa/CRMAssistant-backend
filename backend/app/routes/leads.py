"""Leads (contatos do RD Station CRM): listagem, detalhe, criação e edição (RF07)."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.rd_station_sync import envio
from app.db.database import get_session
from app.db.models import Lead, Oportunidade, Usuario
from app.dependencies import get_current_user, pode_ver_todos_oportunidades
from app.routes.oportunidades import EnvioRd, Pessoa, _responsavel, enviar_ao_rd

router = APIRouter(prefix="/api/crm/leads", tags=["crm"])

LIMITE_LISTA = 200


class LeadBody(BaseModel):
    nome: str = Field(min_length=1, max_length=512)
    email: EmailStr | None = None
    telefone: str | None = Field(default=None, max_length=64)
    # Sem valor, o responsável é quem cria. Perfis sem visão total só atribuem a si mesmos.
    usuario_id: UUID | None = None


class LeadResumo(BaseModel):
    id: UUID
    nome: str
    email: str | None
    telefone: str | None
    responsavel: Pessoa | None
    oportunidades: int
    sincronizado: bool
    updated_at: datetime


class OportunidadeDoLead(BaseModel):
    id: UUID
    nome: str
    etapa_funil: str | None
    valor: float | None
    status: str | None


class LeadDetalhe(BaseModel):
    id: UUID
    nome: str
    email: str | None
    telefone: str | None
    responsavel: Pessoa | None
    sincronizado: bool
    oportunidades: list[OportunidadeDoLead]
    created_at: datetime
    updated_at: datetime


class LeadResposta(BaseModel):
    lead: LeadDetalhe
    rd: EnvioRd


async def _carregar(session: AsyncSession, user: Usuario, lead_id: UUID) -> Lead:
    stmt = (
        select(Lead)
        .where(Lead.id == lead_id)
        .options(selectinload(Lead.usuario), selectinload(Lead.oportunidades))
        .execution_options(populate_existing=True)
    )
    lead = (await session.execute(stmt)).scalar_one_or_none()
    # RF07: quem não tem visão total só enxerga os próprios leads.
    if lead is None or (not pode_ver_todos_oportunidades(user) and lead.usuario_id != user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead não encontrado")
    return lead


def _detalhe(lead: Lead) -> LeadDetalhe:
    return LeadDetalhe(
        id=lead.id,
        nome=lead.nome,
        email=lead.email,
        telefone=lead.telefone,
        responsavel=Pessoa(id=lead.usuario.id, nome=lead.usuario.nome) if lead.usuario else None,
        sincronizado=lead.rd_lead_id is not None,
        oportunidades=[
            OportunidadeDoLead(
                id=op.id,
                nome=op.nome,
                etapa_funil=op.etapa_funil,
                valor=float(op.valor) if op.valor is not None else None,
                status=op.status,
            )
            for op in sorted(lead.oportunidades, key=lambda o: o.updated_at, reverse=True)
        ],
        created_at=lead.created_at,
        updated_at=lead.updated_at,
    )


def _aplicar(lead: Lead, body: LeadBody, responsavel_id: UUID) -> None:
    lead.nome = body.nome.strip()
    lead.email = str(body.email) if body.email else None
    lead.telefone = (body.telefone or "").strip() or None
    lead.usuario_id = responsavel_id


@router.get("", response_model=list[LeadResumo], summary="Lista os leads, com busca e filtro por vendedor (RF07)")
async def listar(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[Usuario, Depends(get_current_user)],
    busca: Annotated[str | None, Query(max_length=100, description="Parte do nome, e-mail ou telefone")] = None,
    usuario_id: Annotated[UUID | None, Query(description="Filtra pelo vendedor responsável")] = None,
):
    qtd = (
        select(Oportunidade.lead_id, func.count(Oportunidade.id).label("qtd"))
        .group_by(Oportunidade.lead_id)
        .subquery()
    )
    stmt = (
        select(Lead, func.coalesce(qtd.c.qtd, 0))
        .outerjoin(qtd, qtd.c.lead_id == Lead.id)
        .options(selectinload(Lead.usuario))
        .order_by(Lead.updated_at.desc())
        .limit(LIMITE_LISTA)
    )
    if not pode_ver_todos_oportunidades(user):
        stmt = stmt.where(Lead.usuario_id == user.id)
    elif usuario_id:
        stmt = stmt.where(Lead.usuario_id == usuario_id)
    if busca and busca.strip():
        padrao = f"%{busca.strip()}%"
        stmt = stmt.where(Lead.nome.ilike(padrao) | Lead.email.ilike(padrao) | Lead.telefone.ilike(padrao))
    linhas = (await session.execute(stmt)).all()
    return [
        LeadResumo(
            id=lead.id,
            nome=lead.nome,
            email=lead.email,
            telefone=lead.telefone,
            responsavel=Pessoa(id=lead.usuario.id, nome=lead.usuario.nome) if lead.usuario else None,
            oportunidades=int(n),
            sincronizado=lead.rd_lead_id is not None,
            updated_at=lead.updated_at,
        )
        for lead, n in linhas
    ]


@router.get("/{lead_id}", response_model=LeadDetalhe, summary="Detalhe do lead com as oportunidades dele (RF07)")
async def obter(
    lead_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[Usuario, Depends(get_current_user)],
):
    return _detalhe(await _carregar(session, user, lead_id))


@router.post(
    "",
    response_model=LeadResposta,
    status_code=status.HTTP_201_CREATED,
    summary="Cria um lead e o envia ao RD Station CRM como contato",
)
async def criar(
    body: LeadBody,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[Usuario, Depends(get_current_user)],
):
    responsavel_id = await _responsavel(session, user, body.usuario_id)
    lead = Lead()
    _aplicar(lead, body, responsavel_id)
    session.add(lead)
    await session.commit()
    lead_id = lead.id  # uma falha no envio faz rollback e expira o objeto
    rd = await enviar_ao_rd(session, envio.enviar_lead, "lead", lead_id)
    return LeadResposta(lead=_detalhe(await _carregar(session, user, lead_id)), rd=rd)


@router.put(
    "/{lead_id}",
    response_model=LeadResposta,
    summary="Atualiza um lead e envia a alteração ao RD Station CRM",
)
async def atualizar(
    lead_id: UUID,
    body: LeadBody,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[Usuario, Depends(get_current_user)],
):
    lead = await _carregar(session, user, lead_id)
    responsavel_id = await _responsavel(session, user, body.usuario_id or lead.usuario_id)
    _aplicar(lead, body, responsavel_id)
    await session.commit()
    rd = await enviar_ao_rd(session, envio.enviar_lead, "lead", lead_id)
    return LeadResposta(lead=_detalhe(await _carregar(session, user, lead_id)), rd=rd)
