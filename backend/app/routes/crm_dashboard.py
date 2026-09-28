from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_session
from app.db.models import Sincronizacao, Usuario
from app.dependencies import get_current_user, pode_ver_todos_oportunidades
from app.domain.repositories.crm import OportunidadeRepository

router = APIRouter(prefix="/api/crm", tags=["crm"])


def _inicio_do_dia(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


@router.get("/kanban", summary="Funil kanban (oportunidades por etapa, RF04 + RF07)")
async def kanban(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[Usuario, Depends(get_current_user)],
    usuario_id: Annotated[UUID | None, Query(description="Filtra pelo vendedor responsável")] = None,
    inicio: Annotated[date | None, Query(description="Criadas a partir desta data (inclusive)")] = None,
    fim: Annotated[date | None, Query(description="Criadas até esta data (inclusive)")] = None,
):
    if inicio and fim and inicio > fim:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Data inicial maior que a final")
    repo = OportunidadeRepository(session, user)
    return await repo.kanban_por_etapa(
        usuario_id=usuario_id,
        inicio=_inicio_do_dia(inicio) if inicio else None,
        fim=_inicio_do_dia(fim + timedelta(days=1)) if fim else None,
    )


@router.get("/vendedores", summary="Vendedores disponíveis para filtro (RF07)")
async def vendedores(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[Usuario, Depends(get_current_user)],
):
    """Perfis com visão total recebem todos os usuários ativos; os demais, apenas a si mesmos."""
    if not pode_ver_todos_oportunidades(user):
        return [{"id": str(user.id), "nome": user.nome}]
    stmt = select(Usuario).where(Usuario.ativo.is_(True)).order_by(Usuario.nome)
    rows = (await session.execute(stmt)).scalars().all()
    return [{"id": str(u.id), "nome": u.nome} for u in rows]


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
