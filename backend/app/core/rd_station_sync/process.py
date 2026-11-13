from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rd_station_sync import normalize
from app.core.rd_station_sync.upsert import log_sync, upsert_lead, upsert_oportunidade
from app.db.models import Usuario


async def _usuario_por_email(session: AsyncSession, email: str | None) -> UUID | None:
    if not email:
        return None
    stmt = select(Usuario.id).where(func.lower(Usuario.email) == email.lower(), Usuario.ativo.is_(True))
    return (await session.execute(stmt)).scalar_one_or_none()


async def process_rd_webhook_payload(
    session: AsyncSession,
    body: dict[str, Any],
    *,
    usuario_padrao_id: UUID | None = None,
) -> dict[str, Any]:
    """
    Processa um evento de conversão/atualização vindo do RD Station.
    `usuario_padrao_id` (usuário responsável pela integração) vira o dono dos registros
    que ainda não têm responsável.
    Retorna resumo para resposta HTTP (não faz commit — quem chama faz commit).
    """
    event_type = normalize.extract_event_type(body)
    deal_raw = normalize.extract_deal_dict(body)
    updated: list[str] = []

    lead_id_db = None
    for lead_raw in normalize.extract_lead_dicts(body):
        fields = normalize.map_lead_fields(lead_raw)
        rid = fields.get("rd_lead_id")
        if rid:
            # Dono no RD (por e-mail) tem prioridade; senão, o usuário responsável da integração.
            dono_id = await _usuario_por_email(session, fields.get("dono_email"))
            row = await upsert_lead(
                session,
                rd_lead_id=rid,
                nome=fields["nome"],
                email=fields.get("email"),
                telefone=fields.get("telefone"),
                status=fields.get("status"),
                dados_extras=fields.get("dados_extras"),
                usuario_id=dono_id or usuario_padrao_id,
            )
            lead_id_db = lead_id_db or row.id
            updated.append(f"lead:{rid}")

    if deal_raw:
        fields = normalize.map_deal_fields(deal_raw)
        rid = fields.get("rd_deal_id")
        if rid:
            await upsert_oportunidade(
                session,
                rd_deal_id=rid,
                nome=fields["nome"],
                etapa_funil=fields.get("etapa_funil"),
                valor=fields.get("valor"),
                status=fields.get("status"),
                dados_extras=fields.get("dados_extras"),
                lead_id=lead_id_db,
                usuario_id=usuario_padrao_id,
            )
            updated.append(f"deal:{rid}")

    if not updated:
        await log_sync(
            session,
            fonte="webhook_rd",
            event_type=event_type,
            rd_entity_id=None,
            status="ignored",
            detalhe={"reason": "no_lead_or_deal_in_payload", "keys": list(body.keys())[:30]},
        )
        return {"ok": True, "updated": [], "note": "payload sem lead/deal reconhecível"}

    primary = updated[0].split(":", 1)[-1]
    await log_sync(
        session,
        fonte="webhook_rd",
        event_type=event_type,
        rd_entity_id=primary,
        status="success",
        detalhe={"updated": updated},
    )
    return {"ok": True, "updated": updated, "event_type": event_type}
