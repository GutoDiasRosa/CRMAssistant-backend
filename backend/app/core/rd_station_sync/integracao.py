"""Configuração da integração com o RD Station e ciclo de vida do token OAuth."""

import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import RdStationConfig, RdStationToken

# Renova o token um pouco antes de expirar, para não falhar no meio de uma chamada.
MARGEM_RENOVACAO = timedelta(minutes=5)


class RdStationErro(Exception):
    """Falha ao falar com o RD Station, com mensagem pronta para a tela."""


@dataclass
class Credenciais:
    client_id: str
    client_secret: str


def novo_webhook_token() -> str:
    return secrets.token_urlsafe(32)


async def obter_config(session: AsyncSession) -> RdStationConfig | None:
    return (await session.execute(select(RdStationConfig).limit(1))).scalar_one_or_none()


async def obter_ou_criar_config(session: AsyncSession) -> RdStationConfig:
    config = await obter_config(session)
    if config is None:
        config = RdStationConfig(ativo=True, webhook_token=novo_webhook_token())
        session.add(config)
        await session.flush()
    elif not config.webhook_token:
        config.webhook_token = novo_webhook_token()
    return config


async def credenciais(session: AsyncSession) -> Credenciais | None:
    """Credenciais cadastradas pela tela; na falta delas, as do .env."""
    config = await obter_config(session)
    if config and config.client_id and config.client_secret:
        return Credenciais(client_id=config.client_id, client_secret=config.client_secret)
    s = get_settings()
    if s.rd_client_id and s.rd_client_secret:
        return Credenciais(client_id=s.rd_client_id, client_secret=s.rd_client_secret)
    return None


async def obter_token(session: AsyncSession) -> RdStationToken | None:
    return (await session.execute(select(RdStationToken).limit(1))).scalar_one_or_none()


def token_expirado(token: RdStationToken) -> bool:
    return token.expires_at is not None and token.expires_at <= datetime.now(UTC) + MARGEM_RENOVACAO


async def _pedir_token(dados: dict[str, str], params: dict[str, str] | None = None) -> dict[str, Any]:
    """POST /auth/token com corpo JSON, como na documentação do RD Station Marketing."""
    s = get_settings()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(s.rd_oauth_token_url, params=params, json=dados)
    except httpx.HTTPError as e:
        raise RdStationErro("Não foi possível falar com o RD Station") from e
    if r.status_code >= 400:
        try:
            tipo = r.json().get("error_type")
        except ValueError:
            tipo = None
        if tipo == "INVALID_REFRESH_TOKEN":
            raise RdStationErro("A autorização foi revogada ou expirou. Autorize o acesso novamente.")
        if tipo == "EXPIRED_CODE_GRANT":
            raise RdStationErro("O código de autorização expirou. Tente autorizar novamente.")
        detalhe = f", {tipo}" if tipo else ""
        raise RdStationErro(f"O RD Station recusou o pedido de token (HTTP {r.status_code}{detalhe})")
    resposta = r.json()
    if not resposta.get("access_token"):
        raise RdStationErro("Resposta do RD Station sem access_token")
    return resposta


async def salvar_token(session: AsyncSession, resposta: dict[str, Any]) -> RdStationToken:
    """Substitui o token guardado pelo recebido do RD Station."""
    expires_at = None
    if resposta.get("expires_in") is not None:
        try:
            expires_at = datetime.now(UTC) + timedelta(seconds=int(resposta["expires_in"]))
        except (TypeError, ValueError):
            pass
    anterior = await obter_token(session)
    await session.execute(delete(RdStationToken))
    token = RdStationToken(
        access_token=resposta["access_token"],
        # O RD pode omitir o refresh_token na renovação: mantém o anterior.
        refresh_token=resposta.get("refresh_token") or (anterior.refresh_token if anterior else None),
        expires_at=expires_at,
    )
    session.add(token)
    await session.flush()
    return token


async def trocar_code_por_token(session: AsyncSession, cred: Credenciais, code: str) -> RdStationToken:
    resposta = await _pedir_token(
        {"client_id": cred.client_id, "client_secret": cred.client_secret, "code": code},
        params={"token_by": "code"},
    )
    return await salvar_token(session, resposta)


async def renovar_token(session: AsyncSession, token: RdStationToken) -> RdStationToken:
    cred = await credenciais(session)
    if cred is None:
        raise RdStationErro("Credenciais do RD Station não cadastradas")
    if not token.refresh_token:
        raise RdStationErro("Token expirado e sem refresh_token. Autorize o acesso novamente.")
    resposta = await _pedir_token(
        {
            "client_id": cred.client_id,
            "client_secret": cred.client_secret,
            "refresh_token": token.refresh_token,
        }
    )
    return await salvar_token(session, resposta)


async def access_token_valido(session: AsyncSession) -> str:
    """Access token pronto para uso, renovando-o se estiver vencido (não faz commit)."""
    token = await obter_token(session)
    if token is None:
        raise RdStationErro("RD Station não autorizado. Clique em Autorizar acesso.")
    if token_expirado(token):
        token = await renovar_token(session, token)
    return token.access_token


async def testar_conexao(session: AsyncSession) -> dict[str, Any]:
    """Chama a API do RD Station com o token atual para confirmar que o acesso funciona."""
    access = await access_token_valido(session)
    url = f"{get_settings().rd_api_url}/marketing/account_info"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, headers={"Authorization": f"Bearer {access}"})
    except httpx.HTTPError as e:
        raise RdStationErro("Não foi possível falar com o RD Station") from e
    if r.status_code == 401:
        raise RdStationErro("O RD Station não aceitou o token. Autorize o acesso novamente.")
    if r.status_code >= 400:
        raise RdStationErro(f"O RD Station respondeu com erro (HTTP {r.status_code})")
    dados = r.json() if r.content else {}
    return {"conta": dados.get("name")}


def webhook_token_confere(config: RdStationConfig | None, recebido: str | None) -> bool:
    if not config or not config.webhook_token or not recebido:
        return False
    return hmac.compare_digest(recebido.strip(), config.webhook_token)
