import logging
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rd_station_sync import integracao, normalize
from app.core.rd_station_sync.integracao import RdStationErro
from app.core.rd_station_sync.upsert import log_sync, upsert_lead, upsert_oportunidade
from app.db.models import Interacao, Lead, Oportunidade, Usuario

logger = logging.getLogger(__name__)

FONTE = "webhook_rd"
EVENTOS_NEGOCIACAO = ("crm_deal_created", "crm_deal_updated")
EVENTOS_CONTATO = ("crm_contact_created", "crm_contact_updated")


async def _usuario_por_email(session: AsyncSession, email: str | None) -> UUID | None:
    if not email:
        return None
    stmt = select(Usuario.id).where(func.lower(Usuario.email) == email.lower(), Usuario.ativo.is_(True))
    return (await session.execute(stmt)).scalar_one_or_none()


async def _salvar_contato(
    session: AsyncSession, rd_id: str, doc: dict[str, Any], usuario_padrao_id: UUID | None
) -> Lead:
    campos = normalize.map_contato(doc)
    return await upsert_lead(
        session,
        rd_lead_id=rd_id,
        nome=campos["nome"],
        email=campos["email"],
        telefone=campos["telefone"],
        status=None,
        dados_extras=doc,
        usuario_id=usuario_padrao_id,
    )


async def _vincular_contato(session: AsyncSession, op: Oportunidade, usuario_padrao_id: UUID | None) -> None:
    """O webhook de negociação não traz os contatos: busca o primeiro na API e liga ao lead."""
    try:
        access = await integracao.access_token_em_sessao_propria()
        r = await integracao.chamar_api(access, "GET", f"/deals/{op.rd_deal_id}", ignorar_404=True)
        if r.status_code == 404:
            return
        contatos = integracao.dados_resposta(r).get("contact_ids") or []
        if not contatos:
            return
        contato_id = str(contatos[0])
        lead = (await session.execute(select(Lead).where(Lead.rd_lead_id == contato_id))).scalar_one_or_none()
        if lead is None:
            rc = await integracao.chamar_api(access, "GET", f"/contacts/{contato_id}", ignorar_404=True)
            if rc.status_code == 404:
                return
            lead = await _salvar_contato(session, contato_id, integracao.dados_resposta(rc), usuario_padrao_id)
        op.lead_id = lead.id
        await session.flush()
    except RdStationErro as e:
        # Sem o contato a oportunidade entra mesmo assim; ele chega no próximo evento.
        logger.warning("webhook_rd: contato da negociação %s não obtido: %s", op.rd_deal_id, e)


async def process_rd_webhook_payload(
    session: AsyncSession,
    body: dict[str, Any],
    *,
    usuario_padrao_id: UUID | None = None,
) -> dict[str, Any]:
    """
    Processa um evento do RD Station CRM (negociação ou contato).
    `usuario_padrao_id` (usuário responsável pela integração) vira o dono dos registros
    que chegam sem um responsável conhecido no CRM Assist.
    Não faz commit — quem chama faz commit.
    """
    evento = normalize.nome_evento(body)
    doc = normalize.documento(body)
    rd_id = str(doc["id"]) if doc and doc.get("id") else None

    if rd_id and evento in EVENTOS_NEGOCIACAO:
        campos = normalize.map_negociacao(doc)
        dono_id = await _usuario_por_email(session, campos["dono_email"])
        op = await upsert_oportunidade(
            session,
            rd_deal_id=rd_id,
            nome=campos["nome"],
            etapa_funil=campos["etapa_funil"],
            rd_stage_id=campos["rd_stage_id"],
            valor=campos["valor"],
            status=campos["status"],
            dados_extras=doc,
            usuario_id=dono_id or usuario_padrao_id,
        )
        if dono_id:
            op.usuario_id = dono_id  # responsável trocado no RD
        if op.lead_id is None:
            await _vincular_contato(session, op, usuario_padrao_id)
    elif rd_id and evento == "crm_deal_deleted":
        op_id = (
            await session.execute(select(Oportunidade.id).where(Oportunidade.rd_deal_id == rd_id))
        ).scalar_one_or_none()
        if op_id:
            await session.execute(delete(Interacao).where(Interacao.oportunidade_id == op_id))
            await session.execute(delete(Oportunidade).where(Oportunidade.id == op_id))
    elif rd_id and evento in EVENTOS_CONTATO:
        await _salvar_contato(session, rd_id, doc, usuario_padrao_id)
    else:
        await log_sync(
            session,
            fonte=FONTE,
            event_type=evento,
            rd_entity_id=rd_id,
            status="ignored",
            detalhe={"reason": "evento_nao_tratado", "keys": list(body.keys())[:30]},
        )
        return {"ok": True, "updated": [], "note": "evento não tratado"}

    await log_sync(
        session,
        fonte=FONTE,
        event_type=evento,
        rd_entity_id=rd_id,
        status="success",
        detalhe={"evento": evento},
    )
    return {"ok": True, "updated": [rd_id], "event_type": evento}
