from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "CRMAssistant API"
    debug: bool = False

    postgres_url: str = "postgresql+asyncpg://crm:crm@localhost:5432/crmassistant"

    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24

    cors_origins: str = "*"

    # RD Station OAuth (documentação RD: ajuste URLs se a API do seu produto diferir)
    rd_client_id: str = ""
    rd_client_secret: str = ""
    rd_redirect_uri: str = "http://localhost:8000/oauth/rd/callback"
    rd_oauth_authorize_url: str = "https://api.rd.services/auth/dialog"
    rd_oauth_token_url: str = "https://api.rd.services/auth/token"

    # Webhook: segredo compartilhado ou validação adicional conforme doc RD
    rd_webhook_secret: str = ""

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5"
    # Esforço do modelo: "low" mantém o chat rápido (RNF01, resposta em até 10 s).
    # Suba para "medium"/"high" se precisar de análises mais elaboradas.
    anthropic_effort: str = "low"


@lru_cache
def get_settings() -> Settings:
    return Settings()
