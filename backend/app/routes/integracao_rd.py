import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.rd_station_sync import envio, integracao
from app.core.rd_station_sync.integracao import RdStationErro
from app.db.database import get_session
from app.db.models import RdStationConfig, RdStationToken, Sincronizacao, Usuario
from app.dependencies import get_current_admin, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/oauth/rd", tags=["rd-station"])

# O "state" do OAuth é um JWT curto: protege o callback contra CSRF sem guardar nada no banco.
STATE_PURPOSE = "rd_oauth_state"
STATE_EXPIRE_MINUTES = 10
FONTE_WEBHOOK = "webhook_rd"


def _url_publica(request: Request, nome_rota: str, caminho: str) -> str:
    """Endereço público de uma rota: API_PUBLIC_URL ou, sem ela, o da própria requisição."""
    base = get_settings().api_public_url
    return f"{base}{caminho}" if base else str(request.url_for(nome_rota))


def _redirect_uri(request: Request) -> str:
    return _url_publica(request, "rd_callback", "/oauth/rd/callback")


def _base_webhook(request: Request) -> str:
    return _url_publica(request, "rd_station_webhook", "/webhooks/rd-station")


def _voltar_ao_front(resultado: str, motivo: str | None = None) -> RedirectResponse:
    """Devolve o navegador para a tela de integração com o resultado na query string."""
    params = {"rd": resultado}
    if motivo:
        params["motivo"] = motivo[:300]
    url = f"{get_settings().frontend_url}/integracao-rd?{urlencode(params)}"
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)


async def _criar_webhooks(session: AsyncSession, request: Request) -> str | None:
    """Cria os webhooks no RD Station e guarda o resultado; devolve o erro, se houver."""
    try:
        access = await integracao.access_token_valido(session)
        await session.commit()  # guarda o token se ele foi renovado
        config = await integracao.obter_ou_criar_config(session)
        await integracao.criar_webhooks(access, config, _base_webhook(request))
        await session.commit()
        return None
    except RdStationErro as e:
        await session.rollback()
        config = await integracao.obter_ou_criar_config(session)
        config.webhook_erro = str(e)
        await session.commit()
        return str(e)


async def _desfazer_autorizacao(session: AsyncSession, request: Request) -> RdStationConfig:
    """Remove os webhooks do RD (se possível), troca o token do webhook e apaga o token OAuth.

    Chame antes de outras alterações na sessão: uma falha no RD desfaz o que estiver pendente.
    Não faz commit.
    """
    config = await integracao.obter_ou_criar_config(session)
    if await integracao.obter_token(session) is not None:
        try:
            access = await integracao.access_token_valido(session)
            await integracao.remover_webhooks(access, config, _base_webhook(request))
        except RdStationErro as e:
            # Os webhooks que ficarem no RD passam a ser recusados pelo token novo abaixo.
            logger.warning("Não foi possível remover os webhooks do RD Station: %s", e)
            await session.rollback()
            config = await integracao.obter_ou_criar_config(session)
    config.webhooks = []
    config.webhook_erro = None
    config.webhook_token = integracao.novo_webhook_token()
    await session.execute(delete(RdStationToken))
    envio.limpar_cache()  # funil e usuários podem ser de outra conta na próxima autorização
    return config


class UsuarioResumo(BaseModel):
    id: UUID
    nome: str


class FalhaSincronizacao(BaseModel):
    em: datetime
    mensagem: str


class WebhookRd(BaseModel):
    event_type: str
    uuid: str | None = None


class ConexaoResponse(BaseModel):
    ativo: bool
    # Passo: credenciais do app
    client_id: str | None
    has_client_secret: bool
    redirect_uri: str
    # Passo: usuário responsável
    usuario_integracao: UsuarioResumo | None
    # Passo: autorização OAuth
    connected: bool
    token_status: Literal["ausente", "valido", "expirado"]
    conectado_em: datetime | None
    token_expira_em: datetime | None
    # Webhooks criados pelo CRM Assist no RD Station
    webhooks: list[WebhookRd]
    webhook_erro: str | None
    ultima_sincronizacao: datetime | None
    ultima_falha: FalhaSincronizacao | None
    falhas_ultimos_7_dias: int


