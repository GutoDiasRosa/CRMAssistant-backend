"""Respostas do assistente sem IA, quando ANTHROPIC_API_KEY não está configurada.

A pergunta é classificada por palavras-chave (intent_router_crm) e respondida com as mesmas
ferramentas que o Claude usa (crm_tools), que já aplicam o RF07. As respostas têm formato fixo e
cobrem as perguntas diretas: vendas e perdas num período, desempenho do time, funil, leads, agenda
de retornos e o resumo de um lead ou de uma oportunidade. Perguntas livres exigem a chave.
"""

import json
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import Any

from app.core.agents.crm_tools import hoje_brt
from app.core.agents.intent_router_crm import normalizar

# Executa uma ferramenta de crm_tools (nome, entrada) e devolve o resultado em JSON.
Executor = Callable[[str, dict[str, Any]], Awaitable[str]]

PREFIXO = "(Modo sem IA) "
MAX_ITENS = 5
MAX_RETORNOS = 8

MESES = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6,
    "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}
STATUS = {"ongoing": "em andamento", "paused": "pausada", "won": "ganha", "lost": "perdida"}
TIPOS = {
    "nota": "nota", "ligacao": "ligação", "email": "e-mail", "whatsapp": "WhatsApp",
    "reuniao": "reunião", "visita": "visita",
}
EXEMPLOS = (
    '"Quanto vendemos em outubro?", "Por que perdemos negócios este ano?", '
    '"Quem é o vendedor com melhor desempenho?", "Como está o funil?" e "O que tenho na agenda esta semana?"'
)

_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)


# ---- Período citado na pergunta ----


def periodo_do_texto(texto: str, hoje: date, *, aceitar_hoje: bool = True) -> tuple[date, date] | None:
    """Primeiro e último dia (inclusive) do período citado na pergunta, ou None se não houver.

    `aceitar_hoje=False` ignora "hoje"/"ontem": em "como está o funil hoje?" o usuário quer a foto
    atual, não as oportunidades criadas hoje.
    """
    t = normalizar(texto)
    if m := re.search(r"\bultim[oa]s (\d{1,4}) dias\b", t):
        n = min(max(int(m.group(1)), 1), 3660)
        return hoje - timedelta(days=n - 1), hoje
    if m := re.search(r"\b(" + "|".join(MESES) + r")\b( de)?( (20\d\d))?", t):
        mes = MESES[m.group(1)]
        # Sem ano, vale o mês mais recente: em novembro, "dezembro" é o do ano passado.
        ano = int(m.group(4)) if m.group(4) else (hoje.year if mes <= hoje.month else hoje.year - 1)
        proximo = date(ano + 1, 1, 1) if mes == 12 else date(ano, mes + 1, 1)
        return date(ano, mes, 1), proximo - timedelta(days=1)
    segunda = hoje - timedelta(days=hoje.weekday())
    primeiro_do_mes = hoje.replace(day=1)
    if re.search(r"\b(semana passada|ultima semana)\b", t):
        return segunda - timedelta(days=7), segunda - timedelta(days=1)
    if re.search(r"\b(est|ess|nest|ness)a semana\b|\bsemana atual\b", t):
        return segunda, hoje
    if re.search(r"\b(mes passado|ultimo mes)\b", t):
        fim = primeiro_do_mes - timedelta(days=1)
        return fim.replace(day=1), fim
    if re.search(r"\b(est|ess|nest|ness)e mes\b|\b(do|no) mes\b|\bmes atual\b", t):
        return primeiro_do_mes, hoje
    if re.search(r"\b(ano passado|ultimo ano)\b", t):
        return date(hoje.year - 1, 1, 1), date(hoje.year - 1, 12, 31)
    if re.search(r"\b(est|ess|nest|ness)e ano\b|\b(do|no) ano\b|\bano atual\b", t):
        return date(hoje.year, 1, 1), hoje
    if (m := re.search(r"\b(em|de) (20\d\d)\b", t)) and int(m.group(2)) <= hoje.year:
        ano = int(m.group(2))
        return date(ano, 1, 1), min(date(ano, 12, 31), hoje)
    if aceitar_hoje and re.search(r"\bhoje\b", t):
        return hoje, hoje
    if aceitar_hoje and re.search(r"\bontem\b", t):
        return hoje - timedelta(days=1), hoje - timedelta(days=1)
    return None


