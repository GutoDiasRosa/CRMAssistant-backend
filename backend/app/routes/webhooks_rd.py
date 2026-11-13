import json
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, status

from app.config import get_settings
from app.core.rd_station_sync import integracao
from app.core.rd_station_sync.normalize import extract_event_type
from app.core.rd_station_sync.process import process_rd_webhook_payload
from app.core.rd_station_sync.upsert import log_sync
from app.core.rd_station_sync.webhook_auth import verify_signature_body, verify_webhook_secret
from app.db.database import async_session_factory

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

FONTE = "webhook_rd"


async def _registrar(status_sync: str, event_type: str | None, detalhe: dict[str, Any]) -> None:
    """Grava o resultado numa sessão própria (a do processamento pode ter sido desfeita)."""
    try:
        async with async_session_factory() as session, session.begin():
            await log_sync(
                session,
                fonte=FONTE,
                event_type=event_type,
                rd_entity_id=None,
                status=status_sync,
                detalhe=detalhe,
            )
    except Exception:
        logger.exception("webhook_rd: não foi possível registrar a sincronização")


async def _run_webhook_sync(body: dict, usuario_padrao_id: UUID | None) -> None:
    try:
        async with async_session_factory() as session, session.begin():
            await process_rd_webhook_payload(session, body, usuario_padrao_id=usuario_padrao_id)
    except Exception as e:
        logger.exception("webhook_rd process failed")
        # Sem este registro a falha só apareceria no log do servidor, nunca na tela.
        # Guarda só a primeira linha da causa: a mensagem completa traz SQL e dados do lead.
        causa = getattr(e, "orig", None) or e
        resumo = (str(causa).strip().splitlines() or [type(causa).__name__])[0]
        await _registrar(
            "error",
            extract_event_type(body),
            {"erro": resumo[:300], "keys": list(body.keys())[:30]},
        )


@router.post(
    "/rd-station",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Webhook RD Station — conversão / atualização de leads e oportunidades",
)
async def rd_station_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    token: str | None = Query(None, description="Token do webhook exibido na tela de integração"),
):
    raw = await request.body()
    async with async_session_factory() as session:
        config = await integracao.obter_config(session)

    # Autenticação: token na URL (gerado pela tela) ou, alternativamente, o cabeçalho
    # X-Webhook-Secret com o RD_WEBHOOK_SECRET do servidor. Sem nenhum dos dois
    # configurados, aceita tudo (apenas desenvolvimento).
    secret_env = bool(get_settings().rd_webhook_secret.strip())
    protegido = bool(config and config.webhook_token) or secret_env
    if protegido:
        token_ok = integracao.webhook_token_confere(config, token)
        secret_hdr = request.headers.get("X-Webhook-Secret")
        header_ok = secret_env and bool(secret_hdr) and verify_webhook_secret(secret_hdr)
        if not (token_ok or header_ok):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token do webhook inválido")
    if not verify_signature_body(raw, request.headers.get("X-RD-Signature")):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Assinatura inválida")

    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "JSON inválido") from e
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Corpo deve ser objeto JSON")

    if config and not config.ativo:
        # Responde 202 para o RD Station não ficar reenviando; o evento fica registrado.
        await _registrar("ignored", extract_event_type(body), {"reason": "integracao_desativada"})
        return {"status": "ignored", "message": "Integração com o RD Station desativada"}

    background_tasks.add_task(_run_webhook_sync, body, config.usuario_integracao_id if config else None)
    return {"status": "accepted", "message": "Sincronização agendada em background"}
