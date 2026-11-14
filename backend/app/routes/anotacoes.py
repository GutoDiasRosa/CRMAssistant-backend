"""Anotações dos vendedores (agenda): ligadas a leads e oportunidades, lidas também pelo assistente."""

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_session
from app.db.models import Interacao, Usuario
from app.dependencies import get_current_user
from app.domain.repositories.anotacoes import AnotacaoRepository
from app.routes.leads import _carregar as carregar_lead
from app.routes.oportunidades import Pessoa
from app.routes.oportunidades import _carregar as carregar_oportunidade

router = APIRouter(prefix="/api/crm/anotacoes", tags=["crm"])

TipoAnotacao = Literal["nota", "ligacao", "email", "whatsapp", "reuniao", "visita"]


class AnotacaoBody(BaseModel):
    # Pelo menos um dos dois. Com a oportunidade, o lead dela é ligado automaticamente.
    lead_id: UUID | None = None
    oportunidade_id: UUID | None = None
    tipo: TipoAnotacao = "nota"
    descricao: str = Field(min_length=1, max_length=5000)
    # Quando o contato aconteceu; sem valor, agora.
    data: datetime | None = None
    # Próximo contato combinado (aparece na agenda).
    retorno_em: date | None = None


class Referencia(BaseModel):
    id: UUID
    nome: str


class AnotacaoResposta(BaseModel):
    id: UUID
    tipo: str
    descricao: str
    data: datetime
    retorno_em: date | None
    autor: Pessoa | None
    lead: Referencia | None
    oportunidade: Referencia | None
    pode_editar: bool


def _resposta(a: Interacao, user: Usuario) -> AnotacaoResposta:
    return AnotacaoResposta(
        id=a.id,
        tipo=a.tipo,
        descricao=a.descricao,
        data=a.data,
        retorno_em=a.retorno_em,
        autor=Pessoa(id=a.autor.id, nome=a.autor.nome) if a.autor else None,
        lead=Referencia(id=a.lead.id, nome=a.lead.nome) if a.lead else None,
        oportunidade=Referencia(id=a.oportunidade.id, nome=a.oportunidade.nome) if a.oportunidade else None,
        pode_editar=a.usuario_id == user.id or user.perfil == "ADMIN",
    )


async def _vinculos(session: AsyncSession, user: Usuario, body: AnotacaoBody) -> tuple[UUID | None, UUID | None]:
    """Confere o acesso (RF07) ao lead/oportunidade e devolve (lead_id, oportunidade_id)."""
    if not body.lead_id and not body.oportunidade_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Informe o lead ou a oportunidade")
    lead_id = None
    if body.lead_id:
        lead_id = (await carregar_lead(session, user, body.lead_id)).id
    op_id = None
    if body.oportunidade_id:
        op = await carregar_oportunidade(session, user, body.oportunidade_id)
        op_id = op.id
        lead_id = lead_id or op.lead_id
    return lead_id, op_id


async def _carregar(session: AsyncSession, user: Usuario, anotacao_id: UUID, *, editar: bool = False) -> Interacao:
    a = await AnotacaoRepository(session, user).obter(anotacao_id)
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Anotação não encontrada")
    if editar and not (a.usuario_id == user.id or user.perfil == "ADMIN"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Só quem escreveu a anotação pode alterá-la")
    return a


@router.get("", response_model=list[AnotacaoResposta], summary="Anotações por cliente ou período (RF07)")
async def listar(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[Usuario, Depends(get_current_user)],
    lead_id: UUID | None = None,
    oportunidade_id: UUID | None = None,
    inicio: Annotated[datetime | None, Query(description="Contatos a partir deste instante (inclusive)")] = None,
    fim: Annotated[datetime | None, Query(description="Contatos antes deste instante")] = None,
    usuario_id: Annotated[UUID | None, Query(description="Filtra pelo autor")] = None,
    limite: Annotated[int, Query(ge=1, le=200)] = 100,
):
    anotacoes = await AnotacaoRepository(session, user).listar(
        lead_id=lead_id, oportunidade_id=oportunidade_id, inicio=inicio, fim=fim, autor_id=usuario_id, limit=limite
    )
    return [_resposta(a, user) for a in anotacoes]


@router.get("/retornos", response_model=list[AnotacaoResposta], summary="Retornos agendados até uma data (RF07)")
async def retornos(
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[Usuario, Depends(get_current_user)],
    ate: date,
    de: Annotated[date | None, Query(description="Sem valor, inclui os atrasados")] = None,
    usuario_id: Annotated[UUID | None, Query(description="Filtra pelo autor")] = None,
):
    anotacoes = await AnotacaoRepository(session, user).retornos(ate=ate, de=de, autor_id=usuario_id)
    return [_resposta(a, user) for a in anotacoes]


@router.post("", response_model=AnotacaoResposta, status_code=status.HTTP_201_CREATED, summary="Registra uma anotação")
async def criar(
    body: AnotacaoBody,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[Usuario, Depends(get_current_user)],
):
    lead_id, op_id = await _vinculos(session, user, body)
    a = Interacao(
        lead_id=lead_id,
        oportunidade_id=op_id,
        usuario_id=user.id,
        tipo=body.tipo,
        descricao=body.descricao.strip(),
        retorno_em=body.retorno_em,
    )
    if body.data:
        a.data = body.data
    session.add(a)
    await session.commit()
    return _resposta(await _carregar(session, user, a.id), user)


@router.put("/{anotacao_id}", response_model=AnotacaoResposta, summary="Altera uma anotação (autor ou admin)")
async def atualizar(
    anotacao_id: UUID,
    body: AnotacaoBody,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[Usuario, Depends(get_current_user)],
):
    a = await _carregar(session, user, anotacao_id, editar=True)
    lead_id, op_id = await _vinculos(session, user, body)
    a.lead_id, a.oportunidade_id = lead_id, op_id
    a.tipo = body.tipo
    a.descricao = body.descricao.strip()
    if body.data:
        a.data = body.data
    a.retorno_em = body.retorno_em
    await session.commit()
    return _resposta(await _carregar(session, user, anotacao_id), user)


@router.delete("/{anotacao_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Exclui uma anotação (autor ou admin)")
async def excluir(
    anotacao_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[Usuario, Depends(get_current_user)],
):
    a = await _carregar(session, user, anotacao_id, editar=True)
    await session.delete(a)
    await session.commit()
