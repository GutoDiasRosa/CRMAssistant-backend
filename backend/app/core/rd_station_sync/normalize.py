"""Normaliza os webhooks do RD Station CRM.

Envelope: {"event_name": "crm_deal_updated", "event_timestamp": ..., "transaction_uuid": ...,
"document": {...}}. Negociações chegam no formato DealV1 (deal_stage, user, amount_total);
contatos, com emails/phones em listas.
"""

from typing import Any


def nome_evento(body: dict[str, Any]) -> str | None:
    """Ex.: "crm_deal_created", "crm_contact_updated"."""
    valor = body.get("event_name") or body.get("event_type")
    return str(valor) if valor else None


def documento(body: dict[str, Any]) -> dict[str, Any] | None:
    doc = body.get("document")
    # Alguns eventos embrulham o registro em document.data.
    if isinstance(doc, dict) and "id" not in doc and isinstance(doc.get("data"), dict):
        doc = doc["data"]
    return doc if isinstance(doc, dict) else None


def _texto(valor: Any, limite: int) -> str | None:
    """Converte para texto e corta no tamanho da coluna (evita perder o evento inteiro)."""
    if valor is None:
        return None
    return str(valor).strip()[:limite] or None


def _numero(*valores: Any) -> float | None:
    for valor in valores:
        if valor is None:
            continue
        try:
            return float(valor)
        except (TypeError, ValueError):
            continue
    return None


def _primeiro(lista: Any, chave: str) -> Any:
    if isinstance(lista, list):
        for item in lista:
            if isinstance(item, dict) and item.get(chave):
                return item[chave]
    return None


def map_negociacao(doc: dict[str, Any]) -> dict[str, Any]:
    etapa = doc.get("deal_stage") if isinstance(doc.get("deal_stage"), dict) else {}
    dono = doc.get("user") if isinstance(doc.get("user"), dict) else {}
    return {
        "nome": str(doc.get("name") or "")[:512],
        "etapa_funil": _texto(etapa.get("name"), 255),
        "rd_stage_id": _texto(etapa.get("id") or doc.get("stage_id"), 32),
        "valor": _numero(doc.get("amount_total"), doc.get("amount_unique"), doc.get("one_time_price")),
        "status": _texto(doc.get("status"), 128),
        # E-mail do responsável no RD, usado para achar o usuário correspondente no CRM Assist.
        "dono_email": _texto(dono.get("email"), 255),
    }


def map_contato(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "nome": str(doc.get("name") or "")[:512],
        "email": _texto(_primeiro(doc.get("emails"), "email"), 255),
        "telefone": _texto(_primeiro(doc.get("phones"), "phone"), 64),
    }
