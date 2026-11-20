"""Modo sem IA: classificação das perguntas, período citado e respostas montadas com as ferramentas."""

import json
from datetime import date

import pytest

from app.core.agents.intent_router_crm import classify_intent
from app.core.agents.modo_sem_ia import periodo_do_texto, responder_sem_ia

HOJE = date(2026, 11, 20)  # uma sexta-feira
ID = "1b4e28ba-2fa1-11d2-883f-0016d3cca427"


@pytest.mark.parametrize(
    ("pergunta", "intencao"),
    [
        # Perguntas do roteiro do Guia da banca
        ("Quanto vendemos em outubro?", "vendas_periodo"),
        ("Quem é o vendedor com melhor desempenho este ano?", "performance_time"),
        ("Por que perdemos negócios este ano?", "perdas_periodo"),
        ("Quais são as oportunidades da Tatiane Rocha?", "consulta_funil"),
        # "foi" e "depois" contêm "oi": antes viravam cumprimento
        ("Qual vendedor foi o melhor do mês?", "performance_time"),
        ("Quais oportunidades fechamos depois do dia 10?", "vendas_periodo"),
        ("Como está o funil hoje?", "consulta_funil"),
        ("O que tenho na agenda esta semana?", "agenda"),
        ("Quem devo ligar hoje?", "agenda"),
        ("Como estou indo este mês?", "performance_time"),
        (f'Resuma a oportunidade "Construtora Alfa" (ID {ID}): situação atual e próximos passos.', "resumo_oportunidade"),
        (f"Resuma o lead Paulo Machado (ID {ID}): oportunidades, histórico de contatos e próximos passos.", "resumo_lead"),
        ("Oi, quanto vendemos este mês?", "vendas_periodo"),
        ("Bom dia!", "cumprimento"),
        ("Qual a previsão do tempo amanhã?", "fora_de_escopo"),
    ],
)
def test_classifica_por_palavra_inteira(pergunta, intencao):
    assert classify_intent(pergunta) == intencao


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("Quanto vendemos em outubro?", (date(2026, 10, 1), date(2026, 10, 31))),
        ("E em dezembro?", (date(2025, 12, 1), date(2025, 12, 31))),  # mês ainda não chegou: o do ano passado
        ("vendas de março de 2026", (date(2026, 3, 1), date(2026, 3, 31))),
        ("este ano", (date(2026, 1, 1), HOJE)),
        ("Qual vendedor foi o melhor do mês?", (date(2026, 11, 1), HOJE)),
        ("no mês passado", (date(2026, 10, 1), date(2026, 10, 31))),
        ("nos últimos 30 dias", (date(2026, 10, 22), HOJE)),
        ("esta semana", (date(2026, 11, 16), HOJE)),
        ("semana passada", (date(2026, 11, 9), date(2026, 11, 15))),
        ("ano passado", (date(2025, 1, 1), date(2025, 12, 31))),
        ("sem período", None),
    ],
)
def test_periodo_citado_na_pergunta(texto, esperado):
    assert periodo_do_texto(texto, HOJE) == esperado


def test_hoje_nao_vira_periodo_quando_a_pergunta_e_sobre_o_funil_atual():
    assert periodo_do_texto("Como está o funil hoje?", HOJE, aceitar_hoje=False) is None


class FerramentasFalsas:
    """Registra as chamadas e devolve respostas prontas, no formato de executar_ferramenta."""

    def __init__(self, respostas: dict[str, object]):
        self.respostas = respostas
        self.chamadas: list[tuple[str, dict]] = []

    async def __call__(self, nome: str, entrada: dict) -> str:
        self.chamadas.append((nome, entrada))
        return json.dumps(self.respostas[nome])


def _vendedor(nome: str, fechados: int, valor_fechado: float) -> dict:
    return {
        "nome": nome, "perfil": "CLOSER", "prospectados": 4, "valor_prospectado": 0.0, "qualificados": 2,
        "convertidos": 1, "fechados": fechados, "valor_fechado": valor_fechado, "perdidos": 1, "valor_perdido": 0.0,
    }


