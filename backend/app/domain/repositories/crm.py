from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Lead, Oportunidade, Usuario
from app.dependencies import pode_ver_todos_oportunidades


class LeadRepository:
    def __init__(self, session: AsyncSession, usuario: Usuario):
        self.session = session
        self.usuario = usuario

    def _scoped_filter(self, stmt):
        if pode_ver_todos_oportunidades(self.usuario):
            return stmt
        return stmt.where(Lead.usuario_id == self.usuario.id)

    async def list_leads(self, limit: int = 100) -> list[Lead]:
        stmt = self._scoped_filter(select(Lead).order_by(Lead.updated_at.desc()).limit(limit))
        r = await self.session.execute(stmt)
        return list(r.scalars().all())

    async def buscar(self, termo: str, limit: int = 20) -> list[Lead]:
        """Busca por nome ou e-mail (sem diferenciar maiúsculas), respeitando RF07."""
        padrao = f"%{termo.strip()}%"
        stmt = self._scoped_filter(
            select(Lead)
            .options(selectinload(Lead.usuario), selectinload(Lead.oportunidades))
            .where((Lead.nome.ilike(padrao)) | (Lead.email.ilike(padrao)))
            .order_by(Lead.updated_at.desc())
            .limit(limit)
        )
        r = await self.session.execute(stmt)
        return list(r.scalars().all())

    async def get_by_rd_id(self, rd_lead_id: str) -> Lead | None:
        stmt = select(Lead).where(Lead.rd_lead_id == rd_lead_id)
        if not pode_ver_todos_oportunidades(self.usuario):
            stmt = stmt.where(Lead.usuario_id == self.usuario.id)
        r = await self.session.execute(stmt)
        return r.scalar_one_or_none()


class OportunidadeRepository:
    def __init__(self, session: AsyncSession, usuario: Usuario):
        self.session = session
        self.usuario = usuario

    def _scoped_filter(self, stmt):
        if pode_ver_todos_oportunidades(self.usuario):
            return stmt
        return stmt.where(Oportunidade.usuario_id == self.usuario.id)

    async def list_oportunidades(
        self,
        limit: int = 500,
        *,
        usuario_id: UUID | None = None,
        inicio: datetime | None = None,
        fim: datetime | None = None,
    ) -> list[Oportunidade]:
        """Lista oportunidades respeitando RF07, com filtros opcionais de vendedor e período.

        O período considera a data em que a oportunidade entrou no CRM Assist (created_at),
        com `inicio` inclusivo e `fim` exclusivo.
        """
        stmt = select(Oportunidade).options(selectinload(Oportunidade.lead))
        if usuario_id:
            stmt = stmt.where(Oportunidade.usuario_id == usuario_id)
        if inicio:
            stmt = stmt.where(Oportunidade.created_at >= inicio)
        if fim:
            stmt = stmt.where(Oportunidade.created_at < fim)
        stmt = self._scoped_filter(stmt.order_by(Oportunidade.updated_at.desc()).limit(limit))
        r = await self.session.execute(stmt)
        return list(r.scalars().all())

    async def kanban_por_etapa(
        self,
        *,
        usuario_id: UUID | None = None,
        inicio: datetime | None = None,
        fim: datetime | None = None,
    ) -> dict[str, list[dict]]:
        """Agrupa oportunidades por etapa_funil para UI kanban (RF04)."""
        oportunidades = await self.list_oportunidades(usuario_id=usuario_id, inicio=inicio, fim=fim)
        buckets: dict[str, list[dict]] = {}
        for op in oportunidades:
            etapa = op.etapa_funil or "Sem etapa"
            buckets.setdefault(etapa, []).append(
                {
                    "id": str(op.id),
                    "rd_deal_id": op.rd_deal_id,
                    "nome": op.nome,
                    "valor": float(op.valor) if op.valor is not None else None,
                    "status": op.status,
                    "usuario_id": str(op.usuario_id) if op.usuario_id else None,
                    "lead_id": str(op.lead_id) if op.lead_id else None,
                    "lead_nome": op.lead.nome if op.lead else None,
                }
            )
        return buckets

    async def buscar(
        self, termo: str | None = None, etapa: str | None = None, limit: int = 50
    ) -> list[Oportunidade]:
        """Filtra por nome da oportunidade/contato e/ou etapa, respeitando RF07."""
        stmt = select(Oportunidade).options(
            selectinload(Oportunidade.lead),
            selectinload(Oportunidade.usuario),
            selectinload(Oportunidade.interacoes),
        )
        if termo:
            padrao = f"%{termo.strip()}%"
            stmt = stmt.outerjoin(Lead, Oportunidade.lead_id == Lead.id).where(
                (Oportunidade.nome.ilike(padrao)) | (Lead.nome.ilike(padrao))
            )
        if etapa:
            stmt = stmt.where(Oportunidade.etapa_funil.ilike(f"%{etapa.strip()}%"))
        stmt = self._scoped_filter(stmt.order_by(Oportunidade.valor.desc().nulls_last()).limit(limit))
        r = await self.session.execute(stmt)
        return list(r.scalars().unique().all())

    async def performance_por_vendedor(self) -> list[dict]:
        """Totais por responsável (RF06). Perfis sem visão total recebem só a própria linha."""
        stmt = (
            select(
                Usuario.nome,
                Usuario.perfil,
                func.count(Oportunidade.id),
                func.coalesce(func.sum(Oportunidade.valor), 0),
            )
            .join(Oportunidade, Oportunidade.usuario_id == Usuario.id)
            .group_by(Usuario.id, Usuario.nome, Usuario.perfil)
            .order_by(func.coalesce(func.sum(Oportunidade.valor), 0).desc())
        )
        stmt = self._scoped_filter(stmt)
        rows = (await self.session.execute(stmt)).all()
        return [
            {
                "vendedor": nome,
                "perfil": perfil,
                "oportunidades": int(qtd),
                "valor_total": float(valor),
            }
            for nome, perfil, qtd, valor in rows
        ]

    async def metricas_resumo(self) -> dict:
        """Contagens agregadas respeitando RF07."""
        base_leads = select(func.count()).select_from(Lead)
        base_ops = select(func.count(), func.coalesce(func.sum(Oportunidade.valor), 0))
        if not pode_ver_todos_oportunidades(self.usuario):
            base_leads = base_leads.where(Lead.usuario_id == self.usuario.id)
            base_ops = base_ops.where(Oportunidade.usuario_id == self.usuario.id)
        n_leads = (await self.session.execute(base_leads)).scalar_one()
        n_ops, valor_total = (await self.session.execute(base_ops)).one()
        return {
            "total_leads": int(n_leads),
            "total_oportunidades": int(n_ops),
            "valor_total_oportunidades": float(valor_total),
        }
