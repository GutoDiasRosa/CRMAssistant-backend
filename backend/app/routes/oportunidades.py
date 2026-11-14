"""Oportunidades criadas e editadas no CRM Assist, sincronizadas com o RD Station CRM."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.rd_station_sync import envio, integracao
from app.core.rd_station_sync.integracao import RdStationErro
from app.core.rd_station_sync.upsert import log_sync
from app.db.database import get_session
from app.db.models import Lead, Oportunidade, Usuario
from app.dependencies import get_current_user, pode_ver_todos_oportunidades

router = APIRouter(prefix="/api/crm", tags=["crm"])

StatusOportunidade = Literal["ongoing", "won", "lost", "paused"]


class ContatoBody(BaseModel):
    nome: str = Field(min_length=1, max_length=512)
    email: EmailStr | None = None
    telefone: str | None = Field(default=None, max_length=64)


class OportunidadeBody(BaseModel):
    nome: str = Field(min_length=1, max_length=512)
    valor: float | None = Field(default=None, ge=0)
    etapa: str | None = Field(default=None, max_length=255)
    # Id da etapa no funil do RD Station CRM (vem de GET /api/crm/etapas).
    etapa_rd_id: str | None = Field(default=None, max_length=32)
    status: StatusOportunidade = "ongoing"
    # Sem valor, o responsável é quem cria. Perfis sem visão total só atribuem a si mesmos.
    usuario_id: UUID | None = None
    contato: ContatoBody | None = None


class Pessoa(BaseModel):
    id: UUID
    nome: str


class ContatoResposta(BaseModel):
    id: UUID
    nome: str
    email: str | None
    telefone: str | None


class InteracaoResposta(BaseModel):
    id: UUID
    tipo: str
    descricao: str
    created_at: datetime


class OportunidadeDetalhe(BaseModel):
    id: UUID
    rd_deal_id: str | None
    nome: str
    valor: float | None
    status: str | None
    etapa_funil: str | None
    rd_stage_id: str | None
    responsavel: Pessoa | None
    contato: ContatoResposta | None
    interacoes: list[InteracaoResposta]
    created_at: datetime
    updated_at: datetime


class EnvioRd(BaseModel):
    enviado: bool
    mensagem: str | None = None


class OportunidadeResposta(BaseModel):
    oportunidade: OportunidadeDetalhe
    rd: EnvioRd


async def _carregar(session: AsyncSession, user: Usuario, op_id: UUID) -> Oportunidade:
    stmt = (
        select(Oportunidade)
        .where(Oportunidade.id == op_id)
        .options(
            selectinload(Oportunidade.lead),
            selectinload(Oportunidade.usuario),
            selectinload(Oportunidade.interacoes),
        )
        .execution_options(populate_existing=True)
    )
    op = (await session.execute(stmt)).scalar_one_or_none()
    # RF07: quem não tem visão total só enxerga as próprias oportunidades.
    if op is None or (not pode_ver_todos_oportunidades(user) and op.usuario_id != user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Oportunidade não encontrada")
    return op


def _detalhe(op: Oportunidade) -> OportunidadeDetalhe:
    return OportunidadeDetalhe(
        id=op.id,
        rd_deal_id=op.rd_deal_id,
        nome=op.nome,
        valor=float(op.valor) if op.valor is not None else None,
        status=op.status,
        etapa_funil=op.etapa_funil,
        rd_stage_id=op.rd_stage_id,
        responsavel=Pessoa(id=op.usuario.id, nome=op.usuario.nome) if op.usuario else None,
        contato=(
            ContatoResposta(id=op.lead.id, nome=op.lead.nome, email=op.lead.email, telefone=op.lead.telefone)
            if op.lead
            else None
        ),
        interacoes=[
            InteracaoResposta(id=i.id, tipo=i.tipo, descricao=i.descricao, created_at=i.created_at)
            for i in sorted(op.interacoes, key=lambda i: i.created_at, reverse=True)
        ],
        created_at=op.created_at,
        updated_at=op.updated_at,
    )


async def _responsavel(session: AsyncSession, user: Usuario, usuario_id: UUID | None) -> UUID:
    if usuario_id is None or usuario_id == user.id:
        return user.id
    if not pode_ver_todos_oportunidades(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Você só pode atribuir oportunidades a si mesmo")
    u = await session.get(Usuario, usuario_id)
    if u is None or not u.ativo:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Responsável não encontrado ou inativo")
    return usuario_id


async def _salvar_contato(
    session: AsyncSession, op: Oportunidade, contato: ContatoBody, responsavel_id: UUID
) -> None:
    """Atualiza o contato da oportunidade; sem um, reaproveita o lead de mesmo e-mail ou cria outro."""
    email = str(contato.email) if contato.email else None
    telefone = (contato.telefone or "").strip() or None
    lead = op.lead if op.lead_id else None
    if lead is None and email:
        lead = (
            await session.execute(select(Lead).where(func.lower(Lead.email) == email.lower()).limit(1))
        ).scalar_one_or_none()
    if lead is None:
        lead = Lead(usuario_id=responsavel_id)
        session.add(lead)
    lead.nome = contato.nome.strip()
    lead.email = email
    lead.telefone = telefone
    await session.flush()
    op.lead_id = lead.id


def _aplicar(op: Oportunidade, body: OportunidadeBody, responsavel_id: UUID) -> None:
    op.nome = body.nome.strip()
    op.valor = body.valor
    op.etapa_funil = (body.etapa or "").strip() or None
    op.rd_stage_id = body.etapa_rd_id or None
    op.status = body.status
    op.usuario_id = responsavel_id


async def _enviar_ao_rd(session: AsyncSession, op_id: UUID) -> EnvioRd:
    """Envia a oportunidade já gravada ao RD Station CRM; uma falha não desfaz o que foi salvo."""
    if await integracao.obter_token(session) is None:
        return EnvioRd(enviado=False, mensagem="RD Station não conectado: ficou só no CRM Assist.")
    config = await integracao.obter_config(session)
    if config is not None and not config.ativo:
        return EnvioRd(enviado=False, mensagem="Integração com o RD Station desativada: ficou só no CRM Assist.")
    try:
        await envio.enviar_oportunidade(session, op_id)
        return EnvioRd(enviado=True)
    except RdStationErro as e:
        await session.rollback()
        await log_sync(
            session,
            fonte="envio_rd",
            event_type="oportunidade",
            rd_entity_id=None,
            status="error",
            detalhe={"erro": str(e), "oportunidade_id": str(op_id)},
        )
        await session.commit()
        return EnvioRd(enviado=False, mensagem=f"Salva no CRM Assist, mas não enviada ao RD Station: {e}")


@router.get("/etapas", summary="Funis e etapas para criar oportunidades (do RD Station CRM, se conectado)")
async def etapas(
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[Usuario, Depends(get_current_user)],
):
    config = await integracao.obter_config(session)
    aviso = None
    if config and config.ativo and await integracao.obter_token(session) is not None:
        try:
            access = await integracao.access_token_valido(session)
            await session.commit()
            return {"origem": "rd", "funis": await envio.listar_funis(access), "aviso": None}
        except RdStationErro as e:
            await session.rollback()
            aviso = f"Não foi possível ler o funil do RD Station: {e}"
    # Sem RD: as etapas que já existem no CRM Assist servem de sugestão.
    nomes = (
        await session.execute(
            select(Oportunidade.etapa_funil)
            .where(Oportunidade.etapa_funil.is_not(None))
            .distinct()
            .order_by(Oportunidade.etapa_funil)
        )
    ).scalars().all()
    return {
        "origem": "local",
        "funis": [{"id": None, "nome": "CRM Assist", "etapas": [{"id": None, "nome": n} for n in nomes]}],
        "aviso": aviso,
    }


@router.get("/oportunidades/{op_id}", response_model=OportunidadeDetalhe, summary="Detalhe de uma oportunidade (RF07)")
async def obter(
    op_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[Usuario, Depends(get_current_user)],
):
    return _detalhe(await _carregar(session, user, op_id))


@router.post(
    "/oportunidades",
    response_model=OportunidadeResposta,
    status_code=status.HTTP_201_CREATED,
    summary="Cria uma oportunidade e a envia ao RD Station CRM",
)
async def criar(
    body: OportunidadeBody,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[Usuario, Depends(get_current_user)],
):
    responsavel_id = await _responsavel(session, user, body.usuario_id)
    op = Oportunidade()
    _aplicar(op, body, responsavel_id)
    session.add(op)
    await session.flush()
    if body.contato:
        await _salvar_contato(session, op, body.contato, responsavel_id)
    await session.commit()
    op_id = op.id  # uma falha no envio faz rollback e expira o objeto
    rd = await _enviar_ao_rd(session, op_id)
    return OportunidadeResposta(oportunidade=_detalhe(await _carregar(session, user, op_id)), rd=rd)


@router.put(
    "/oportunidades/{op_id}",
    response_model=OportunidadeResposta,
    summary="Atualiza uma oportunidade e envia a alteração ao RD Station CRM",
)
async def atualizar(
    op_id: UUID,
    body: OportunidadeBody,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[Usuario, Depends(get_current_user)],
):
    op = await _carregar(session, user, op_id)
    responsavel_id = await _responsavel(session, user, body.usuario_id or op.usuario_id)
    _aplicar(op, body, responsavel_id)
    if body.contato:
        await _salvar_contato(session, op, body.contato, responsavel_id)
    await session.commit()
    rd = await _enviar_ao_rd(session, op_id)
    return OportunidadeResposta(oportunidade=_detalhe(await _carregar(session, user, op_id)), rd=rd)