async def test_vendas_do_mes_citado():
    ferramentas = FerramentasFalsas({"vendas_periodo": {
        "periodo": "de 01/10/2026 a 31/10/2026", "situacao": "ganhas", "quantidade": 2,
        "valor_total": 150000.0, "ticket_medio": 75000.0,
        "oportunidades": [
            {"empresa": "Construtora Alfa", "valor": 50000.0, "responsavel": "Tatiane Rocha", "fechada_em": "10/10/2026"},
            {"empresa": "Obras Beta", "valor": 100000.0, "responsavel": "Marcos", "fechada_em": "20/10/2026"},
        ],
    }})
    resposta = await responder_sem_ia("Quanto vendemos em outubro?", "vendas_periodo", ferramentas, hoje=HOJE)
    assert ferramentas.chamadas == [("vendas_periodo", {"inicio": "2026-10-01", "fim": "2026-10-31"})]
    assert "2 vendas, total de R$ 150.000" in resposta
    assert resposta.index("Obras Beta") < resposta.index("Construtora Alfa")  # maior valor primeiro


async def test_perdas_agrupadas_por_motivo():
    ferramentas = FerramentasFalsas({"vendas_periodo": {
        "periodo": "de 01/01/2026 a 20/11/2026", "situacao": "perdidas", "quantidade": 3,
        "valor_total": 90000.0, "ticket_medio": 30000.0,
        "oportunidades": [
            {"empresa": "A", "valor": 30000.0, "motivo_perda": "Preço"},
            {"empresa": "B", "valor": 30000.0, "motivo_perda": "Preço"},
            {"empresa": "C", "valor": 30000.0, "motivo_perda": None},
        ],
    }})
    resposta = await responder_sem_ia("Por que perdemos negócios este ano?", "perdas_periodo", ferramentas, hoje=HOJE)
    assert ferramentas.chamadas == [
        ("vendas_periodo", {"situacao": "perdidas", "inicio": "2026-01-01", "fim": "2026-11-20"})
    ]
    assert "- Preço: 2" in resposta and "- sem motivo registrado: 1" in resposta


async def test_desempenho_compara_por_valor_e_por_quantidade():
    ferramentas = FerramentasFalsas({"performance_vendedores": {
        "periodo": "de 01/01/2026 a 20/11/2026",
        "vendedores": [_vendedor("Tatiane Rocha", 3, 90000.0), _vendedor("Marcos", 5, 60000.0)],
        "totais": {},
    }})
    resposta = await responder_sem_ia(
        "Quem é o vendedor com melhor desempenho este ano?", "performance_time", ferramentas, hoje=HOJE
    )
    assert "Por valor, o destaque é Tatiane Rocha; por quantidade de vendas, Marcos." in resposta


async def test_resumo_usa_o_id_enviado_pelo_botao():
    ferramentas = FerramentasFalsas({"detalhes_lead": [{
        "id": ID, "nome": "Paulo Machado", "email": "paulo@exemplo.com", "telefone": None, "status": None,
        "responsavel": "Carlos", "cadastrado_em": "01/02/2026", "oportunidades": [],
        "anotacoes": [{
            "data": "18/11/2026 10:00", "tipo": "ligacao", "texto": "Pediu proposta.", "autor": "Carlos",
            "proximo_retorno": "25/11/2026", "lead": "Paulo Machado", "oportunidade": None,
        }],
    }]})
    pergunta = f"Resuma o lead Paulo Machado (ID {ID}): oportunidades, histórico de contatos e próximos passos."
    resposta = await responder_sem_ia(pergunta, "resumo_lead", ferramentas, hoje=HOJE)
    assert ferramentas.chamadas == [("detalhes_lead", {"id": ID})]
    assert "Lead Paulo Machado" in resposta and "Próximo retorno combinado: 25/11/2026." in resposta


async def test_pergunta_livre_explica_o_modo_sem_ia_sem_consultar_dados():
    ferramentas = FerramentasFalsas({})
    resposta = await responder_sem_ia("Qual a previsão do tempo amanhã?", "fora_de_escopo", ferramentas, hoje=HOJE)
    assert ferramentas.chamadas == []
    assert "ANTHROPIC_API_KEY" in resposta
