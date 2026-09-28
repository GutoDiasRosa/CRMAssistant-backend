from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.database import get_session
from app.db.models import RdStationConfig, RdStationToken, Sincronizacao, Usuario
from app.dependencies import get_current_admin, get_current_user

router = APIRouter(prefix="/oauth/rd", tags=["rd-station"])

# O "state" do OAuth é um JWT curto: protege o callback contra CSRF sem guardar nada no banco.
STATE_PURPOSE = "rd_oauth_state"
STATE_EXPIRE_MINUTES = 10


class Credenciais(BaseModel):
    client_id: str
    client_secret: str


async def _credenciais(session: AsyncSession) -> Credenciais | None:
    """Credenciais cadastradas pela tela; na falta delas, as do .env."""
    row = (await session.execute(select(RdStationConfig).limit(1))).scalar_one_or_none()
    if row:
        return Credenciais(client_id=row.client_id, client_secret=row.client_secret)
    s = get_settings()
    if s.rd_client_id and s.rd_client_secret:
        return Credenciais(client_id=s.rd_client_id, client_secret=s.rd_client_secret)
    return None


def _voltar_ao_front(resultado: str, motivo: str | None = None) -> RedirectResponse:
    """Devolve o navegador para a tela de integração com o resultado na query string."""
    params = {"rd": resultado}
    if motivo:
        params["motivo"] = motivo[:200]
    url = f"{get_settings().frontend_url}/integracao-rd?{urlencode(params)}"
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)


class ConexaoResponse(BaseModel):
    connected: bool
    client_id: str | None
    has_client_secret: bool
    credenciais_origem: str | None  # "tela", "env" ou None
    redirect_uri: str
    webhook_url: str
    webhook_secret_configurado: bool
    conectado_em: datetime | None
    token_expira_em: datetime | None
    ultima_sincronizacao: datetime | None


@router.get(
    "/conexao",
    response_model=ConexaoResponse,
    summary="Detalhes da conexão com o RD Station (admin)",
)
async def obter_conexao(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[Usuario, Depends(get_current_admin)],
):
    s = get_settings()
    config = (await session.execute(select(RdStationConfig).limit(1))).scalar_one_or_none()
    token = (await session.execute(select(RdStationToken).limit(1))).scalar_one_or_none()
    ultima = (
        await session.execute(
            select(Sincronizacao.created_at).order_by(Sincronizacao.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()

    if config:
        client_id, has_secret, origem = config.client_id, bool(config.client_secret), "tela"
    elif s.rd_client_id:
        client_id, has_secret, origem = s.rd_client_id, bool(s.rd_client_secret), "env"
    else:
        client_id, has_secret, origem = None, False, None

    return ConexaoResponse(
        connected=token is not None,
        client_id=client_id,
        has_client_secret=has_secret,
        credenciais_origem=origem,
        redirect_uri=s.rd_redirect_uri,
        webhook_url=(
            f"{s.api_public_url}/webhooks/rd-station"
            if s.api_public_url
            else str(request.url_for("rd_station_webhook"))
        ),
        webhook_secret_configurado=bool(s.rd_webhook_secret.strip()),
        conectado_em=token.created_at if token else None,
        token_expira_em=token.expires_at if token else None,
        ultima_sincronizacao=ultima,
    )


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
    row = (await session.execute(select(RdStationConfig).limit(1))).scalar_one_or_none()
    if row is None:
        if not secret:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Informe o Client Secret")
        session.add(RdStationConfig(client_id=client_id, client_secret=secret))
        mudou = True
    else:
        mudou = row.client_id != client_id or (bool(secret) and secret != row.client_secret)
        row.client_id = client_id
        if secret:
            row.client_secret = secret
    # Token emitido para outro app deixa de valer: exige nova autorização.
    if mudou:
        await session.execute(delete(RdStationToken))
    await session.commit()


@router.post(
    "/authorize-url",
    summary="Gera a URL de autorização do RD Station com state assinado (admin)",
)
async def gerar_url_autorizacao(
    session: Annotated[AsyncSession, Depends(get_session)],
    admin: Annotated[Usuario, Depends(get_current_admin)],
):
    s = get_settings()
    cred = await _credenciais(session)
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
    params = {
        "response_type": "code",
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
        return _voltar_ao_front("erro", f"O RD Station recusou a autorização ({error})")
    s = get_settings()
    try:
        payload = jwt.decode(state or "", s.jwt_secret, algorithms=[s.jwt_algorithm])
    except JWTError:
        payload = None
    if not payload or payload.get("purpose") != STATE_PURPOSE:
        return _voltar_ao_front("erro", "Autorização expirada ou inválida. Tente conectar novamente.")
    if not code:
        return _voltar_ao_front("erro", "O RD Station não enviou o código de autorização")
    cred = await _credenciais(session)
    if cred is None:
        return _voltar_ao_front("erro", "Credenciais do RD Station não cadastradas")

    data = {
        "client_id": cred.client_id,
        "client_secret": cred.client_secret,
        "code": code,
        "redirect_uri": s.rd_redirect_uri,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                s.rd_oauth_token_url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except httpx.HTTPError:
        return _voltar_ao_front("erro", "Não foi possível falar com o RD Station")
    if r.status_code >= 400:
        return _voltar_ao_front(
            "erro",
            f"O RD Station rejeitou as credenciais (HTTP {r.status_code}). Confira o Client Secret.",
        )
    resposta = r.json()
    access = resposta.get("access_token")
    if not access:
        return _voltar_ao_front("erro", "Resposta do RD Station sem access_token")

    expires_at = None
    if resposta.get("expires_in") is not None:
        try:
            expires_at = datetime.now(UTC) + timedelta(seconds=int(resposta["expires_in"]))
        except (TypeError, ValueError):
            pass
    await session.execute(delete(RdStationToken))
    session.add(
        RdStationToken(
            access_token=access,
            refresh_token=resposta.get("refresh_token"),
            expires_at=expires_at,
        )
    )
    await session.commit()
    return _voltar_ao_front("conectado")


@router.delete(
    "/conexao",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Desconecta o RD Station, apagando os tokens (admin)",
)
async def desconectar(
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[Usuario, Depends(get_current_admin)],
):
    await session.execute(delete(RdStationToken))
    await session.commit()


@router.get("/status", summary="Indica se há token RD configurado (requer usuário autenticado)")
async def rd_status(
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[Usuario, Depends(get_current_user)],
):
    r = await session.execute(select(RdStationToken.id).limit(1))
    return {"connected": r.scalar_one_or_none() is not None}
