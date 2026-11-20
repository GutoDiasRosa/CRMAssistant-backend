from datetime import UTC, datetime, timedelta
from typing import Any

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import get_settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Tipo gravado no token de login. Outros JWT assinados com a mesma chave (ex.: o `state` do
# OAuth do RD Station) não têm esse tipo e por isso não servem para acessar a API.
TIPO_TOKEN_ACESSO = "access"


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def create_access_token(subject: str, extra: dict[str, Any] | None = None) -> str:
    settings = get_settings()
    expire = datetime.now(UTC) + timedelta(minutes=settings.jwt_expire_minutes)
    # `extra` vem antes: não pode trocar o dono, a validade nem o tipo do token.
    payload: dict[str, Any] = {**(extra or {}), "sub": subject, "exp": expire, "typ": TIPO_TOKEN_ACESSO}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


def decode_token_safe(token: str) -> dict[str, Any] | None:
    try:
        return decode_token(token)
    except JWTError:
        return None


def payload_de_acesso(token: str) -> dict[str, Any] | None:
    """Payload de um token de login válido; None para qualquer outro JWT, mesmo bem assinado."""
    payload = decode_token_safe(token)
    if not payload or payload.get("typ") != TIPO_TOKEN_ACESSO or not payload.get("sub"):
        return None
    return payload
