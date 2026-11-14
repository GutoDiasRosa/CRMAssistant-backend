"""Integração com o RD Station CRM (API v2): configuração, token OAuth, webhooks e chamadas à API."""

import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import async_session_factory
from app.db.models import RdStationConfig, RdStationToken

# Endereços do RD Station CRM (https://developers.rdstation.com/reference/crm-v2-introduction).
RD_AUTHORIZE_URL = "https://accounts.rdstation.com/oauth/authorize"
RD_TOKEN_URL = "https://api.rd.services/oauth2/token"
RD_CRM_URL = "https://api.rd.services/crm/v2"

# Eventos do RD Station CRM que o CRM Assist assina ao concluir a configuração.
EVENTOS_WEBHOOK = (
    "crm_deal_created",
    "crm_deal_updated",
    "crm_deal_deleted",
    "crm_contact_created",
    "crm_contact_updated",
)

# Contato usado pelo botão "Testar conexão". E-mail fixo (domínio reservado para exemplos):
# testes repetidos atualizam o mesmo contato em vez de encher a base.
LEAD_TESTE_EMAIL = "teste.crmassist@example.com"
LEAD_TESTE_NOME = "Lead de teste do CRM Assist"

# O token do RD Station CRM dura 2 horas: renova um pouco antes de expirar.
MARGEM_RENOVACAO = timedelta(minutes=5)

HOSTS_LOCAIS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


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
    """Client ID e Client Secret cadastrados pela tela de integração."""
    config = await obter_config(session)
    if config and config.client_id and config.client_secret:
        return Credenciais(client_id=config.client_id, client_secret=config.client_secret)
    return None


