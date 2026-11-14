from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Lead, Oportunidade, Sincronizacao


def _juntar(atuais: dict[str, Any] | None, novos: dict[str, Any] | None) -> dict[str, Any] | None:
    """Atualiza os dados extras sem apagar os gravados aqui (ex.: empresa, produto, motivo da perda,
    marca da carga de demonstração): as chaves que vêm do RD sobrescrevem só as de mesmo nome."""
    if novos is None:
        return atuais
    return {**(atuais or {}), **novos}


async def upsert_lead(
    session: AsyncSession,
    *,
    rd_lead_id: str,
    nome: str,
    email: str | None,
    telefone: str | None,
    status: str | None,
    dados_extras: dict[str, Any] | None,
    usuario_id: UUID | None = None,
) -> Lead:
    stmt = select(Lead).where(Lead.rd_lead_id == rd_lead_id)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row:
        row.nome = nome or row.nome
        row.email = email if email is not None else row.email
        row.telefone = telefone if telefone is not None else row.telefone
        row.status = status if status is not None else row.status
        row.dados_extras = _juntar(row.dados_extras, dados_extras)
        if usuario_id and row.usuario_id is None:
            row.usuario_id = usuario_id
        await session.flush()
        return row
    lead = Lead(
        id=uuid4(),
        rd_lead_id=rd_lead_id,
        usuario_id=usuario_id,
        nome=nome,
        email=email,
        telefone=telefone,
        status=status,
        dados_extras=dados_extras,
    )
    session.add(lead)
    await session.flush()
    return lead


async def upsert_oportunidade(
    session: AsyncSession,
    *,
    rd_deal_id: str,
    nome: str,
    etapa_funil: str | None,
    valor: float | None,
    status: str | None,
    dados_extras: dict[str, Any] | None,
    lead_id: UUID | None = None,
    usuario_id: UUID | None = None,
    rd_stage_id: str | None = None,
) -> Oportunidade:
    stmt = select(Oportunidade).where(Oportunidade.rd_deal_id == rd_deal_id)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row:
        row.nome = nome or row.nome
        row.etapa_funil = etapa_funil if etapa_funil is not None else row.etapa_funil
        row.rd_stage_id = rd_stage_id if rd_stage_id is not None else row.rd_stage_id
        row.valor = valor if valor is not None else row.valor
        row.status = status if status is not None else row.status
        row.dados_extras = _juntar(row.dados_extras, dados_extras)
        if lead_id and row.lead_id is None:
            row.lead_id = lead_id
        if usuario_id and row.usuario_id is None:
            row.usuario_id = usuario_id
        await session.flush()
        return row
    op = Oportunidade(
        id=uuid4(),
        rd_deal_id=rd_deal_id,
        lead_id=lead_id,
        usuario_id=usuario_id,
        nome=nome,
        etapa_funil=etapa_funil,
        rd_stage_id=rd_stage_id,
        valor=valor,
        status=status,
        dados_extras=dados_extras,
    )
    session.add(op)
    await session.flush()
    return op


async def log_sync(
    session: AsyncSession,
    *,
    fonte: str,
    event_type: str | None,
    rd_entity_id: str | None,
    status: str,
    detalhe: dict[str, Any] | None = None,
) -> Sincronizacao:
    log = Sincronizacao(
        id=uuid4(),
        fonte=fonte,
        event_type=event_type,
        rd_entity_id=rd_entity_id,
        status=status,
        detalhe=detalhe,
    )
    session.add(log)
    await session.flush()
    return log
