"""Envio do CRM Assist para o RD Station CRM: contatos, negociações e consulta do funil.

Os ids do RD ficam gravados nos registros locais (rd_lead_id / rd_deal_id). Quando o RD
devolve o mesmo dado pelo webhook, o upsert pelo id não gera duplicata.
"""

import time
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.rd_station_sync import integracao
from app.core.rd_station_sync.integracao import RdStationErro, chamar_api, dados_resposta, listar
from app.db.models import Interacao, Lead, Oportunidade

# Status de negociação aceitos pelo RD Station CRM.
STATUS_RD = ("ongoing", "won", "lost", "paused")

# Funil e usuários do RD mudam pouco: guarda por alguns minutos para não consultar a cada tela.
_CACHE_SEGUNDOS = 300
_cache: dict[str, tuple[float, Any]] = {}


async def _em_cache(chave: str, carregar):
    agora = time.monotonic()
    guardado = _cache.get(chave)
    if guardado and agora - guardado[0] < _CACHE_SEGUNDOS:
        return guardado[1]
    valor = await carregar()
    _cache[chave] = (agora, valor)
    return valor


def limpar_cache() -> None:
    _cache.clear()


async def listar_funis(access: str) -> list[dict[str, Any]]:
    """Funis de vendas do RD com as etapas em ordem: [{id, nome, etapas: [{id, nome}]}]."""

    async def carregar():
        funis = []
        for funil in await listar(access, "/pipelines"):
            etapas = await listar(access, f"/pipelines/{funil['id']}/stages")
            etapas.sort(key=lambda e: e.get("order") or 0)
            funis.append(
                {
                    "id": funil["id"],
                    "nome": funil.get("name") or "Funil",
                    "etapas": [{"id": e["id"], "nome": e.get("name") or ""} for e in etapas if e.get("id")],
                }
            )
        return funis

    return await _em_cache("funis", carregar)


async def _id_usuario_rd(access: str, email: str | None) -> str | None:
    """Usuário do RD com o mesmo e-mail do responsável no CRM Assist."""
    if not email:
        return None

    async def carregar():
        return {
            str(u["email"]).lower(): u["id"] for u in await listar(access, "/users") if u.get("email") and u.get("id")
        }

    return (await _em_cache("usuarios", carregar)).get(email.lower())


async def enviar_contato(
    access: str, *, rd_id: str | None, nome: str, email: str | None, telefone: str | None
) -> str:
    """Cria ou atualiza o contato no RD Station CRM e devolve o id dele."""
    dados: dict[str, Any] = {"name": nome}
    if email:
        dados["emails"] = [{"email": email}]
    if telefone:
        dados["phones"] = [{"phone": telefone, "type": "mobile"}]
    if rd_id:
        r = await chamar_api(access, "PUT", f"/contacts/{rd_id}", json={"data": dados}, ignorar_404=True)
        if r.status_code != 404:
            return rd_id
    r = await chamar_api(access, "POST", "/contacts", json={"data": dados})
    novo_id = dados_resposta(r).get("id")
    if not novo_id:
        raise RdStationErro("O RD Station não devolveu o id do contato criado")
    return str(novo_id)


async def vincular_lead(session: AsyncSession, lead: Lead, rd_id: str) -> None:
    """Grava o id do RD no lead. Se o webhook já criou outro lead com esse id, une os dois."""
    if lead.rd_lead_id == rd_id:
        return
    duplicado = (
        await session.execute(select(Lead).where(Lead.rd_lead_id == rd_id, Lead.id != lead.id))
    ).scalar_one_or_none()
    if duplicado:
        await session.execute(
            update(Oportunidade).where(Oportunidade.lead_id == duplicado.id).values(lead_id=lead.id)
        )
        await session.execute(update(Interacao).where(Interacao.lead_id == duplicado.id).values(lead_id=lead.id))
        await session.delete(duplicado)
        await session.flush()
    lead.rd_lead_id = rd_id
    await session.flush()


async def _vincular_oportunidade(session: AsyncSession, op: Oportunidade, rd_id: str) -> None:
    """Grava o id do RD na oportunidade. Se o webhook já criou outra com esse id, fica a local."""
    if op.rd_deal_id == rd_id:
        return
    duplicada = (
        await session.execute(select(Oportunidade).where(Oportunidade.rd_deal_id == rd_id, Oportunidade.id != op.id))
    ).scalar_one_or_none()
    if duplicada:
        await session.execute(
            update(Interacao).where(Interacao.oportunidade_id == duplicada.id).values(oportunidade_id=op.id)
        )
        await session.delete(duplicada)
        await session.flush()
    op.rd_deal_id = rd_id
    await session.flush()