def url_autorizacao(cred: Credenciais, redirect_uri: str, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": cred.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"{RD_AUTHORIZE_URL}?{urlencode(params)}"


# ---- Token OAuth ----


async def obter_token(session: AsyncSession) -> RdStationToken | None:
    return (await session.execute(select(RdStationToken).limit(1))).scalar_one_or_none()


def token_expirado(token: RdStationToken) -> bool:
    return token.expires_at is not None and token.expires_at <= datetime.now(UTC) + MARGEM_RENOVACAO


async def _pedir_token(dados: dict[str, str]) -> dict[str, Any]:
    """POST /oauth2/token com corpo form-urlencoded, como na documentação do RD Station CRM."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(RD_TOKEN_URL, data=dados, headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        raise RdStationErro("Não foi possível falar com o RD Station") from e
    if r.status_code >= 400:
        try:
            corpo = r.json()
            erro = corpo.get("error") or corpo.get("error_type")
        except (ValueError, AttributeError):
            erro = None
        if erro == "invalid_client":
            raise RdStationErro("O RD Station não reconheceu o Client ID e o Client Secret. Confira as credenciais.")
        if erro in ("invalid_grant", "INVALID_REFRESH_TOKEN", "EXPIRED_CODE_GRANT"):
            if dados.get("grant_type") == "refresh_token":
                raise RdStationErro("A autorização foi revogada ou expirou. Autorize o acesso novamente.")
            raise RdStationErro("O código de autorização expirou ou é inválido. Tente autorizar novamente.")
        detalhe = f", {erro}" if erro else ""
        raise RdStationErro(f"O RD Station recusou o pedido de token (HTTP {r.status_code}{detalhe})")
    resposta = r.json()
    if not resposta.get("access_token"):
        raise RdStationErro("Resposta do RD Station sem access_token")
    return resposta


async def salvar_token(
    session: AsyncSession, resposta: dict[str, Any], *, nova_autorizacao: bool = False
) -> RdStationToken:
    """Grava o token recebido do RD Station, atualizando a linha existente."""
    expires_at = None
    if resposta.get("expires_in") is not None:
        try:
            expires_at = datetime.now(UTC) + timedelta(seconds=int(resposta["expires_in"]))
        except (TypeError, ValueError):
            pass
    token = await obter_token(session)
    if token is None:
        token = RdStationToken(
            access_token=resposta["access_token"],
            refresh_token=resposta.get("refresh_token"),
            expires_at=expires_at,
        )
        session.add(token)
    else:
        token.access_token = resposta["access_token"]
        # Cada renovação traz um refresh_token novo e o anterior deixa de valer.
        token.refresh_token = resposta.get("refresh_token") or token.refresh_token
        token.expires_at = expires_at
        if nova_autorizacao:
            token.created_at = datetime.now(UTC)
    await session.flush()
    return token


async def trocar_code_por_token(
    session: AsyncSession, cred: Credenciais, code: str, redirect_uri: str
) -> RdStationToken:
    resposta = await _pedir_token(
        {
            "grant_type": "authorization_code",
            "client_id": cred.client_id,
            "client_secret": cred.client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        }
    )
    return await salvar_token(session, resposta, nova_autorizacao=True)


async def renovar_token(session: AsyncSession, token: RdStationToken) -> RdStationToken:
    cred = await credenciais(session)
    if cred is None:
        raise RdStationErro("Credenciais do RD Station não cadastradas")
    if not token.refresh_token:
        raise RdStationErro("Token expirado e sem refresh_token. Autorize o acesso novamente.")
    resposta = await _pedir_token(
        {
            "grant_type": "refresh_token",
            "client_id": cred.client_id,
            "client_secret": cred.client_secret,
            "refresh_token": token.refresh_token,
        }
    )
    return await salvar_token(session, resposta)


async def access_token_valido(session: AsyncSession) -> str:
    """Access token pronto para uso, renovando-o se estiver vencido.

    Não faz commit: quem chama deve fazer commit logo em seguida, porque o refresh_token
    antigo deixa de valer assim que o RD devolve o novo.
    """
    token = await obter_token(session)
    if token is None:
        raise RdStationErro("RD Station não autorizado. Clique em Autorizar acesso.")
    if token_expirado(token):
        # Trava a linha: duas renovações simultâneas usariam o mesmo refresh_token
        # e a segunda seria recusada. Quem esperar a trava relê o token já renovado.
        token = (
            await session.execute(
                select(RdStationToken)
                .where(RdStationToken.id == token.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if token is None:
            raise RdStationErro("RD Station não autorizado. Clique em Autorizar acesso.")
        if token_expirado(token):
            token = await renovar_token(session, token)
    return token.access_token


async def access_token_em_sessao_propria() -> str:
    """Como access_token_valido, mas renovando numa transação própria, já gravada.

    Para tarefas em segundo plano: um rollback no processamento não pode descartar o
    refresh_token novo.
    """
    async with async_session_factory() as session, session.begin():
        return await access_token_valido(session)


# ---- Chamadas à API do CRM ----


def _mensagem_erro_rd(r: httpx.Response) -> str | None:
    """Primeira mensagem de erro do corpo da resposta, nos formatos usados pelo RD."""
    try:
        corpo = r.json()
    except ValueError:
        return None
    if not isinstance(corpo, dict):
        return None
    erros = corpo.get("errors", corpo)
    if isinstance(erros, list):
        erros = erros[0] if erros else {}
    if not isinstance(erros, dict):
        return None
    texto = erros.get("detail") or erros.get("error_message") or erros.get("message") or erros.get("title")
    return str(texto)[:200] if texto else None


async def chamar_api(
    access: str,
    metodo: str,
    caminho: str,
    *,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    ignorar_404: bool = False,
) -> httpx.Response:
    """Chama a API v2 do RD Station CRM (`caminho` relativo a /crm/v2)."""
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.request(
                metodo,
                f"{RD_CRM_URL}{caminho}",
                json=json,
                params=params,
                headers={"Authorization": f"Bearer {access}", "Accept": "application/json"},
            )
    except httpx.HTTPError as e:
        raise RdStationErro("Não foi possível falar com o RD Station") from e
    if r.status_code == 404 and ignorar_404:
        return r
    if r.status_code == 401:
        raise RdStationErro("O RD Station não aceitou o token. Autorize o acesso novamente.")
    if r.status_code == 403:
        raise RdStationErro(
            "O RD Station negou o acesso (HTTP 403). A API exige o plano Basic, Pro ou Advanced do RD Station CRM."
        )
    if r.status_code == 429:
        raise RdStationErro("O RD Station limitou as requisições por excesso de chamadas. Tente de novo em instantes.")
    if r.status_code >= 400:
        detalhe = _mensagem_erro_rd(r)
        raise RdStationErro(
            f"O RD Station respondeu com erro (HTTP {r.status_code}{f': {detalhe}' if detalhe else ''})"
        )
    return r


def dados_resposta(r: httpx.Response) -> dict[str, Any]:
    """Conteúdo de `data` de uma resposta da API v2 (as respostas vêm embrulhadas em `data`)."""
    corpo = r.json() if r.content else {}
    dados = corpo.get("data") if isinstance(corpo, dict) else None
    return dados if isinstance(dados, dict) else {}


async def listar(access: str, caminho: str, *, max_paginas: int = 20) -> list[dict[str, Any]]:
    """Todos os itens de uma listagem paginada (`page[number]`, `links.next`)."""
    itens: list[dict[str, Any]] = []
    for pagina in range(1, max_paginas + 1):
        r = await chamar_api(access, "GET", caminho, params={"page[number]": pagina})
        corpo = r.json() if r.content else {}
        dados = corpo.get("data") if isinstance(corpo, dict) else None
        if not isinstance(dados, list) or not dados:
            break
        itens.extend(d for d in dados if isinstance(d, dict))
        if not (corpo.get("links") or {}).get("next"):
            break
    return itens


# ---- Webhooks ----


def url_webhook(base_webhook: str, config: RdStationConfig) -> str:
    return f"{base_webhook}?{urlencode({'token': config.webhook_token})}"


def endereco_local(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host in HOSTS_LOCAIS or host.endswith(".localhost")


async def remover_webhooks(access: str, config: RdStationConfig, base_webhook: str | None) -> None:
    """Apaga no RD os webhooks do CRM Assist: os guardados e os que apontam para esta API.

    A busca pelo endereço limpa também webhooks órfãos de tentativas interrompidas.
    """
    ids = {w["uuid"] for w in (config.webhooks or []) if w.get("uuid")}
    if base_webhook:
        for w in await listar(access, "/webhooks"):
            if str(w.get("url", "")).startswith(base_webhook) and w.get("id"):
                ids.add(w["id"])
    for webhook_id in ids:
        await chamar_api(access, "DELETE", f"/webhooks/{webhook_id}", ignorar_404=True)
    config.webhooks = []


async def criar_webhooks(access: str, config: RdStationConfig, base_webhook: str) -> None:
    """Cria no RD Station CRM os webhooks que enviam negociações e contatos ao CRM Assist.

    Antes, remove os criados anteriormente, então pode ser chamada de novo sem duplicar.
    Não faz commit.
    """
    url = url_webhook(base_webhook, config)
    if endereco_local(url):
        raise RdStationErro(
            "O RD Station só envia webhooks para endereços públicos e a API está rodando em um endereço "
            "local. Publique a API (ou defina API_PUBLIC_URL) e clique em Criar webhook."
        )
    await remover_webhooks(access, config, base_webhook)
    criados: list[dict[str, Any]] = []
    for evento in EVENTOS_WEBHOOK:
        r = await chamar_api(
            access,
            "POST",
            "/webhooks",
            json={"data": {"event_name": evento, "url": url, "http_method": "POST"}},
        )
        criados.append({"uuid": dados_resposta(r).get("id"), "event_type": evento})
    config.webhooks = criados
    config.webhook_erro = None


def webhook_token_confere(config: RdStationConfig | None, recebido: str | None) -> bool:
    if not config or not config.webhook_token or not recebido:
        return False
    return hmac.compare_digest(recebido.strip(), config.webhook_token)
