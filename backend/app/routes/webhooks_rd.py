import json
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, status

from app.core.rd_station_sync import integracao
from app.core.rd_station_sync.normalize import nome_evento
from app.core.rd_station_sync.process import process_rd_webhook_payload
from app.core.rd_station_sync.upsert import log_sync
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
            nome_evento(body),
            {"erro": resumo[:300], "keys": list(body.keys())[:30]},
        )


@router.post(
    "/rd-station",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Webhook do RD Station CRM — negociações e contatos",
)
async def rd_station_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    token: str | None = Query(None, description="Token do webhook, cadastrado pelo CRM Assist no RD Station"),
):
    raw = await request.body()
    async with async_session_factory() as session:
        config = await integracao.obter_config(session)

    # Autenticação: o token vai na URL que o próprio CRM Assist cadastrou no RD Station.
    if config is None or not integracao.webhook_token_confere(config, token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token do webhook inválido")

    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "JSON inválido") from e
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Corpo deve ser objeto JSON")

    if not config.ativo:
        # Responde 202 para o RD Station não ficar reenviando; o evento fica registrado.
        await _registrar("ignored", nome_evento(body), {"reason": "integracao_desativada"})
        return {"status": "ignored", "message": "Integração com o RD Station desativada"}

    background_tasks.add_task(_run_webhook_sync, body, config.usuario_integracao_id)
    return {"status": "accepted", "message": "Sincronização agendada em background"}
