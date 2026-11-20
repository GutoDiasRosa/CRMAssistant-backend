"""Classificação de intenção por palavras-chave, sem IA.

Registra a intenção de cada pergunta e, principalmente, guia o modo sem IA (sem
ANTHROPIC_API_KEY), que responde às perguntas diretas com as mesmas ferramentas do chat
(app/core/agents/modo_sem_ia.py). As regras valem na ordem em que aparecem: a primeira que casar
decide. A comparação é por palavra inteira, sem acento e sem diferenciar maiúsculas, para que
"foi" ou "depois" não sejam confundidos com o cumprimento "oi".
"""

import re
import unicodedata

# (intenção, padrão), na ordem de prioridade. O texto chega normalizado (ver normalizar).
# Sem regra que case: "cumprimento" (se começar com um) ou "fora_de_escopo".
_REGRAS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # "Resuma a oportunidade X (ID ...)": a pergunta que o botão "Pedir resumo ao assistente" monta
    ("resumo_oportunidade", re.compile(r"\b(resum\w*|detalh\w*)( \w+){0,2}? (oportunidade|negocio|negociacao)\b")),
    # "Resuma o lead X (ID ...)", "ficha do cliente X"
    ("resumo_lead", re.compile(r"\b(resum\w*|detalh\w*|ficha)( \w+){0,2}? (lead|contato|cliente)\b")),
    ("agenda", re.compile(r"\b(agenda|retornos?|ligar|ligacao|ligacoes|atrasad[oa]s?|follow up|lembretes?)\b")),
    ("perdas_periodo", re.compile(r"\b(perd\w*|motivos?)\b")),
    (
        "performance_time",
        re.compile(
            r"\b(vendedor(es|a|as)?|equipe|time|desempenho|performance|ranking|melhor(es)?|pior(es)?|metas?)\b"
            r"|\bcomo (eu )?(estou|to|vou) indo\b"
            r"|\bquem( mais)? (vend|fech|ganh)\w*"
        ),
    ),
    ("vendas_periodo", re.compile(r"\b(vend\w*|ganh\w*|fech\w*|fatur\w*|receita|ticket)\b")),
    (
        "consulta_funil",
        re.compile(
            r"\b(funil|kanban|etapas?|pipeline|oportunidades?|negocios?|negociac\w*|carteira|em aberto"
            r"|indicador(es)?|metricas?|graficos?|relatorios?|resumo|numeros)\b"
        ),
    ),
    ("consulta_leads", re.compile(r"\b(leads?|contatos?|clientes?)\b")),
)

# Só vale quando nenhuma regra acima casou: "Oi, quanto vendemos?" é uma pergunta de vendas.
_CUMPRIMENTO = re.compile(r"^(oi|ola|ei|hey|opa|bom dia|boa tarde|boa noite|e ai|tudo bem|obrigad[oa]|valeu)\b")


def normalizar(texto: str) -> str:
    """Minúsculas, sem acento e só as palavras, separadas por um espaço: "Negócios?" -> "negocios"."""
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return " ".join(re.findall(r"\w+", sem_acento.lower()))


def classify_intent(mensagem: str) -> str:
    texto = normalizar(mensagem)
    for intencao, padrao in _REGRAS:
        if padrao.search(texto):
            return intencao
    if _CUMPRIMENTO.search(texto):
        return "cumprimento"
    return "fora_de_escopo"
