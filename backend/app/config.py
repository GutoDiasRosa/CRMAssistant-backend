from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import AliasChoices, Field, model_validator
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
    # Sem valor explícito, é montada a partir de API_PUBLIC_URL (ver _derivar_urls)
    rd_redirect_uri: str = "http://localhost:8000/oauth/rd/callback"
    rd_oauth_authorize_url: str = "https://api.rd.services/auth/dialog"
    rd_oauth_token_url: str = "https://api.rd.services/auth/token"
    # Tela do front-end para onde o callback OAuth devolve o usuário
    frontend_url: str = "http://localhost:5173"
    # Endereço público da API. No Render vem sozinho de RENDER_EXTERNAL_URL.
    # Vazio = usa o endereço da própria requisição (desenvolvimento local).
    api_public_url: str = Field(
        default="", validation_alias=AliasChoices("API_PUBLIC_URL", "RENDER_EXTERNAL_URL")
    )

    # Webhook: segredo compartilhado ou validação adicional conforme doc RD
    rd_webhook_secret: str = ""

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5"
    # Esforço do modelo: "low" mantém o chat rápido (RNF01, resposta em até 10 s).
    # Suba para "medium"/"high" se precisar de análises mais elaboradas.
    anthropic_effort: str = "low"

    @model_validator(mode="after")
    def _derivar_urls(self) -> "Settings":
        self.api_public_url = self.api_public_url.rstrip("/")
        self.frontend_url = self.frontend_url.rstrip("/")
        if "rd_redirect_uri" not in self.model_fields_set and self.api_public_url:
            self.rd_redirect_uri = f"{self.api_public_url}/oauth/rd/callback"
        return self

    def database_url(self, driver: str) -> str:
        """POSTGRES_URL com o driver pedido ("asyncpg" ou "psycopg2").

        Aceita também a URL crua dos provedores, como a do Neon
        (postgresql://...?sslmode=require&channel_binding=require), traduzindo o
        parâmetro de SSL para o nome que cada driver entende.
        """
        parts = urlsplit(self.postgres_url)
        query = []
        for chave, valor in parse_qsl(parts.query):
            if chave == "channel_binding" and driver == "asyncpg":
                continue  # asyncpg não aceita esse parâmetro (o libpq do psycopg2 aceita)
            if chave in ("sslmode", "ssl"):
                chave = "ssl" if driver == "asyncpg" else "sslmode"
            query.append((chave, valor))
        return urlunsplit(parts._replace(scheme=f"postgresql+{driver}", query=urlencode(query)))


@lru_cache
def get_settings() -> Settings:
    return Settings()
