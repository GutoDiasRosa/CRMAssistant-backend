"""Anotações dos vendedores sobre os clientes, com o filtro de acesso por perfil (RF07)."""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Interacao, Lead, Oportunidade, Usuario
from app.dependencies import pode_ver_todos_oportunidades
from app.domain.repositories.crm import conduz_negocio_do_lead


class AnotacaoRepository:
    def __init__(self, session: AsyncSession, usuario: Usuario):
        self.session = session
        self.usuario = usuario

    def _base(self):
        stmt = (
            select(Interacao)
            .outerjoin(Lead, Interacao.lead_id == Lead.id)
            .outerjoin(Oportunidade, Interacao.oportunidade_id == Oportunidade.id)
            .options(
                selectinload(Interacao.autor),
                selectinload(Interacao.lead),
                selectinload(Interacao.oportunidade),
            )
        )
        if not pode_ver_todos_oportunidades(self.usuario):
            # Mesma regra dos leads (ver domain/repositories/crm.py): as próprias anotações, as das
            # próprias oportunidades, todas as dos próprios leads e, nos leads cujos negócios conduz,
            # as anotações gerais do cliente (sem oportunidade), não as dos negócios de colegas.
            uid = self.usuario.id
            stmt = stmt.where(
                or_(
                    Interacao.usuario_id == uid,
                    Oportunidade.usuario_id == uid,
                    Lead.usuario_id == uid,
                    and_(Interacao.oportunidade_id.is_(None), conduz_negocio_do_lead(self.usuario)),
                )
            )
        return stmt

    async def listar(
        self,
        *,
        lead_id: UUID | None = None,
        oportunidade_id: UUID | None = None,
        inicio: datetime | None = None,
        fim: datetime | None = None,
        autor_id: UUID | None = None,
        termo: str | None = None,
        limit: int = 100,
    ) -> list[Interacao]:
        """Anotações mais recentes primeiro. `lead_id` inclui as das oportunidades do lead."""
        stmt = self._base()
        if lead_id:
            ops_do_lead = select(Oportunidade.id).where(Oportunidade.lead_id == lead_id)
            stmt = stmt.where(or_(Interacao.lead_id == lead_id, Interacao.oportunidade_id.in_(ops_do_lead)))
        if oportunidade_id:
            stmt = stmt.where(Interacao.oportunidade_id == oportunidade_id)
        if inicio:
            stmt = stmt.where(Interacao.data >= inicio)
        if fim:
            stmt = stmt.where(Interacao.data < fim)
        if autor_id:
            stmt = stmt.where(Interacao.usuario_id == autor_id)
        if termo and termo.strip():
            padrao = f"%{termo.strip()}%"
            stmt = stmt.where(
                or_(Lead.nome.ilike(padrao), Oportunidade.nome.ilike(padrao), Interacao.descricao.ilike(padrao))
            )
        stmt = stmt.order_by(Interacao.data.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars().unique().all())

    async def retornos(
        self,
        *,
        ate: date,
        de: date | None = None,
        autor_id: UUID | None = None,
        termo: str | None = None,
        limit: int = 100,
    ) -> list[Interacao]:
        """Próximos contatos agendados até `ate` (sem `de`, inclui os atrasados)."""
        stmt = self._base().where(Interacao.retorno_em.is_not(None), Interacao.retorno_em <= ate)
        if de:
            stmt = stmt.where(Interacao.retorno_em >= de)
        if autor_id:
            stmt = stmt.where(Interacao.usuario_id == autor_id)
        if termo and termo.strip():
            padrao = f"%{termo.strip()}%"
            stmt = stmt.where(or_(Lead.nome.ilike(padrao), Oportunidade.nome.ilike(padrao)))
        stmt = stmt.order_by(Interacao.retorno_em, Interacao.data).limit(limit)
        return list((await self.session.execute(stmt)).scalars().unique().all())

    async def obter(self, anotacao_id: UUID) -> Interacao | None:
        # populate_existing: recém-criada, a data (padrão do banco) ainda não foi lida.
        stmt = self._base().where(Interacao.id == anotacao_id).execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalars().unique().one_or_none()
