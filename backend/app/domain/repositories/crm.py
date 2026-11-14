from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, case, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import STATUS_FECHADO, STATUS_GANHO, STATUS_PERDIDO, Interacao, Lead, Oportunidade, Usuario
from app.dependencies import pode_ver_todos_oportunidades

_STATUS = func.lower(func.coalesce(Oportunidade.status, ""))


def _no_periodo(coluna, inicio: datetime | None, fim: datetime | None):
    """Condição `inicio <= coluna < fim` (cada limite é opcional)."""
    condicoes = []
    if inicio:
        condicoes.append(coluna >= inicio)
    if fim:
        condicoes.append(coluna < fim)
    return and_(*condicoes) if condicoes else true()


def _contar(condicao):
    return func.count(case((condicao, Oportunidade.id)))


def _somar(condicao):
    return func.coalesce(func.sum(case((condicao, Oportunidade.valor))), 0)


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

    async def obter(self, lead_id: UUID) -> Lead | None:
        """Lead pelo id (com responsável e oportunidades), respeitando RF07."""
        stmt = self._scoped_filter(
            select(Lead)
            .options(
                selectinload(Lead.usuario),
                selectinload(Lead.oportunidades).selectinload(Oportunidade.usuario),
            )
            .where(Lead.id == lead_id)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

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
        self,
        termo: str | None = None,
        etapa: str | None = None,
        limit: int = 50,
        *,
        situacao: str | None = None,
        inicio: datetime | None = None,
        fim: datetime | None = None,
    ) -> list[Oportunidade]:
        """Filtra por nome da oportunidade/contato, etapa, situação e período, respeitando RF07.

        `situacao`: "abertas" (em andamento ou pausadas), "ganhas" ou "perdidas". O período vale
        pela data de fechamento nas ganhas/perdidas e pela data de criação nos demais casos.
        """
        stmt = select(Oportunidade).options(
            selectinload(Oportunidade.lead),
            selectinload(Oportunidade.usuario),
            selectinload(Oportunidade.interacoes).selectinload(Interacao.autor),
        )
        if termo:
            padrao = f"%{termo.strip()}%"
            stmt = stmt.outerjoin(Lead, Oportunidade.lead_id == Lead.id).where(
                (Oportunidade.nome.ilike(padrao)) | (Lead.nome.ilike(padrao))
            )
        if etapa:
            stmt = stmt.where(Oportunidade.etapa_funil.ilike(f"%{etapa.strip()}%"))
        if situacao == "ganhas":
            stmt = stmt.where(_STATUS.in_(STATUS_GANHO))
        elif situacao == "perdidas":
            stmt = stmt.where(_STATUS.in_(STATUS_PERDIDO))
        elif situacao == "abertas":
            stmt = stmt.where(~_STATUS.in_(STATUS_FECHADO))
        data = Oportunidade.fechada_em if situacao in ("ganhas", "perdidas") else Oportunidade.created_at
        stmt = stmt.where(_no_periodo(data, inicio, fim))
        stmt = self._scoped_filter(stmt.order_by(Oportunidade.valor.desc().nulls_last()).limit(limit))
        r = await self.session.execute(stmt)
        return list(r.scalars().unique().all())

    async def obter(self, op_id: UUID) -> Oportunidade | None:
        """Oportunidade pelo id (com contato, responsável e anotações), respeitando RF07."""
        stmt = self._scoped_filter(
            select(Oportunidade)
            .options(
                selectinload(Oportunidade.lead),
                selectinload(Oportunidade.usuario),
                selectinload(Oportunidade.interacoes).selectinload(Interacao.autor),
            )
            .where(Oportunidade.id == op_id)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def performance_time(
        self,
        *,
        inicio: datetime | None = None,
        fim: datetime | None = None,
    ) -> dict:
        """Números por vendedor no período (RF06 + RF07).

        Pela data de criação (created_at), as oportunidades que entraram no período:
        - prospectados / valor_prospectado: quantidade e valor;
        - qualificados: dessas, as que já passaram da etapa de prospecção (ou foram ganhas);
        - convertidos: dessas, as já ganhas (base da taxa de conversão).
        Pela data de fechamento (fechada_em), o que foi encerrado no período:
        - fechados / valor_fechado: vendas ganhas;
        - perdidos / valor_perdido: oportunidades perdidas.
        Vendedores (SDR/CLOSER) ativos aparecem mesmo sem movimento; os demais perfis só aparecem
        se tiverem algum. Perfis sem visão total recebem só a própria linha.
        """
        ganho = _STATUS.in_(STATUS_GANHO)
        perdido = _STATUS.in_(STATUS_PERDIDO)
        etapa = func.lower(func.coalesce(Oportunidade.etapa_funil, ""))
        qualificado = or_(ganho, and_(etapa != "", ~etapa.like("prospec%")))
        criada = _no_periodo(Oportunidade.created_at, inicio, fim)
        ganha_no_periodo = and_(ganho, _no_periodo(Oportunidade.fechada_em, inicio, fim))
        perdida_no_periodo = and_(perdido, _no_periodo(Oportunidade.fechada_em, inicio, fim))

        colunas = {
            "prospectados": _contar(criada),
            "valor_prospectado": _somar(criada),
            "qualificados": _contar(and_(criada, qualificado)),
            "convertidos": _contar(and_(criada, ganho)),
            "fechados": _contar(ganha_no_periodo),
            "valor_fechado": _somar(ganha_no_periodo),
            "perdidos": _contar(perdida_no_periodo),
            "valor_perdido": _somar(perdida_no_periodo),
        }
        stmt = (
            select(Usuario.id, Usuario.nome, Usuario.perfil, *colunas.values())
            .outerjoin(Oportunidade, Oportunidade.usuario_id == Usuario.id)
            .where(Usuario.ativo.is_(True))
            .group_by(Usuario.id, Usuario.nome, Usuario.perfil)
            .having(
                or_(
                    colunas["prospectados"] > 0,
                    colunas["fechados"] > 0,
                    colunas["perdidos"] > 0,
                    Usuario.perfil.in_(("SDR", "CLOSER")),
                )
            )
            .order_by(Usuario.nome)
        )
        if not pode_ver_todos_oportunidades(self.usuario):
            stmt = stmt.where(Usuario.id == self.usuario.id)
        rows = (await self.session.execute(stmt)).all()

        vendedores = []
        for uid, nome, perfil, *numeros in rows:
            linha = {"id": str(uid), "nome": nome, "perfil": perfil}
            for chave, n in zip(colunas, numeros):
                linha[chave] = float(n) if chave.startswith("valor_") else int(n)
            vendedores.append(linha)
        totais = {chave: sum(v[chave] for v in vendedores) for chave in colunas}
        return {"vendedores": vendedores, "totais": totais}

    async def metricas_resumo(self, *, inicio: datetime | None = None, fim: datetime | None = None) -> dict:
        """Indicadores do período, respeitando RF07.

        Leads e oportunidades contam pela data de criação; ganhas e perdidas, pela data de
        fechamento. Sem período, considera tudo.
        """
        criada = _no_periodo(Oportunidade.created_at, inicio, fim)
        ganha = and_(_STATUS.in_(STATUS_GANHO), _no_periodo(Oportunidade.fechada_em, inicio, fim))
        perdida = and_(_STATUS.in_(STATUS_PERDIDO), _no_periodo(Oportunidade.fechada_em, inicio, fim))
        aberta = and_(criada, ~_STATUS.in_(STATUS_FECHADO))

        base_leads = select(func.count()).select_from(Lead).where(_no_periodo(Lead.created_at, inicio, fim))
        base_ops = select(
            _contar(criada), _somar(criada), _contar(aberta), _somar(aberta),
            _contar(ganha), _somar(ganha), _contar(perdida), _somar(perdida),
        )
        if not pode_ver_todos_oportunidades(self.usuario):
            base_leads = base_leads.where(Lead.usuario_id == self.usuario.id)
            base_ops = base_ops.where(Oportunidade.usuario_id == self.usuario.id)
        n_leads = (await self.session.execute(base_leads)).scalar_one()
        n_ops, valor_total, n_abertas, valor_abertas, n_ganhas, valor_ganhas, n_perdidas, valor_perdidas = (
            await self.session.execute(base_ops)
        ).one()
        return {
            "total_leads": int(n_leads),
            "total_oportunidades": int(n_ops),
            "valor_total_oportunidades": float(valor_total),
            "abertas": {"quantidade": int(n_abertas), "valor": float(valor_abertas)},
            "ganhas": {"quantidade": int(n_ganhas), "valor": float(valor_ganhas)},
            "perdidas": {"quantidade": int(n_perdidas), "valor": float(valor_perdidas)},
        }
