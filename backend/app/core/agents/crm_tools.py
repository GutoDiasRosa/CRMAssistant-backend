"""
Ferramentas (tool use) que o assistente pode chamar para consultar o CRM.

Toda consulta passa pelos repositórios, que já aplicam o RF07: um SDR/Closer só
recebe a própria carteira, mesmo que peça dados de outra pessoa no chat.

Períodos: vendas ganhas e perdidas contam pela data de fechamento (fechada_em);
oportunidades em aberto e novos leads, pela data de criação (created_at).
"""

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import STATUS_FECHADO, Interacao, Oportunidade, Usuario
from app.domain.repositories.anotacoes import AnotacaoRepository
from app.domain.repositories.crm import LeadRepository, OportunidadeRepository

# Horário de Brasília: define "hoje" e o começo/fim dos dias informados pelo usuário.
BRT = timezone(timedelta(hours=-3))

_PERIODO = {
    "inicio": {
        "type": "string",
        "description": "Primeiro dia do período, no formato AAAA-MM-DD.",
    },
    "fim": {
        "type": "string",
        "description": "Último dia do período (incluído), no formato AAAA-MM-DD.",
    },
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "resumo_carteira",
        "description": (
            "Visão geral do funil visível ao usuário, separada por situação: oportunidades EM ABERTO "
            "(em andamento ou pausadas) com quantidade e valor por etapa, total de GANHAS e de PERDIDAS, "
            "e quantidade de leads. Sem período, é a foto de tudo o que existe. Com `inicio`/`fim`, "
            "considera as oportunidades e os leads criados no período e as ganhas/perdidas fechadas nele."
        ),
        "input_schema": {"type": "object", "properties": {**_PERIODO}, "additionalProperties": False},
    },
    {
        "name": "listar_oportunidades",
        "description": (
            "Lista oportunidades (negociações) com empresa, contato, etapa, valor, status, responsável, "
            "data de criação e de fechamento, ordenadas do maior para o menor valor. Filtros opcionais: "
            "etapa do funil, nome da empresa ou do contato, situação e período. O período vale pela data "
            "de fechamento quando a situação é 'ganhas' ou 'perdidas' e pela data de criação nos demais casos."
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
                "situacao": {
                    "type": "string",
                    "enum": ["abertas", "ganhas", "perdidas"],
                    "description": "abertas = em andamento ou pausadas. Sem valor, traz todas.",
                },
                **_PERIODO,
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "vendas_periodo",
        "description": (
            "Vendas fechadas num período, pela data de fechamento: total vendido, quantidade, ticket médio "
            "e a lista das vendas (empresa, valor, responsável, data). Com situacao='perdidas', traz as "
            "oportunidades perdidas no período e o motivo, quando registrado. Use para 'quanto vendemos em "
            "outubro', 'o que fechamos esta semana', 'por que perdemos negócios'. Sem período informado "
            "pelo usuário, não envie datas: a ferramenta usa o mês atual."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                **_PERIODO,
                "situacao": {
                    "type": "string",
                    "enum": ["ganhas", "perdidas"],
                    "description": "Padrão: ganhas.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "detalhes_oportunidade",
        "description": (
            "Detalhes de uma oportunidade específica, incluindo dados do contato e as anotações "
            "dos vendedores (histórico de contatos e próximo retorno). Use para resumir uma "
            "negociação antes de uma reunião. Se a pergunta trouxer o ID da oportunidade, "
            "informe o `id` (identifica o registro exato); senão, procure pelo `termo`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "ID (UUID) da oportunidade, quando informado na pergunta.",
                },
                "termo": {
                    "type": "string",
                    "description": "Nome (ou parte) da empresa ou do contato.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "detalhes_lead",
        "description": (
            "Ficha completa de um lead (contato): dados, responsável, todas as oportunidades "
            "dele e as anotações dos vendedores sobre ele e sobre essas oportunidades. Use para "
            "resumir um lead. Se a pergunta trouxer o ID do lead, informe o `id`; senão, o `termo`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "ID (UUID) do lead, quando informado na pergunta.",
                },
                "termo": {
                    "type": "string",
                    "description": "Parte do nome ou do e-mail do lead.",
                },
            },
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
            "Próximos contatos (retornos) agendados nas anotações, incluindo os atrasados, em ordem de "
            "data. Vem em dois grupos: `meus_retornos` (agendados pelo próprio usuário: é a agenda dele) "
            "e `agendados_por_colegas` (retornos que outros vendedores marcaram em clientes que o usuário "
            "acompanha; para gerência, os do time). Em 'quem devo ligar hoje' ou 'minha agenda', responda "
            "com `meus_retornos` e cite os dos colegas à parte, dizendo de quem são."
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
            "Desempenho por vendedor num período, com os mesmos números da tela Performance do time. "
            "Por data de criação: `prospectados` e `valor_prospectado` (oportunidades que entraram), "
            "`qualificados` e `convertidos` (dessas, as já ganhas). Por data de fechamento: `fechados` e "
            "`valor_fechado` (vendas ganhas), `perdidos` e `valor_perdido`. Ao comparar vendedores ou "
            "apontar o melhor, analise SEMPRE das duas formas: por valor e por quantidade, e diga quando "
            "os dois critérios dão resultados diferentes. Sem período informado pelo usuário, não envie "
            "datas: a ferramenta usa o mês atual. Usuários sem visão do time recebem só os próprios números."
        ),
        "input_schema": {"type": "object", "properties": {**_PERIODO}, "additionalProperties": False},
    },
]


def hoje_brt() -> date:
    return datetime.now(BRT).date()


def _dia(valor: Any, campo: str) -> date | None:
    if valor in (None, ""):
        return None
    try:
        return date.fromisoformat(str(valor).strip()[:10])
    except ValueError as e:
        raise ValueError(f"Data inválida em {campo}: {valor}. Use o formato AAAA-MM-DD.") from e


def _periodo(entrada: dict[str, Any], *, padrao_mes_atual: bool) -> tuple[datetime | None, datetime | None, str]:
    """(inicio, fim exclusivo, descrição) a partir de `inicio`/`fim` em AAAA-MM-DD (fim incluído)."""
    de, ate = _dia(entrada.get("inicio"), "inicio"), _dia(entrada.get("fim"), "fim")
    if de and ate and de > ate:
        raise ValueError("O início do período é depois do fim.")
    padrao = ""
    if de is None and ate is None:
        if not padrao_mes_atual:
            return None, None, "tudo (sem filtro de período)"
        de, ate = hoje_brt().replace(day=1), hoje_brt()
        padrao = " (mês atual, usado porque nenhum período foi informado)"
    inicio = datetime(de.year, de.month, de.day, tzinfo=BRT) if de else None
    fim = datetime(ate.year, ate.month, ate.day, tzinfo=BRT) + timedelta(days=1) if ate else None
    texto = f"de {de:%d/%m/%Y}" if de else "desde o início"
    texto += f" a {ate:%d/%m/%Y}" if ate else " até hoje"
    return inicio, fim, texto + padrao


def _uuid(valor: str) -> UUID:
    try:
        return UUID(str(valor).strip())
    except ValueError as e:
        raise ValueError(f"ID inválido: {valor}") from e


def _data(d: datetime | None, formato: str = "%d/%m/%Y") -> str | None:
    return d.astimezone(BRT).strftime(formato) if d else None


def _oportunidade(op: Oportunidade, com_interacoes: bool = False) -> dict[str, Any]:
    dados: dict[str, Any] = {
        "id": str(op.id),
        "empresa": op.nome,
        "contato": op.lead.nome if op.lead else None,
        "email_contato": op.lead.email if op.lead else None,
        "telefone_contato": op.lead.telefone if op.lead else None,
        "etapa": op.etapa_funil,
        "valor": float(op.valor) if op.valor is not None else None,
        "status": op.status,
        "responsavel": op.usuario.nome if op.usuario else None,
        "criada_em": _data(op.created_at),
        "fechada_em": _data(op.fechada_em),
        "atualizado_em": _data(op.updated_at),
    }
    if com_interacoes:
        dados["anotacoes"] = [
            _anotacao(i, com_cliente=False) for i in sorted(op.interacoes, key=lambda i: i.data, reverse=True)
        ]
    return dados


def _anotacao(a: Interacao, com_cliente: bool = True) -> dict[str, Any]:
    dados: dict[str, Any] = {
        "data": _data(a.data, "%d/%m/%Y %H:%M"),
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
        inicio, fim, periodo = _periodo(entrada, padrao_mes_atual=False)
        m = await ops.metricas_resumo(inicio=inicio, fim=fim)
        etapas: dict[str, dict[str, float]] = {}
        for op in await ops.list_oportunidades(inicio=inicio, fim=fim):
            if (op.status or "").lower() in STATUS_FECHADO:
                continue
            etapa = etapas.setdefault(op.etapa_funil or "Sem etapa", {"quantidade": 0, "valor": 0.0})
            etapa["quantidade"] += 1
            etapa["valor"] += float(op.valor or 0)
        resultado: Any = {
            "periodo": periodo,
            "leads": m["total_leads"],
            "oportunidades_em_aberto": {**m["abertas"], "por_etapa": etapas},
            "oportunidades_ganhas": m["ganhas"],
            "oportunidades_perdidas": m["perdidas"],
        }

    elif nome == "listar_oportunidades":
        inicio, fim, _ = _periodo(entrada, padrao_mes_atual=False)
        encontradas = await ops.buscar(
            termo=entrada.get("termo"),
            etapa=entrada.get("etapa"),
            situacao=entrada.get("situacao"),
            inicio=inicio,
            fim=fim,
        )
        resultado = [_oportunidade(op) for op in encontradas]

    elif nome == "vendas_periodo":
        inicio, fim, periodo = _periodo(entrada, padrao_mes_atual=True)
        situacao = "perdidas" if entrada.get("situacao") == "perdidas" else "ganhas"
        fechadas = await ops.buscar(situacao=situacao, inicio=inicio, fim=fim, limit=300)
        total = sum(float(op.valor or 0) for op in fechadas)
        resultado = {
            "periodo": periodo,
            "situacao": situacao,
            "quantidade": len(fechadas),
            "valor_total": total,
            "ticket_medio": round(total / len(fechadas), 2) if fechadas else 0,
            "oportunidades": [
                {
                    "empresa": op.nome,
                    "contato": op.lead.nome if op.lead else None,
                    "valor": float(op.valor) if op.valor is not None else None,
                    "responsavel": op.usuario.nome if op.usuario else None,
                    "fechada_em": _data(op.fechada_em),
                    **(
                        {"motivo_perda": (op.dados_extras or {}).get("motivo_perda")}
                        if situacao == "perdidas"
                        else {}
                    ),
                }
                for op in sorted(fechadas, key=lambda o: o.fechada_em or o.updated_at, reverse=True)
            ],
        }

    elif nome == "detalhes_oportunidade":
        if entrada.get("id"):
            op = await ops.obter(_uuid(entrada["id"]))
            resultado = [_oportunidade(op, com_interacoes=True)] if op else []
        elif entrada.get("termo"):
            encontradas = await ops.buscar(termo=entrada["termo"], limit=3)
            resultado = [_oportunidade(op, com_interacoes=True) for op in encontradas]
        else:
            raise ValueError("Informe o id ou o termo da oportunidade")

    elif nome == "detalhes_lead":
        leads_repo = LeadRepository(session, usuario)
        if entrada.get("id"):
            lead = await leads_repo.obter(_uuid(entrada["id"]))
            leads = [lead] if lead else []
        elif entrada.get("termo"):
            encontrados = await leads_repo.buscar(entrada["termo"], limit=3)
            leads = [lead for lead in [await leads_repo.obter(e.id) for e in encontrados] if lead]
        else:
            raise ValueError("Informe o id ou o termo do lead")
        anotacoes_repo = AnotacaoRepository(session, usuario)
        resultado = []
        for lead in leads:
            anotacoes = await anotacoes_repo.listar(lead_id=lead.id, limit=60)
            resultado.append(
                {
                    "id": str(lead.id),
                    "nome": lead.nome,
                    "email": lead.email,
                    "telefone": lead.telefone,
                    "status": lead.status,
                    "responsavel": lead.usuario.nome if lead.usuario else None,
                    "cadastrado_em": _data(lead.created_at),
                    "oportunidades": [
                        {
                            "id": str(op.id),
                            "empresa": op.nome,
                            "etapa": op.etapa_funil,
                            "valor": float(op.valor) if op.valor is not None else None,
                            "status": op.status,
                            "responsavel": op.usuario.nome if op.usuario else None,
                            "criada_em": _data(op.created_at),
                            "fechada_em": _data(op.fechada_em),
                        }
                        for op in sorted(lead.oportunidades, key=lambda o: o.created_at, reverse=True)
                    ],
                    "anotacoes": [_anotacao(a) for a in anotacoes],
                }
            )

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
            termo=entrada.get("termo"), inicio=datetime.now(BRT) - timedelta(days=dias), limit=60
        )
        resultado = [_anotacao(a) for a in anotacoes]

    elif nome == "agenda_retornos":
        valor = entrada.get("dias_a_frente")
        dias = min(max(int(7 if valor is None else valor), 0), 90)
        anotacoes = await AnotacaoRepository(session, usuario).retornos(ate=hoje_brt() + timedelta(days=dias))
        resultado = {
            "hoje": hoje_brt().strftime("%d/%m/%Y"),
            "meus_retornos": [_anotacao(a) for a in anotacoes if a.usuario_id == usuario.id],
            "agendados_por_colegas": [_anotacao(a) for a in anotacoes if a.usuario_id != usuario.id],
        }

    elif nome == "performance_vendedores":
        inicio, fim, periodo = _periodo(entrada, padrao_mes_atual=True)
        desempenho = await ops.performance_time(inicio=inicio, fim=fim)
        resultado = {
            "periodo": periodo,
            "vendedores": [{k: v for k, v in linha.items() if k != "id"} for linha in desempenho["vendedores"]],
            "totais": desempenho["totais"],
        }

    else:
        raise ValueError(f"Ferramenta desconhecida: {nome}")

    if resultado == []:
        return "Nenhum registro encontrado para essa consulta."
    return json.dumps(resultado, ensure_ascii=False, default=str)
