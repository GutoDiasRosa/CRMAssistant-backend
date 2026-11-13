"""
Normaliza payloads do RD Station.
A API real pode variar — aceitamos chaves comuns e aninhamento genérico (`data`, `payload`).
"""

from typing import Any


def _pick(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def extract_event_type(body: dict[str, Any]) -> str | None:
    """Ex.: "WEBHOOK.CONVERTED" ou "WEBHOOK.MARKED_OPPORTUNITY" (RD Station Marketing)."""
    data = body.get("data")
    return _pick(body, "event_type", "eventType", "type") or (
        _pick(data, "event_type", "eventType", "type") if isinstance(data, dict) else None
    )


def extract_lead_dict(body: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("lead", "Lead"):
        v = body.get(key)
        if isinstance(v, dict):
            return v
    data = body.get("data")
    if isinstance(data, dict):
        for key in ("lead", "Lead"):
            v = data.get(key)
            if isinstance(v, dict):
                return v
    if body.get("uuid") and (_pick(body, "email") or _pick(body, "name", "nome")):
        return body
    return None


def extract_lead_dicts(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Todos os leads/contatos do evento.

    - Webhook atual do RD Station Marketing: `{"event_type": ..., "contact": {...}}`;
    - webhook legado (tela antiga de Integrações): `{"leads": [{...}, ...]}`.
    """
    for origem in (body, body.get("data")):
        if not isinstance(origem, dict):
            continue
        if isinstance(origem.get("contact"), dict):
            return [origem["contact"]]
        if isinstance(origem.get("leads"), list):
            leads = [lead for lead in origem["leads"] if isinstance(lead, dict)]
            if leads:
                return leads
    lead = extract_lead_dict(body)
    return [lead] if lead else []


def extract_deal_dict(body: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("deal", "Deal", "oportunidade", "opportunity"):
        v = body.get(key)
        if isinstance(v, dict):
            return v
    data = body.get("data")
    if isinstance(data, dict):
        for key in ("deal", "Deal", "oportunidade", "opportunity"):
            v = data.get(key)
            if isinstance(v, dict):
                return v
    return None


def lead_rd_id(lead: dict[str, Any]) -> str | None:
    rid = _pick(lead, "uuid", "id", "lead_id", "lead_uuid", "_id")
    return str(rid) if rid is not None else None


def deal_rd_id(deal: dict[str, Any]) -> str | None:
    rid = _pick(deal, "uuid", "id", "deal_id", "opportunity_id", "_id")
    return str(rid) if rid is not None else None


def _texto(valor: Any, limite: int) -> str | None:
    """Converte para texto e corta no tamanho da coluna (evita perder o evento inteiro)."""
    if valor is None:
        return None
    return str(valor).strip()[:limite] or None


def map_lead_fields(lead: dict[str, Any]) -> dict[str, Any]:
    # No payload atual, etapa e dono do contato ficam em `funnel` (com include_relations=CONTACT_FUNNEL).
    funil = lead.get("funnel") if isinstance(lead.get("funnel"), dict) else {}
    return {
        "rd_lead_id": lead_rd_id(lead),
        "nome": str(_pick(lead, "name", "nome", "title") or "")[:512],
        "email": _texto(_pick(lead, "email", "personal_email"), 255),
        "telefone": _texto(_pick(lead, "mobile_phone", "personal_phone", "phone", "telefone"), 64),
        "status": _texto(
            _pick(lead, "status", "lifecycle_stage", "lead_stage") or _pick(funil, "lifecycle_stage"), 128
        ),
        # E-mail do responsável no RD, usado para achar o usuário correspondente no CRM Assist.
        "dono_email": _texto(_pick(funil, "contact_owner_email") or _pick(lead, "contact_owner_email"), 255),
        "dados_extras": lead,
    }


def map_deal_fields(deal: dict[str, Any]) -> dict[str, Any]:
    valor = _pick(deal, "value", "valor", "amount")
    try:
        valor_f = float(valor) if valor is not None else None
    except (TypeError, ValueError):
        valor_f = None
    etapa = _pick(deal, "deal_stage", "stage", "etapa_funil", "pipeline_stage")
    etapa_str = str(etapa)[:255] if etapa is not None else None
    return {
        "rd_deal_id": deal_rd_id(deal),
        "nome": str(_pick(deal, "name", "nome", "title") or "")[:512],
        "etapa_funil": etapa_str or None,
        "valor": valor_f,
        "status": str(_pick(deal, "status") or "")[:128] or None,
        "dados_extras": deal,
    }
