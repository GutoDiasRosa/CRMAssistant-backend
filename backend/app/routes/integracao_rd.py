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
from app.core.rd_station_sync import integracao
from app.core.rd_station_sync.integracao import RdStationErro
from app.db.database import get_session
from app.db.models import RdStationToken, Sincronizacao, Usuario
from app.dependencies import get_current_admin, get_current_user

router = APIRouter(prefix="/oauth/rd", tags=["rd-station"])

# O "state" do OAuth é um JWT curto: protege o callback contra CSRF sem guardar nada no banco.
STATE_PURPOSE = "rd_oauth_state"
STATE_EXPIRE_MINUTES = 10
FONTE_WEBHOOK = "webhook_rd"


def _voltar_ao_front(resultado: str, motivo: str | None = None) -> RedirectResponse:
    """Devolve o navegador para a tela de integração com o resultado na query string."""
    params = {"rd": resultado}
    if motivo:
        params["motivo"] = motivo[:200]
    url = f"{get_settings().frontend_url}/integracao-rd?{urlencode(params)}"
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)


class UsuarioResumo(BaseModel):
    id: UUID
    nome: str


class FalhaSincronizacao(BaseModel):
    em: datetime
    mensagem: str


class ConexaoResponse(BaseModel):
    ativo: bool
    # Passo: credenciais do app
    client_id: str | None
    has_client_secret: bool
    credenciais_origem: Literal["tela", "env"] | None
    redirect_uri: str
    # Passo: usuário responsável
    usuario_integracao: UsuarioResumo | None
    # Passo: autorização OAuth
    connected: bool
    token_status: Literal["ausente", "valido", "expirado"]
    conectado_em: datetime | None
    token_expira_em: datetime | None
    # Passo: tokens da API (o privado nunca volta para a tela)
    token_publico: str | None
    has_token_privado: bool
    # Passo: webhook
    webhook_url: str
    webhook_secret_configurado: bool
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
        token = await integracao.renovar_token(session, token)
        await session.commit()
        return token, "valido"
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
    s = get_settings()
    # Antes de carregar a config: um rollback na renovação expiraria os objetos carregados.
    token, token_status = await _status_token(session)
    # Garante o registro (e o token do webhook) desde a primeira visita à tela.
    config = await integracao.obter_ou_criar_config(session)
    await session.commit()

    if config.client_id:
        client_id, has_secret, origem = config.client_id, bool(config.client_secret), "tela"
    elif s.rd_client_id:
        client_id, has_secret, origem = s.rd_client_id, bool(s.rd_client_secret), "env"
    else:
        client_id, has_secret, origem = None, False, None

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

    base_webhook = (
        f"{s.api_public_url}/webhooks/rd-station"
        if s.api_public_url
        else str(request.url_for("rd_station_webhook"))
    )
    return ConexaoResponse(
        ativo=config.ativo,
        client_id=client_id,
        has_client_secret=has_secret,
        credenciais_origem=origem,
        redirect_uri=s.rd_redirect_uri,
        usuario_integracao=usuario,
        connected=token is not None,
        token_status=token_status,
        conectado_em=token.created_at if token else None,
        token_expira_em=token.expires_at if token else None,
        token_publico=config.token_publico,
        has_token_privado=bool(config.token_privado),
        webhook_url=f"{base_webhook}?{urlencode({'token': config.webhook_token})}",
        webhook_secret_configurado=bool(s.rd_webhook_secret.strip()),
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
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[Usuario, Depends(get_current_admin)],
):
    client_id = body.client_id.strip()
    secret = (body.client_secret or "").strip()
    if not client_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Informe o Client ID")
    # Compara com as credenciais em uso (tela ou .env) para saber se o token continua válido.
    atuais = await integracao.credenciais(session)
    config = await integracao.obter_ou_criar_config(session)
    if not secret and not config.client_secret:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Informe o Client Secret")
    novo_secret = secret or config.client_secret
    mudou = atuais is None or atuais.client_id != client_id or atuais.client_secret != novo_secret
    config.client_id = client_id
    config.client_secret = novo_secret
    # Token emitido para outro app deixa de valer: exige nova autorização.
    if mudou:
        await session.execute(delete(RdStationToken))
    await session.commit()


class TokensApiBody(BaseModel):
    token_publico: str = Field(min_length=1, max_length=255)
    # Vazio mantém o token privado já salvo.
    token_privado: str | None = Field(default=None, max_length=255)


@router.put(
    "/tokens-api",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Salva os tokens público e privado da API do RD Station (admin)",
)
async def salvar_tokens_api(
    body: TokensApiBody,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[Usuario, Depends(get_current_admin)],
):
    publico = body.token_publico.strip()
    privado = (body.token_privado or "").strip()
    if not publico:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Informe o token público")
    config = await integracao.obter_ou_criar_config(session)
    if not privado and not config.token_privado:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Informe o token privado")
    config.token_publico = publico
    if privado:
        config.token_privado = privado
    await session.commit()


@router.post(
    "/webhook-token",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Gera um novo token para a URL do webhook; a URL antiga deixa de funcionar (admin)",
)
async def regenerar_webhook_token(
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[Usuario, Depends(get_current_admin)],
):
    config = await integracao.obter_ou_criar_config(session)
    config.webhook_token = integracao.novo_webhook_token()
    await session.commit()


@router.post("/testar", summary="Testa o acesso à API do RD Station com o token atual (admin)")
async def testar(
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[Usuario, Depends(get_current_admin)],
):
    try:
        resultado = await integracao.testar_conexao(session)
    except RdStationErro as e:
        await session.rollback()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e
    await session.commit()  # guarda o token se ele foi renovado durante o teste
    return resultado


@router.post(
    "/authorize-url",
    summary="Gera a URL de autorização do RD Station com state assinado (admin)",
)
async def gerar_url_autorizacao(
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
    # Parâmetros da documentação: https://developers.rdstation.com/reference/gerar-code
    params = {
        "client_id": cred.client_id,
        "redirect_uri": s.rd_redirect_uri,
        "state": state,
    }
    return {"url": f"{s.rd_oauth_authorize_url}?{urlencode(params)}"}


@router.get(
    "/callback",
    summary="Callback OAuth2 — troca code por tokens e volta para a tela de integração",
)
async def rd_callback(
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
        await integracao.trocar_code_por_token(session, cred, code)
    except RdStationErro as e:
        await session.rollback()
        return _voltar_ao_front("erro", f"{e}. Confira o Client Secret.")
    await session.commit()
    return _voltar_ao_front("conectado")


@router.delete(
    "/conexao",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Desconecta o RD Station, apagando os tokens OAuth (admin)",
)
async def desconectar(
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[Usuario, Depends(get_current_admin)],
):
    await session.execute(delete(RdStationToken))
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