async def _status_token(session: AsyncSession) -> tuple[RdStationToken | None, str]:
    """Token atual e seu estado; tenta renovar se estiver vencido."""
    token = await integracao.obter_token(session)
    if token is None:
        return None, "ausente"
    if not integracao.token_expirado(token):
        return token, "valido"
    try:
        await integracao.access_token_valido(session)
        await session.commit()
        return await integracao.obter_token(session), "valido"
    except RdStationErro:
        await session.rollback()
        return await integracao.obter_token(session), "expirado"


@router.get(
    "/conexao",
    response_model=ConexaoResponse,
    summary="Configuração e estado da integração com o RD Station (admin)",
)
async def obter_conexao(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[Usuario, Depends(get_current_admin)],
):
    # Antes de carregar a config: um rollback na renovação expiraria os objetos carregados.
    token, token_status = await _status_token(session)
    # Garante o registro (e o token do webhook) desde a primeira visita à tela.
    config = await integracao.obter_ou_criar_config(session)
    await session.commit()

    usuario = None
    if config.usuario_integracao_id:
        u = await session.get(Usuario, config.usuario_integracao_id)
        if u:
            usuario = UsuarioResumo(id=u.id, nome=u.nome)

    ultima = (
        await session.execute(
            select(func.max(Sincronizacao.created_at)).where(
                Sincronizacao.fonte == FONTE_WEBHOOK, Sincronizacao.status == "success"
            )
        )
    ).scalar_one_or_none()
    falha = (
        await session.execute(
            select(Sincronizacao)
            .where(Sincronizacao.fonte == FONTE_WEBHOOK, Sincronizacao.status == "error")
            .order_by(Sincronizacao.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    n_falhas = (
        await session.execute(
            select(func.count()).where(
                Sincronizacao.fonte == FONTE_WEBHOOK,
                Sincronizacao.status == "error",
                Sincronizacao.created_at >= datetime.now(UTC) - timedelta(days=7),
            )
        )
    ).scalar_one()

    return ConexaoResponse(
        ativo=config.ativo,
        client_id=config.client_id,
        has_client_secret=bool(config.client_secret),
        redirect_uri=_redirect_uri(request),
        usuario_integracao=usuario,
        connected=token is not None,
        token_status=token_status,
        conectado_em=token.created_at if token else None,
        token_expira_em=token.expires_at if token else None,
        webhooks=[WebhookRd(**w) for w in (config.webhooks or [])],
        webhook_erro=config.webhook_erro,
        ultima_sincronizacao=ultima,
        # Só interessa a falha que aconteceu depois do último sucesso.
        ultima_falha=(
            FalhaSincronizacao(
                em=falha.created_at, mensagem=str((falha.detalhe or {}).get("erro", "Erro desconhecido"))
            )
            if falha and (ultima is None or falha.created_at > ultima)
            else None
        ),
        falhas_ultimos_7_dias=int(n_falhas),
    )


class ConfiguracaoBody(BaseModel):
    ativo: bool | None = None
    usuario_integracao_id: UUID | None = None


@router.put(
    "/configuracao",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Ativa/desativa a integração e define o usuário responsável (admin)",
)
async def salvar_configuracao(
    body: ConfiguracaoBody,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[Usuario, Depends(get_current_admin)],
):
    config = await integracao.obter_ou_criar_config(session)
    if body.ativo is not None:
        config.ativo = body.ativo
    if "usuario_integracao_id" in body.model_fields_set:
        if body.usuario_integracao_id is not None:
            u = await session.get(Usuario, body.usuario_integracao_id)
            if u is None or not u.ativo:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Usuário não encontrado ou inativo")
        config.usuario_integracao_id = body.usuario_integracao_id
    await session.commit()


class SalvarCredenciaisBody(BaseModel):
    client_id: str = Field(min_length=1, max_length=255)
    # Vazio mantém o secret já salvo (a tela nunca recebe o valor de volta).
    client_secret: str | None = Field(default=None, max_length=1000)


@router.put(
    "/credenciais",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Salva Client ID e Client Secret do app RD Station (admin)",
)
async def salvar_credenciais(
    body: SalvarCredenciaisBody,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[Usuario, Depends(get_current_admin)],
):
    client_id = body.client_id.strip()
    secret = (body.client_secret or "").strip()
    if not client_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Informe o Client ID")
    atuais = await integracao.credenciais(session)
    config = await integracao.obter_ou_criar_config(session)
    if not secret and not config.client_secret:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Informe o Client Secret")
    novo_secret = secret or config.client_secret
    mudou = atuais is None or atuais.client_id != client_id or atuais.client_secret != novo_secret
    # Token emitido para outro app deixa de valer: exige nova autorização.
    if mudou:
        config = await _desfazer_autorizacao(session, request)
    config.client_id = client_id
    config.client_secret = novo_secret
    await session.commit()


@router.post(
    "/webhook",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cria (ou recria) no RD Station os webhooks que enviam dados ao CRM Assist (admin)",
)
async def criar_webhook(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[Usuario, Depends(get_current_admin)],
):
    erro = await _criar_webhooks(session, request)
    if erro:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, erro)


@router.post(
    "/testar",
    summary="Testa a conexão criando (ou atualizando) um contato de teste no RD Station CRM (admin)",
)
async def testar(
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[Usuario, Depends(get_current_admin)],
):
    try:
        return await envio.enviar_contato_teste(session)
    except RdStationErro as e:
        await session.rollback()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e


@router.post(
    "/authorize-url",
    summary="Gera a URL de autorização do RD Station com state assinado (admin)",
)
async def gerar_url_autorizacao(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[Usuario, Depends(get_current_admin)],
):
    s = get_settings()
    cred = await integracao.credenciais(session)
    if cred is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Cadastre o Client ID e o Client Secret antes de conectar",
        )
    state = jwt.encode(
        {
            "sub": str(admin.id),
            "purpose": STATE_PURPOSE,
            "exp": datetime.now(UTC) + timedelta(minutes=STATE_EXPIRE_MINUTES),
        },
        s.jwt_secret,
        algorithm=s.jwt_algorithm,
    )
    # https://developers.rdstation.com/reference/crm-v2-authentication-step-2
    return {"url": integracao.url_autorizacao(cred, _redirect_uri(request), state)}


@router.get(
    "/callback",
    summary="Callback OAuth2 — troca code por tokens, cria os webhooks e volta para a tela",
)
async def rd_callback(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
):
    if error:
        return _voltar_ao_front("erro", "O RD Station recusou a autorização")
    s = get_settings()
    try:
        payload = jwt.decode(state or "", s.jwt_secret, algorithms=[s.jwt_algorithm])
    except JWTError:
        payload = None
    if not payload or payload.get("purpose") != STATE_PURPOSE:
        return _voltar_ao_front("erro", "Autorização expirada ou inválida. Tente conectar novamente.")
    if not code:
        return _voltar_ao_front("erro", "O RD Station não enviou o código de autorização")
    cred = await integracao.credenciais(session)
    if cred is None:
        return _voltar_ao_front("erro", "Credenciais do RD Station não cadastradas")
    try:
        await integracao.trocar_code_por_token(session, cred, code, _redirect_uri(request))
    except RdStationErro as e:
        await session.rollback()
        return _voltar_ao_front("erro", f"{e}. Confira o Client Secret.")
    await session.commit()
    # Último passo da configuração: o próprio CRM Assist cadastra os webhooks no RD Station.
    erro_webhook = await _criar_webhooks(session, request)
    return _voltar_ao_front("conectado", erro_webhook)


@router.delete(
    "/conexao",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Desconecta o RD Station, removendo os webhooks e apagando os tokens OAuth (admin)",
)
async def desconectar(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[Usuario, Depends(get_current_admin)],
):
    await _desfazer_autorizacao(session, request)
    await session.commit()


@router.get("/status", summary="Indica se o RD Station está autorizado (requer usuário autenticado)")
async def rd_status(
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[Usuario, Depends(get_current_user)],
):
    token = await integracao.obter_token(session)
    # Vencido mas renovável conta como conectado: a renovação acontece no próximo uso.
    utilizavel = token is not None and (not integracao.token_expirado(token) or bool(token.refresh_token))
    return {"connected": utilizavel}
