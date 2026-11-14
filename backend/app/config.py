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

    # Tela do front-end para onde o callback OAuth do RD Station devolve o usuário
    frontend_url: str = "http://localhost:5173"
    # Endereço público da API (URLs de callback e de webhook do RD Station).
    # No Render vem sozinho de RENDER_EXTERNAL_URL. Vazio = usa o endereço da própria
    # requisição (desenvolvimento local).
    api_public_url: str = Field(
        default="", validation_alias=AliasChoices("API_PUBLIC_URL", "RENDER_EXTERNAL_URL")
    )

    # Administrador criado (ou com a senha redefinida) a cada inicialização da API.
    # Credenciais do RD Station não vêm do ambiente: são cadastradas pela tela Integração RD.
    admin_email: str = ""
    admin_password: str = ""

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5"
    # Esforço do modelo: "low" mantém o chat rápido (RNF01, resposta em até 10 s).
    # Suba para "medium"/"high" se precisar de análises mais elaboradas.
    anthropic_effort: str = "low"

    @model_validator(mode="after")
    def _derivar_urls(self) -> "Settings":
        self.api_public_url = self.api_public_url.rstrip("/")
        self.frontend_url = self.frontend_url.rstrip("/")
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