async def _carregar_oportunidade(session: AsyncSession, op_id: UUID) -> Oportunidade:
    stmt = (
        select(Oportunidade)
        .where(Oportunidade.id == op_id)
        .options(selectinload(Oportunidade.lead), selectinload(Oportunidade.usuario))
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one()


async def enviar_oportunidade(session: AsyncSession, op_id: UUID) -> None:
    """Cria ou atualiza no RD Station CRM a negociação da oportunidade (e o contato dela).

    Faz commit. Levanta RdStationErro se o RD recusar.
    """
    access = await integracao.access_token_valido(session)
    await session.commit()  # guarda o token se ele foi renovado

    op = await _carregar_oportunidade(session, op_id)
    contato_id = None
    if op.lead:
        contato_id = await enviar_contato(
            access, rd_id=op.lead.rd_lead_id, nome=op.lead.nome or op.nome, email=op.lead.email, telefone=op.lead.telefone
        )

    dados: dict[str, Any] = {"name": op.nome}
    if op.valor is not None:
        dados["one_time_price"] = float(op.valor)
    if op.rd_stage_id:
        dados["stage_id"] = op.rd_stage_id
    dono = await _id_usuario_rd(access, op.usuario.email if op.usuario else None)
    if dono:
        dados["owner_id"] = dono
    if contato_id:
        dados["contact_ids"] = [contato_id]
    status = op.status if op.status in STATUS_RD else None

    deal_id = None
    if op.rd_deal_id:
        corpo = {**dados, "status": status} if status else dados
        r = await chamar_api(access, "PUT", f"/deals/{op.rd_deal_id}", json={"data": corpo}, ignorar_404=True)
        if r.status_code != 404:
            deal_id = op.rd_deal_id
    if deal_id is None:
        # O RD só aceita criar negociações em andamento; o status final vai numa atualização.
        r = await chamar_api(access, "POST", "/deals", json={"data": {**dados, "status": "ongoing"}})
        deal_id = dados_resposta(r).get("id")
        if not deal_id:
            raise RdStationErro("O RD Station não devolveu o id da negociação criada")
        deal_id = str(deal_id)
        if status and status != "ongoing":
            await chamar_api(access, "PUT", f"/deals/{deal_id}", json={"data": {"status": status}})

    # O webhook do próprio RD pode chegar antes deste commit e criar o mesmo registro:
    # nesse caso a união acontece aqui, e um conflito simultâneo é resolvido na segunda tentativa.
    for tentativa in (1, 2):
        try:
            op = await _carregar_oportunidade(session, op_id)
            if contato_id and op.lead:
                await vincular_lead(session, op.lead, contato_id)
            await _vincular_oportunidade(session, op, deal_id)
            await session.commit()
            return
        except IntegrityError:
            await session.rollback()
            if tentativa == 2:
                raise


async def enviar_lead(session: AsyncSession, lead_id: UUID) -> None:
    """Cria ou atualiza no RD Station CRM o contato correspondente ao lead. Faz commit."""
    access = await integracao.access_token_valido(session)
    await session.commit()
    lead = await session.get(Lead, lead_id, populate_existing=True)
    contato_id = await enviar_contato(
        access, rd_id=lead.rd_lead_id, nome=lead.nome, email=lead.email, telefone=lead.telefone
    )
    for tentativa in (1, 2):
        try:
            lead = await session.get(Lead, lead_id, populate_existing=True)
            await vincular_lead(session, lead, contato_id)
            await session.commit()
            return
        except IntegrityError:
            await session.rollback()
            if tentativa == 2:
                raise


async def enviar_contato_teste(session: AsyncSession) -> dict[str, Any]:
    """Cria (ou atualiza) o contato de teste no RD Station CRM e o grava como lead local.

    Testa o envio; a volta pelo webhook (crm_contact_created/updated) atualiza o
    "Último dado recebido". Faz commit.
    """
    access = await integracao.access_token_valido(session)
    await session.commit()
    lead = (
        await session.execute(select(Lead).where(Lead.email == integracao.LEAD_TESTE_EMAIL).limit(1))
    ).scalar_one_or_none()
    rd_id_anterior = lead.rd_lead_id if lead else None
    contato_id = await enviar_contato(
        access, rd_id=rd_id_anterior, nome=integracao.LEAD_TESTE_NOME, email=integracao.LEAD_TESTE_EMAIL, telefone=None
    )
    for tentativa in (1, 2):
        try:
            # Se o webhook já trouxe o contato, o lead local existe com esse id.
            vinculado = (
                await session.execute(select(Lead).where(Lead.rd_lead_id == contato_id))
            ).scalar_one_or_none()
            if vinculado is None:
                lead = (
                    await session.execute(select(Lead).where(Lead.email == integracao.LEAD_TESTE_EMAIL).limit(1))
                ).scalar_one_or_none()
                if lead is None:
                    lead = Lead(nome=integracao.LEAD_TESTE_NOME, email=integracao.LEAD_TESTE_EMAIL)
                    session.add(lead)
                    await session.flush()
                await vincular_lead(session, lead, contato_id)
            await session.commit()
            break
        except IntegrityError:
            await session.rollback()
            if tentativa == 2:
                raise
    return {"email": integracao.LEAD_TESTE_EMAIL, "contato_id": contato_id, "criado": contato_id != rd_id_anterior}