def _entrada(periodo: tuple[date, date] | None) -> dict[str, str]:
    return {"inicio": periodo[0].isoformat(), "fim": periodo[1].isoformat()} if periodo else {}


def _dias_agenda(t: str, hoje: date) -> int:
    """Até quantos dias à frente olhar a agenda (0 = hoje e atrasados)."""
    if re.search(r"\bhoje\b", t):
        return 0
    if re.search(r"\bamanha\b", t):
        return 1
    if re.search(r"\bproxima semana\b", t):
        return 13 - hoje.weekday()  # até o domingo da semana que vem
    if re.search(r"\bsemana\b", t):
        return 6 - hoje.weekday()  # até domingo
    if re.search(r"\bmes\b", t):
        return 30
    return 7


# ---- Formatação ----


def _moeda(valor: Any) -> str:
    return "R$ " + f"{float(valor or 0):,.0f}".replace(",", ".")


def _valor(valor: Any) -> str:
    """Valor de uma oportunidade; "sem valor" quando não foi informado."""
    return "sem valor" if valor is None else _moeda(valor)


def _qtd(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def _curto(texto: str, limite: int) -> str:
    texto = " ".join(texto.split())
    return texto if len(texto) <= limite else texto[: limite - 1].rstrip() + "…"


def _dia(texto_br: str) -> date:
    return datetime.strptime(texto_br, "%d/%m/%Y").date()


def _situacao(status: Any) -> str:
    return STATUS.get(str(status or "").lower(), str(status or "sem status"))


def _historico(anotacoes: list[dict[str, Any]]) -> list[str]:
    if not anotacoes:
        return ["Nenhuma anotação registrada."]
    linhas = ["Últimos contatos:"]
    linhas += [
        f"- {a['data'][:10]}, {TIPOS.get(a['tipo'], a['tipo'])} ({a.get('autor') or 'sem autor'}): "
        f"{_curto(a.get('texto') or '', 90)}"
        for a in anotacoes[:3]
    ]
    # As anotações vêm da mais recente para a mais antiga: vale o retorno combinado por último.
    if proximo := next((a["proximo_retorno"] for a in anotacoes if a.get("proximo_retorno")), None):
        linhas.append(f"Próximo retorno combinado: {proximo}.")
    return linhas


def _texto_lead(lead: dict[str, Any]) -> str:
    linhas = [f"Lead {lead['nome']}, responsável: {lead.get('responsavel') or 'não definido'}."]
    if contato := ", ".join(x for x in (lead.get("email"), lead.get("telefone")) if x):
        linhas.append(f"Contato: {contato}.")
    ops = lead.get("oportunidades") or []
    if ops:
        linhas.append(f"Oportunidades ({len(ops)}):")
        linhas += [
            f"- {o['empresa']}: {_situacao(o.get('status'))}, etapa {o.get('etapa') or 'não definida'}, "
            f"{_valor(o.get('valor'))}"
            for o in ops[:MAX_ITENS]
        ]
    else:
        linhas.append("Nenhuma oportunidade.")
    return "\n".join(linhas + _historico(lead.get("anotacoes") or []))


def _texto_oportunidade(op: dict[str, Any]) -> str:
    linhas = [
        f"Oportunidade {op['empresa']}: {_situacao(op.get('status'))}, etapa {op.get('etapa') or 'não definida'}, "
        f"{_valor(op.get('valor'))}, responsável: {op.get('responsavel') or 'não definido'}."
    ]
    if op.get("contato"):
        meios = ", ".join(x for x in (op.get("email_contato"), op.get("telefone_contato")) if x)
        linhas.append(f"Contato: {op['contato']}" + (f" ({meios})" if meios else "") + ".")
    datas = f"Criada em {op.get('criada_em')}"
    if op.get("fechada_em"):
        datas += f", fechada em {op['fechada_em']}"
    linhas.append(datas + ".")
    return "\n".join(linhas + _historico(op.get("anotacoes") or []))


def _termo(mensagem: str, entidade: str) -> str | None:
    """Nome citado depois de "lead"/"oportunidade" (ou entre aspas), para buscar sem o ID."""
    if m := re.search(r'"([^"]{2,80})"', mensagem):
        return m.group(1).strip()
    if m := re.search(rf"\b(?:{entidade})\s+(?:d[oa]s?\s+)?([^(:?,.!]{{2,80}})", mensagem, re.IGNORECASE):
        return m.group(1).strip() or None
    return None


# ---- Respostas por intenção ----


async def _consultar(executar: Executor, ferramenta: str, entrada: dict[str, Any]) -> Any:
    """Resultado da ferramenta já decodificado; None quando ela não encontra nada."""
    bruto = await executar(ferramenta, entrada)
    try:
        return json.loads(bruto)
    except ValueError:
        return None  # "Nenhum registro encontrado para essa consulta."


async def _resumo(mensagem: str, intencao: str, executar: Executor) -> str:
    uuid = _UUID.search(mensagem)
    if intencao == "resumo_oportunidade":
        ferramenta, nome, texto = "detalhes_oportunidade", "essa oportunidade", _texto_oportunidade
        termo = None if uuid else _termo(mensagem, r"oportunidade|neg[oó]cio|negocia[cç][aã]o")
    else:
        ferramenta, nome, texto = "detalhes_lead", "esse lead", _texto_lead
        termo = None if uuid else _termo(mensagem, r"lead|contato|cliente")
    if not uuid and not termo:
        return PREFIXO + f"Diga o nome de {nome} ou abra o registro e use \"Pedir resumo ao assistente\"."
    encontrados = await _consultar(executar, ferramenta, {"id": uuid.group(0)} if uuid else {"termo": termo})
    if not encontrados:
        return PREFIXO + f"Não encontrei {nome} entre os registros que você pode ver."
    resposta = texto(encontrados[0])
    if len(encontrados) > 1:
        resposta += f"\nHá mais {len(encontrados) - 1} com nome parecido; use o botão do registro para escolher."
    return PREFIXO + resposta


async def _vendas(mensagem: str, executar: Executor, hoje: date) -> str:
    r = await _consultar(executar, "vendas_periodo", _entrada(periodo_do_texto(mensagem, hoje)))
    if not r["quantidade"]:
        return PREFIXO + f"Nenhuma venda ganha {r['periodo']}."
    linhas = [
        f"Vendas ganhas {r['periodo']}: {_qtd(r['quantidade'], 'venda', 'vendas')}, "
        f"total de {_moeda(r['valor_total'])}, ticket médio de {_moeda(r['ticket_medio'])}.",
        "Maiores vendas:",
    ]
    maiores = sorted(r["oportunidades"], key=lambda o: o.get("valor") or 0, reverse=True)[:MAX_ITENS]
    linhas += [
        f"- {o['empresa']}: {_moeda(o.get('valor'))} ({o.get('responsavel') or 'sem responsável'}, "
        f"{o.get('fechada_em') or 'sem data'})"
        for o in maiores
    ]
    return PREFIXO + "\n".join(linhas)


async def _perdas(mensagem: str, executar: Executor, hoje: date) -> str:
    entrada = {"situacao": "perdidas", **_entrada(periodo_do_texto(mensagem, hoje))}
    r = await _consultar(executar, "vendas_periodo", entrada)
    if not r["quantidade"]:
        return PREFIXO + f"Nenhuma oportunidade perdida {r['periodo']}."
    motivos = Counter(o.get("motivo_perda") or "sem motivo registrado" for o in r["oportunidades"])
    linhas = [
        f"Oportunidades perdidas {r['periodo']}: {r['quantidade']}, somando {_moeda(r['valor_total'])}.",
        "Motivos registrados:",
    ]
    linhas += [f"- {motivo}: {n}" for motivo, n in motivos.most_common(MAX_ITENS)]
    return PREFIXO + "\n".join(linhas)


async def _performance(mensagem: str, executar: Executor, hoje: date, ve_todo_time: bool) -> str:
    r = await _consultar(executar, "performance_vendedores", _entrada(periodo_do_texto(mensagem, hoje)))
    vendedores = r["vendedores"]
    if not vendedores:
        return PREFIXO + f"Sem números de desempenho {r['periodo']}."
    por_valor = sorted(vendedores, key=lambda v: (v["valor_fechado"], v["fechados"]), reverse=True)
    titulo = "Desempenho do time" if ve_todo_time else "Seus números"
    linhas = [f"{titulo} {r['periodo']}, do maior para o menor valor fechado:"]
    linhas += [
        f"- {v['nome']}: {_qtd(v['fechados'], 'venda', 'vendas')} ({_moeda(v['valor_fechado'])}), "
        f"{_qtd(v['prospectados'], 'oportunidade nova', 'oportunidades novas')}, "
        f"{_qtd(v['perdidos'], 'perdida', 'perdidas')}"
        for v in por_valor
    ]
    if len(vendedores) > 1 and por_valor[0]["fechados"]:
        mais_vendas = max(vendedores, key=lambda v: (v["fechados"], v["valor_fechado"]))
        if mais_vendas is por_valor[0]:
            linhas.append(f"Destaque: {mais_vendas['nome']}, com o maior valor fechado e o maior número de vendas.")
        else:
            linhas.append(
                f"Por valor, o destaque é {por_valor[0]['nome']}; por quantidade de vendas, {mais_vendas['nome']}."
            )
    return PREFIXO + "\n".join(linhas)


async def _funil(mensagem: str, executar: Executor, hoje: date, ve_todo_time: bool) -> str:
    periodo = periodo_do_texto(mensagem, hoje, aceitar_hoje=False)
    r = await _consultar(executar, "resumo_carteira", _entrada(periodo))
    abertas = r["oportunidades_em_aberto"]
    titulo = (
        f"Funil {r['periodo']} (criadas no período; ganhas e perdidas pela data de fechamento)"
        if periodo
        else "Funil atual"
    )
    linhas = [
        f"{titulo}: {_qtd(abertas['quantidade'], 'oportunidade em aberto', 'oportunidades em aberto')}, "
        f"somando {_moeda(abertas['valor'])}."
    ]
    etapas = sorted(abertas["por_etapa"].items(), key=lambda e: e[1]["quantidade"], reverse=True)
    linhas += [f"- {etapa}: {dados['quantidade']} ({_moeda(dados['valor'])})" for etapa, dados in etapas]
    ganhas, perdidas = r["oportunidades_ganhas"], r["oportunidades_perdidas"]
    linhas.append(
        f"Ganhas: {ganhas['quantidade']} ({_moeda(ganhas['valor'])}). "
        f"Perdidas: {perdidas['quantidade']} ({_moeda(perdidas['valor'])}). Leads: {r['leads']}."
    )
    if not ve_todo_time:
        linhas.append("Você vê apenas a sua carteira.")
    return PREFIXO + "\n".join(linhas)


async def _leads(mensagem: str, executar: Executor, hoje: date, ve_todo_time: bool) -> str:
    periodo = periodo_do_texto(mensagem, hoje, aceitar_hoje=False)
    r = await _consultar(executar, "resumo_carteira", _entrada(periodo))
    quando = f" cadastrados {r['periodo']}" if periodo else ""
    onde = "na base" if ve_todo_time else "na sua carteira"
    return PREFIXO + (
        f"Há {_qtd(r['leads'], 'lead', 'leads')}{quando} {onde}. Para o resumo de um lead, "
        'abra-o na tela Leads e use "Pedir resumo ao assistente".'
    )


def _linha_retorno(a: dict[str, Any], hoje: date, com_autor: bool) -> str:
    atrasado = " (atrasado)" if _dia(a["proximo_retorno"]) < hoje else ""
    cliente = a.get("lead") or a.get("oportunidade") or "cliente sem nome"
    autor = f", de {a['autor']}" if com_autor and a.get("autor") else ""
    return (
        f"- {a['proximo_retorno']}{atrasado}: {cliente} ({TIPOS.get(a['tipo'], a['tipo'])}{autor}): "
        f"{_curto(a.get('texto') or '', 70)}"
    )


async def _agenda(mensagem: str, executar: Executor, hoje: date, ve_todo_time: bool) -> str:
    t = normalizar(mensagem)
    dias = _dias_agenda(t, hoje)
    r = await _consultar(executar, "agenda_retornos", {"dias_a_frente": dias})
    # Para a gerência, a agenda que interessa é a do time; para os vendedores, a própria.
    retornos = r["meus_retornos"] + r["agendados_por_colegas"] if ve_todo_time else list(r["meus_retornos"])
    so_atrasados = bool(re.search(r"\batrasad", t))
    if so_atrasados:
        retornos = [a for a in retornos if _dia(a["proximo_retorno"]) < hoje]
    retornos.sort(key=lambda a: _dia(a["proximo_retorno"]))
    quais = "atrasados" if so_atrasados else f"até {(hoje + timedelta(days=dias)).strftime('%d/%m/%Y')}"
    titulo = f"Retornos do time {quais}" if ve_todo_time else f"Seus retornos {quais}"
    if not retornos:
        linhas = [f"{titulo}: nenhum."]
    else:
        atrasados = sum(1 for a in retornos if _dia(a["proximo_retorno"]) < hoje)
        extra = f", {atrasados} atrasados" if atrasados and not so_atrasados else ""
        linhas = [f"{titulo}: {len(retornos)}{extra}."]
        linhas += [_linha_retorno(a, hoje, ve_todo_time) for a in retornos[:MAX_RETORNOS]]
        if len(retornos) > MAX_RETORNOS:
            linhas.append(f"- e mais {len(retornos) - MAX_RETORNOS}.")
    if not ve_todo_time and r["agendados_por_colegas"]:
        linhas.append(f"Colegas marcaram {len(r['agendados_por_colegas'])} retornos em clientes que você acompanha.")
    return PREFIXO + "\n".join(linhas)


async def responder_sem_ia(
    mensagem: str,
    intencao: str,
    executar: Executor,
    *,
    nome_usuario: str = "",
    ve_todo_time: bool = True,
    hoje: date | None = None,
) -> str:
    """Resposta em formato fixo para a pergunta já classificada (ver intent_router_crm)."""
    hoje = hoje or hoje_brt()
    try:
        if intencao in ("resumo_lead", "resumo_oportunidade"):
            return await _resumo(mensagem, intencao, executar)
        if intencao == "vendas_periodo":
            return await _vendas(mensagem, executar, hoje)
        if intencao == "perdas_periodo":
            return await _perdas(mensagem, executar, hoje)
        if intencao == "performance_time":
            return await _performance(mensagem, executar, hoje, ve_todo_time)
        if intencao == "consulta_funil":
            return await _funil(mensagem, executar, hoje, ve_todo_time)
        if intencao == "consulta_leads":
            return await _leads(mensagem, executar, hoje, ve_todo_time)
        if intencao == "agenda":
            return await _agenda(mensagem, executar, hoje, ve_todo_time)
    except ValueError as e:
        return PREFIXO + f"Não consegui responder: {e}"
    if intencao == "cumprimento":
        primeiro = nome_usuario.split()[0] if nome_usuario.strip() else ""
        saudacao = f"Olá, {primeiro}!" if primeiro else "Olá!"
        return PREFIXO + f"{saudacao} Sem a chave de IA, respondo a perguntas diretas como {EXEMPLOS}."
    return PREFIXO + (
        "Sem a chave de IA, só entendo perguntas diretas sobre vendas, perdas, desempenho do time, funil, "
        f"agenda e resumo de lead ou oportunidade. Exemplos: {EXEMPLOS}. "
        "Para perguntas livres, configure ANTHROPIC_API_KEY no backend."
    )
