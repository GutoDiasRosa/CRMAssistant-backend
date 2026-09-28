import logging
import os
from contextlib import asynccontextmanager

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.middleware.request_logging import RequestLoggingMiddleware
from app.routes import (
    auth,
    chat_crm,
    crm_dashboard,
    health,
    integracao_rd,
    usuarios,
    webhooks_rd,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


def _run_alembic_upgrade() -> None:
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.getenv("SKIP_ALEMBIC_ON_STARTUP") != "1":
        try:
            _run_alembic_upgrade()
            logger.info("Alembic upgrade concluído")
        except Exception:
            logger.exception("Alembic upgrade falhou — verifique POSTGRES_URL e o banco")
            raise
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.add_middleware(RequestLoggingMiddleware)

    origins = ["*"] if settings.cors_origins.strip() == "*" else [
        o.strip() for o in settings.cors_origins.split(",") if o.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(usuarios.router)
    app.include_router(integracao_rd.router)
    app.include_router(webhooks_rd.router)
    app.include_router(chat_crm.router)
    app.include_router(crm_dashboard.router)
    return app


app = create_app()
