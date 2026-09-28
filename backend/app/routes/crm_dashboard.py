from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_session
from app.db.models import Sincronizacao, Usuario
from app.dependencies import get_current_user
from app.domain.repositories.crm import OportunidadeRepository

router = APIRouter(prefix="/api/crm", tags=["crm"])


@router.get("/kanban", summary="Funil kanban (oportunidades por etapa, RF04 + RF07)")
async def kanban(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[Usuario, Depends(get_current_user)],
):
    repo = OportunidadeRepository(session, user)
    return await repo.kanban_por_etapa()


@router.get("/metricas", summary="Resumo numérico (RF07)")
async def metricas(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[Usuario, Depends(get_current_user)],
):
    repo = OportunidadeRepository(session, user)
    return await repo.metricas_resumo()


@router.get("/sincronizacoes", summary="Últimas sincronizações recebidas do RD Station")
async def sincronizacoes(
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[Usuario, Depends(get_current_user)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
):
    stmt = select(Sincronizacao).order_by(Sincronizacao.created_at.desc()).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": str(s.id),
            "status": s.status,
            "event_type": s.event_type,
            "created_at": s.created_at.isoformat(),
        }
        for s in rows
    ]
