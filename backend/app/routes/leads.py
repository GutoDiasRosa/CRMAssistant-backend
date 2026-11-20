"""Leads (contatos do RD Station CRM): listagem, detalhe, criação e edição (RF07)."""

from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from app.core.rd_station_sync import envio
from app.db.database import get_session
from app.db.models import STATUS_FECHADO, STATUS_GANHO, Lead, Oportunidade, Usuario
from app.dependencies import get_current_user, pode_ver_todos_oportunidades
from app.domain.repositories.crm import leads_visiveis, oportunidade_visivel_no_lead
from app.routes.oportunidades import EnvioRd, Pessoa, _responsavel, enviar_ao_rd

router = APIRouter(prefix="/api/crm/leads", tags=["crm"])

LIMITE_LISTA = 200
# Datas dos filtros são dias no horário de Brasília.
BRT = timezone(timedelta(hours=-3))


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
    # Id do contato no RD Station CRM (monta o link para a página dele no RD).
    rd_lead_id: str | None
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
    # Id do contato no RD Station CRM (monta o link para a página dele no RD).
    rd_lead_id: str | None
    oportunidades: list[OportunidadeDoLead]
    created_at: datetime
    updated_at: datetime


class LeadResposta(BaseModel):
    lead: LeadDetalhe
    rd: EnvioRd


async def _carregar(session: AsyncSession, user: Usuario, lead_id: UUID) -> Lead:
    stmt = (
        select(Lead)
        # RF07: os próprios leads e os contatos dos negócios que o usuário conduz.
        .where(Lead.id == lead_id, leads_visiveis(user))
        .options(selectinload(Lead.usuario), selectinload(Lead.oportunidades))
        .execution_options(populate_existing=True)
    )
    lead = (await session.execute(stmt)).scalar_one_or_none()
    if lead is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead não encontrado")
    return lead


def _detalhe(lead: Lead, user: Usuario) -> LeadDetalhe:
    return LeadDetalhe(
        id=lead.id,
        nome=lead.nome,
        email=lead.email,
        telefone=lead.telefone,
        responsavel=Pessoa(id=lead.usuario.id, nome=lead.usuario.nome) if lead.usuario else None,
        sincronizado=lead.rd_lead_id is not None,
        rd_lead_id=lead.rd_lead_id,
        oportunidades=[
            OportunidadeDoLead(
                id=op.id,
                nome=op.nome,
                etapa_funil=op.etapa_funil,
                valor=float(op.valor) if op.valor is not None else None,
                status=op.status,
            )
            for op in sorted(lead.oportunidades, key=lambda o: o.updated_at, reverse=True)
            if oportunidade_visivel_no_lead(user, lead, op)
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
    criado_de: Annotated[date | None, Query(description="Cadastrados a partir deste dia (inclusive)")] = None,
    criado_ate: Annotated[date | None, Query(description="Cadastrados até este dia (inclusive)")] = None,
    oportunidades: Annotated[
        Literal["com_aberta", "com_ganha", "sem"] | None,
        Query(description="com_aberta: tem negociação em aberto; com_ganha: já comprou; sem: nenhuma"),
    ] = None,
    rd: Annotated[
        Literal["sincronizado", "local"] | None,
        Query(description="sincronizado: está no RD Station; local: só no CRM Assist"),
    ] = None,
):
    if criado_de and criado_ate and criado_de > criado_ate:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A data inicial é depois da final")
    # Oportunidades do lead que o usuário enxerga (RF07): todas para quem vê o time e para o dono do
    # lead; só as próprias para quem vê o lead por conduzir um negócio dele.
    op = aliased(Oportunidade)
    op_visivel = (
        true() if pode_ver_todos_oportunidades(user) else or_(Lead.usuario_id == user.id, op.usuario_id == user.id)
    )

    def tem_oportunidade(condicao):
        return select(op.id).where(op.lead_id == Lead.id, op_visivel, condicao).exists()

    qtd = select(func.count(op.id)).where(op.lead_id == Lead.id, op_visivel).scalar_subquery()
    stmt = (
        select(Lead, qtd)
        .where(leads_visiveis(user))
        .options(selectinload(Lead.usuario))
        .order_by(Lead.updated_at.desc())
        .limit(LIMITE_LISTA)
    )
    if usuario_id and pode_ver_todos_oportunidades(user):
        stmt = stmt.where(Lead.usuario_id == usuario_id)
    if busca and busca.strip():
        padrao = f"%{busca.strip()}%"
        stmt = stmt.where(Lead.nome.ilike(padrao) | Lead.email.ilike(padrao) | Lead.telefone.ilike(padrao))
    if criado_de:
        stmt = stmt.where(Lead.created_at >= datetime(criado_de.year, criado_de.month, criado_de.day, tzinfo=BRT))
    if criado_ate:
        fim = datetime(criado_ate.year, criado_ate.month, criado_ate.day, tzinfo=BRT) + timedelta(days=1)
        stmt = stmt.where(Lead.created_at < fim)
    status_op = func.lower(func.coalesce(op.status, ""))
    if oportunidades == "com_aberta":
        stmt = stmt.where(tem_oportunidade(~status_op.in_(STATUS_FECHADO)))
    elif oportunidades == "com_ganha":
        stmt = stmt.where(tem_oportunidade(status_op.in_(STATUS_GANHO)))
    elif oportunidades == "sem":
        stmt = stmt.where(~tem_oportunidade(true()))
    if rd == "sincronizado":
        stmt = stmt.where(Lead.rd_lead_id.is_not(None))
    elif rd == "local":
        stmt = stmt.where(Lead.rd_lead_id.is_(None))
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
            rd_lead_id=lead.rd_lead_id,
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
    return _detalhe(await _carregar(session, user, lead_id), user)


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
    return LeadResposta(lead=_detalhe(await _carregar(session, user, lead_id), user), rd=rd)


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
    if lead.usuario_id is not None and body.usuario_id in (None, lead.usuario_id):
        # Sem troca de responsável: o dono continua o mesmo. Assim o closer edita o contato do
        # próprio negócio sem tirar o lead da carteira do SDR.
        responsavel_id = lead.usuario_id
    elif not pode_ver_todos_oportunidades(user) and lead.usuario_id not in (None, user.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Só a gerência pode transferir o lead de outro vendedor")
    else:
        responsavel_id = await _responsavel(session, user, body.usuario_id)
    _aplicar(lead, body, responsavel_id)
    await session.commit()
    rd = await enviar_ao_rd(session, envio.enviar_lead, "lead", lead_id)
    return LeadResposta(lead=_detalhe(await _carregar(session, user, lead_id), user), rd=rd)
