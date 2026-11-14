"""
Ferramentas (tool use) que o assistente pode chamar para consultar o CRM.

Toda consulta passa pelos repositórios, que já aplicam o RF07: um SDR/Closer só
recebe a própria carteira, mesmo que peça dados de outra pessoa no chat.
"""

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Interacao, Oportunidade, Usuario
from app.domain.repositories.anotacoes import AnotacaoRepository
from app.domain.repositories.crm import LeadRepository, OportunidadeRepository

TOOLS: list[dict[str, Any]] = [
    {
        "name": "resumo_carteira",
        "description": (
            "Visão geral do funil visível ao usuário: total de leads, total e valor somado "
            "das oportunidades, e quantidade/valor por etapa do funil."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "listar_oportunidades",
        "description": (
            "Lista oportunidades (negociações) com empresa, contato, etapa, valor, status, "
            "responsável e data da última atualização, ordenadas do maior para o menor valor. "
            "Filtros opcionais por etapa do funil e por nome da empresa ou do contato."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "etapa": {
                    "type": "string",
                    "description": "Parte do nome da etapa, ex.: 'Negociação', 'Proposta'.",
                },
                "termo": {
                    "type": "string",
                    "description": "Parte do nome da empresa ou do contato.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "detalhes_oportunidade",
        "description": (
            "Detalhes de uma oportunidade específica, incluindo dados do contato e as anotações "
            "dos vendedores (histórico de contatos e próximo retorno). Use para resumir um lead "
            "antes de uma reunião."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "termo": {
                    "type": "string",
                    "description": "Nome (ou parte) da empresa ou do contato.",
                }
            },
            "required": ["termo"],
            "additionalProperties": False,
        },
    },
    {
        "name": "buscar_leads",
        "description": "Procura leads por nome ou e-mail e informa as oportunidades de cada um.",
        "input_schema": {
            "type": "object",
            "properties": {
                "termo": {"type": "string", "description": "Parte do nome ou do e-mail."}
            },
            "required": ["termo"],
            "additionalProperties": False,
        },
    },
    {
        "name": "consultar_anotacoes",
        "description": (
            "Anotações que os vendedores registraram sobre os clientes: ligações, e-mails, "
            "WhatsApp, reuniões, visitas e notas, com data, autor e próximo retorno combinado. "
            "Use para saber o que aconteceu com um cliente, o que foi feito num período ou o que "
            "um vendedor registrou. Mais recentes primeiro."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "termo": {
                    "type": "string",
                    "description": "Parte do nome do lead/empresa ou de um assunto citado na anotação.",
                },
                "dias": {
                    "type": "integer",
                    "description": "Considera só os últimos N dias (padrão 30, máximo 365).",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "agenda_retornos",
        "description": (
            "Próximos contatos (retornos) agendados nas anotações, incluindo os atrasados, em "
            "ordem de data. Use para perguntas como 'quem devo ligar hoje' ou 'o que tenho na "
            "agenda esta semana'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dias_a_frente": {
                    "type": "integer",
                    "description": "Até quantos dias a partir de hoje (padrão 7; 0 = só hoje e atrasados).",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "performance_vendedores",
        "description": (
            "Quantidade e valor somado de oportunidades por vendedor responsável. "
            "Usuários sem visão do time recebem apenas os próprios números."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def _oportunidade(op: Oportunidade, com_interacoes: bool = False) -> dict[str, Any]:
    dados: dict[str, Any] = {
        "empresa": op.nome,
        "contato": op.lead.nome if op.lead else None,
        "email_contato": op.lead.email if op.lead else None,
        "telefone_contato": op.lead.telefone if op.lead else None,
        "etapa": op.etapa_funil,
        "valor": float(op.valor) if op.valor is not None else None,
        "status": op.status,
        "responsavel": op.usuario.nome if op.usuario else None,
        "atualizado_em": op.updated_at.strftime("%d/%m/%Y") if op.updated_at else None,
    }
    if com_interacoes:
        dados["anotacoes"] = [_anotacao(i, com_cliente=False) for i in sorted(op.interacoes, key=lambda i: i.data, reverse=True)]
    return dados


def _anotacao(a: Interacao, com_cliente: bool = True) -> dict[str, Any]:
    dados: dict[str, Any] = {
        "data": a.data.strftime("%d/%m/%Y %H:%M") if a.data else None,
        "tipo": a.tipo,
        "texto": a.descricao,
        "autor": a.autor.nome if a.autor else None,
        "proximo_retorno": a.retorno_em.strftime("%d/%m/%Y") if a.retorno_em else None,
    }
    if com_cliente:
        dados["lead"] = a.lead.nome if a.lead else None
        dados["oportunidade"] = a.oportunidade.nome if a.oportunidade else None
    return dados


async def executar_ferramenta(
    nome: str, entrada: dict[str, Any], session: AsyncSession, usuario: Usuario
) -> str:
    """Executa a ferramenta pedida pelo modelo e devolve o resultado em JSON."""
    ops = OportunidadeRepository(session, usuario)

    if nome == "resumo_carteira":
        resultado: Any = await ops.metricas_resumo()
        etapas: dict[str, dict[str, float]] = {}
        for op in await ops.list_oportunidades():
            etapa = etapas.setdefault(op.etapa_funil or "Sem etapa", {"quantidade": 0, "valor": 0.0})
            etapa["quantidade"] += 1
            etapa["valor"] += float(op.valor or 0)
        resultado["por_etapa"] = etapas

    elif nome == "listar_oportunidades":
        encontradas = await ops.buscar(termo=entrada.get("termo"), etapa=entrada.get("etapa"))
        resultado = [_oportunidade(op) for op in encontradas]

    elif nome == "detalhes_oportunidade":
        encontradas = await ops.buscar(termo=entrada["termo"], limit=3)
        resultado = [_oportunidade(op, com_interacoes=True) for op in encontradas]

    elif nome == "buscar_leads":
        leads = await LeadRepository(session, usuario).buscar(entrada["termo"])
        resultado = [
            {
                "nome": lead.nome,
                "email": lead.email,
                "telefone": lead.telefone,
                "status": lead.status,
                "responsavel": lead.usuario.nome if lead.usuario else None,
                "oportunidades": [op.nome for op in lead.oportunidades],
            }
            for lead in leads
        ]

    elif nome == "consultar_anotacoes":
        dias = min(max(int(entrada.get("dias") or 30), 1), 365)
        anotacoes = await AnotacaoRepository(session, usuario).listar(
            termo=entrada.get("termo"), inicio=datetime.now(UTC) - timedelta(days=dias), limit=60
        )
        resultado = [_anotacao(a) for a in anotacoes]

    elif nome == "agenda_retornos":
        dias = min(max(int(entrada.get("dias_a_frente") or 7), 0), 90)
        anotacoes = await AnotacaoRepository(session, usuario).retornos(ate=date.today() + timedelta(days=dias))
        resultado = [_anotacao(a) for a in anotacoes]

    elif nome == "performance_vendedores":
        resultado = await ops.performance_por_vendedor()

    else:
        raise ValueError(f"Ferramenta desconhecida: {nome}")

    if resultado == []:
        return "Nenhum registro encontrado para essa consulta."
    return json.dumps(resultado, ensure_ascii=False, default=str)
