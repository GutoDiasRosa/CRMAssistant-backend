"""Só o token de login dá acesso à API.

O `state` do OAuth do RD Station é assinado com a mesma chave e leva o id do administrador:
antes desta regra, ele valia como token de admin pelos 10 minutos de validade.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from jose import jwt

from app.config import get_settings
from app.main import create_app
from app.security import create_access_token, payload_de_acesso


def _state_do_oauth(sub: str) -> str:
    # Mesmo formato do state gerado em POST /oauth/rd/authorize-url (app/routes/integracao_rd.py).
    s = get_settings()
    claims = {"sub": sub, "purpose": "rd_oauth_state", "exp": datetime.now(UTC) + timedelta(minutes=10)}
    return jwt.encode(claims, s.jwt_secret, algorithm=s.jwt_algorithm)


def test_token_de_login_e_aceito():
    sub = str(uuid4())
    payload = payload_de_acesso(create_access_token(sub, extra={"perfil": "ADMIN"}))
    assert payload is not None and payload["sub"] == sub


def test_extra_nao_troca_o_dono_nem_o_tipo_do_token():
    sub = str(uuid4())
    payload = payload_de_acesso(create_access_token(sub, extra={"sub": "outro", "typ": "outro"}))
    assert payload is not None and payload["sub"] == sub and payload["typ"] == "access"


def test_state_do_oauth_nao_vale_como_token():
    assert payload_de_acesso(_state_do_oauth(str(uuid4()))) is None


def test_api_recusa_o_state_do_oauth():
    client = TestClient(create_app())
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {_state_do_oauth(str(uuid4()))}"})
    assert r.status_code == 401
