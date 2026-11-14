"""
Carga de anotações (agenda) para os leads e oportunidades da carga de demonstração.

Precisa da carga_demo já feita. Para cada oportunidade, monta o histórico de contatos de acordo
com a etapa em que ela está (primeiro contato, qualificação, visita, proposta, negociação, fechamento),
com o SDR nas primeiras etapas e o closer da proposta em diante. As oportunidades em aberto ganham
um próximo retorno: a maioria nos próximos dias úteis e algumas atrasadas, como numa agenda de verdade.

Os retornos são relativos à data em que a carga roda. Antes de uma apresentação, refaça:
    python -m scripts.carga_agenda --limpar && python -m scripts.carga_agenda

Uso (dentro de backend/, com o .venv ativo):
    python -m scripts.carga_agenda
    python -m scripts.carga_agenda --limpar   # remove TODAS as anotações dos leads/oportunidades da carga

O banco é o de POSTGRES_URL. Se não for local, é preciso repetir o comando com --confirmar.
"""

import argparse
import asyncio
import random
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db.database import async_session_factory, engine
from app.db.models import Interacao, Lead, Oportunidade
from scripts.carga_demo import BRT, HOSTS_LOCAIS, MARCA

# Passos do atendimento, por etapa alcançada: (etapa mínima, [(tipo, texto), ...]).
# Campos: {nome} primeiro nome do contato, {empresa}, {produto}, {valor}, {cidade}, {cargo}.
PASSOS = [
    (0, [
        ("ligacao", "Primeiro contato com {nome} ({cargo}). Tem obra em andamento em {cidade} e cotou {produto} com outros fornecedores."),
        ("whatsapp", "Mandei apresentação da empresa pelo WhatsApp para {nome}. Respondeu pedindo catálogo de {produto}."),
        ("email", "E-mail de apresentação enviado para {nome} com o catálogo de {produto}."),
        ("ligacao", "Liguei para a {empresa}, falei com {nome}. Pediu para retornar com condições de {produto}."),
    ]),
    (0, [
        ("ligacao", "Tentei falar com {nome} duas vezes, sem sucesso. Deixei recado com a recepção."),
        ("whatsapp", "{nome} confirmou recebimento do catálogo. Vai alinhar a demanda com o engenheiro da obra."),
        ("nota", "{empresa} tem duas obras previstas para o próximo semestre. Vale acompanhar de perto."),
    ]),
    (1, [
        ("reuniao", "Reunião de diagnóstico com {nome}: precisam de {produto} para a obra em {cidade}, início previsto em 30 dias."),
        ("ligacao", "Qualificação feita com {nome}. Orçamento aprovado pela diretoria, decisão até o fim do mês."),
        ("reuniao", "Call com {nome} e o engenheiro responsável. Levantamos quantidades e prazo de entrega de {produto}."),
    ]),
    (2, [
        ("visita", "Visita técnica à obra da {empresa} em {cidade}. Medições feitas para dimensionar {produto}."),
        ("visita", "Visitei o canteiro com {nome}. Espaço confirmado para {produto}; precisam de entrega em duas etapas."),
    ]),
    (2, [
        ("email", "Proposta enviada para {nome}: {produto}, {valor}, entrega em 15 dias e pagamento em 3 parcelas."),
        ("email", "Proposta comercial de {valor} enviada por e-mail. {nome} vai apresentar à diretoria."),
    ]),
    (3, [
        ("ligacao", "{nome} pediu desconto de 8% para fechar. Fiquei de verificar com a gerência."),
        ("reuniao", "Reunião de negociação com {nome}: concorrente ofereceu prazo menor. Ajustamos frete e condição de pagamento."),
        ("whatsapp", "{nome} pediu revisão da proposta com entrega parcelada. Enviei versão ajustada."),
    ]),
]
GANHO = [
    ("ligacao", "Pedido fechado com {nome}: {produto} por {valor}. Encaminhado para faturamento."),
    ("email", "Contrato assinado pela {empresa}. Entrega de {produto} agendada com a logística."),
    ("reuniao", "Fechamento com {nome}. {valor} aprovado, pedido de compra recebido."),
]
PERDA = {
    "Preço": "{nome} informou que fechou pelo menor preço. Nossa proposta ficou acima do orçamento.",
    "Fechou com concorrente": "{empresa} fechou {produto} com concorrente. {nome} elogiou o atendimento, manter contato.",
    "Obra adiada": "Obra em {cidade} foi adiada para o ano que vem. {nome} pediu para retomarmos depois.",
    "Sem orçamento": "{nome} avisou que a verba de {produto} foi cortada neste ano.",
    "Sem retorno do cliente": "Sem resposta de {nome} após várias tentativas. Oportunidade encerrada.",
    "Prazo de entrega": "{nome} precisava de {produto} em uma semana; não conseguimos atender o prazo.",
}
PROXIMO = [
    ("ligacao", "Ligar para {nome} para saber se a proposta foi aprovada."),
    ("whatsapp", "Retomar com {nome}: ficou de dar retorno sobre {produto}."),
    ("reuniao", "Agendar reunião com {nome} para apresentar condições de {produto}."),
]
PAUSADO = [
    ("nota", "{nome} pediu para retomar o assunto mais para frente; a obra ainda não tem data de início."),
    ("ligacao", "Contato com {nome}: projeto em compasso de espera. Combinamos de falar no próximo mês."),
]
POS_VENDA = [
    ("ligacao", "Pós-venda: confirmar com {nome} se a entrega de {produto} foi tranquila."),
]
SEM_OPORTUNIDADE = [
    ("ligacao", "Contato inicial com {nome} ({cargo}). Ainda sem obra definida; pediu para retornar."),
    ("email", "Enviei catálogo para {nome}. Lead chegou por {origem}, ainda sem demanda clara."),
    ("whatsapp", "{nome} respondeu que está avaliando fornecedores para obra em {cidade}."),
]


def _util(dia: date, passo: int) -> date:
    """Primeiro dia útil a partir de `dia`, andando para frente (+1) ou para trás (-1)."""
    while dia.weekday() >= 5:
        dia += timedelta(days=passo)
    return dia


def _somar_uteis(dia: date, n: int) -> date:
    passo = 1 if n >= 0 else -1
    for _ in range(abs(n)):
        dia = _util(dia + timedelta(days=passo), passo)
    return dia


def _momento(rng: random.Random, m: datetime, agora: datetime) -> datetime:
    """Dia útil em horário comercial, nunca depois de agora."""
    dia = _util(m.date(), -1)
    while True:
        r = datetime(dia.year, dia.month, dia.day, rng.choice([8, 9, 10, 10, 11, 14, 14, 15, 16, 17]),
                     rng.randint(0, 59), tzinfo=BRT)
        if r <= agora:
            return r
        dia = _util(dia - timedelta(days=1), -1)


def _etapa_indice(etapa: str | None) -> int:
    e = (etapa or "").lower()
    for chave, i in (("prospec", 0), ("qualif", 1), ("proposta", 2), ("negocia", 3)):
        if chave in e:
            return i
    return 1


def _campos(lead: Lead, op: Oportunidade | None) -> dict:
    extras = lead.dados_extras or {}
    valor = float(op.valor) if op is not None and op.valor is not None else None
    return {
        "nome": lead.nome.split()[0],
        "empresa": extras.get("empresa", lead.nome),
        "cidade": extras.get("cidade", "Belo Horizonte"),
        "cargo": (extras.get("cargo") or "responsável").lower(),
        "origem": extras.get("origem", "indicação").lower(),
        "produto": ((op.dados_extras or {}).get("produto", "equipamentos") if op is not None else "equipamentos").lower(),
        "valor": f"R$ {valor:,.0f}".replace(",", ".") if valor else "valor a definir",
    }


def _retorno_aberta(rng: random.Random, hoje: date) -> date:
    sorteio = rng.random()
    if sorteio < 0.18:  # atrasado
        return _somar_uteis(hoje, -rng.randint(1, 6))
    if sorteio < 0.30:  # hoje (ou o próximo dia útil)
        return _util(hoje, 1)
    return _somar_uteis(hoje, rng.choice([1, 1, 2, 2, 3, 3, 4, 5, 6, 8, 10, 15]))


def anotacoes_da_oportunidade(rng: random.Random, lead: Lead, op: Oportunidade, agora: datetime) -> list[Interacao]:
    hoje = agora.date()
    campos = _campos(lead, op)
    aberta = op.status in ("ongoing", "paused", "open", None)
    etapa = _etapa_indice(op.etapa_funil) if op.status != "won" else 3
    passos = [rng.choice(opcoes) + (minimo,) for minimo, opcoes in PASSOS if minimo <= etapa]
    if not aberta and op.status != "won" and len(passos) > 2 and rng.random() < 0.5:
        passos.pop(1)  # nem todo histórico é completo
    if op.status == "won":
        passos.append(rng.choice(GANHO) + (3,))
    elif op.status == "lost":
        motivo = (op.dados_extras or {}).get("motivo_perda", "Sem retorno do cliente")
        passos.append(("nota", PERDA.get(motivo, PERDA["Sem retorno do cliente"]), etapa))
    elif op.status == "paused":
        passos.append(rng.choice(PAUSADO) + (etapa,))

    inicio = op.created_at
    fim = op.updated_at if not aberta else agora - timedelta(days=rng.uniform(0, 6))
    fim = max(fim, inicio)
    datas = sorted(_momento(rng, inicio + (fim - inicio) * (i / max(len(passos) - 1, 1)) * rng.uniform(0.85, 1.0), agora)
                   for i in range(len(passos)))
    datas = sorted(max(d, inicio) for d in datas)

    # SDR (dono do lead) nas primeiras etapas; o responsável pela oportunidade da proposta em diante.
    sdr, dono = lead.usuario_id or op.usuario_id, op.usuario_id or lead.usuario_id
    notas = []
    for (tipo, texto, minimo), data in zip(passos, datas):
        notas.append(Interacao(lead_id=lead.id, oportunidade_id=op.id, usuario_id=sdr if minimo <= 1 else dono,
                               tipo=tipo, descricao=texto.format(**campos), data=data, created_at=data))

    ultima = notas[-1]
    if op.status == "ongoing" or op.status in ("open", None):
        if rng.random() < 0.85:
            tipo, texto = rng.choice(PROXIMO)
            proxima = Interacao(lead_id=lead.id, oportunidade_id=op.id, usuario_id=dono, tipo=tipo,
                                descricao=texto.format(**campos), data=ultima.data, created_at=ultima.data)
            proxima.data = _momento(rng, ultima.data + timedelta(hours=1), agora)
            proxima.created_at = proxima.data
            proxima.retorno_em = max(_retorno_aberta(rng, hoje), _util(proxima.data.date() + timedelta(days=1), 1))
            notas.append(proxima)
    elif op.status == "paused" and rng.random() < 0.6:
        ultima.retorno_em = _somar_uteis(hoje, rng.randint(10, 35))
    elif op.status == "won" and (hoje - op.updated_at.date()).days < 30 and rng.random() < 0.7:
        tipo, texto = POS_VENDA[0]
        notas.append(Interacao(lead_id=lead.id, oportunidade_id=op.id, usuario_id=dono, tipo=tipo,
                               descricao=texto.format(**campos), data=ultima.data, created_at=ultima.data,
                               retorno_em=_somar_uteis(max(op.updated_at.date(), hoje - timedelta(days=3)), rng.randint(3, 10))))
    return notas


def anotacoes_sem_oportunidade(rng: random.Random, lead: Lead, agora: datetime) -> list[Interacao]:
    campos = _campos(lead, None)
    tipo, texto = rng.choice(SEM_OPORTUNIDADE)
    data = _momento(rng, lead.created_at + timedelta(hours=rng.randint(1, 48)), agora)
    nota = Interacao(lead_id=lead.id, usuario_id=lead.usuario_id, tipo=tipo, descricao=texto.format(**campos),
                     data=max(data, min(lead.created_at, agora)), created_at=data)
    if (agora - lead.created_at).days < 60:
        nota.retorno_em = _retorno_aberta(rng, agora.date())
    return [nota]


def _filtro_carga():
    leads = select(Lead.id).where(Lead.dados_extras["carga"].as_string() == MARCA)
    ops = select(Oportunidade.id).where(Oportunidade.dados_extras["carga"].as_string() == MARCA)
    return or_(Interacao.lead_id.in_(leads), Interacao.oportunidade_id.in_(ops))


async def main() -> None:
    p = argparse.ArgumentParser(description="Anotações e retornos de agenda para a carga de demonstração.")
    p.add_argument("--semente", type=int, default=2026, help="semente do sorteio")
    p.add_argument("--limpar", action="store_true", help="remove as anotações dos registros da carga e sai")
    p.add_argument("--confirmar", action="store_true", help="obrigatório quando o banco não é local")
    args = p.parse_args()

    url = urlsplit(get_settings().postgres_url)
    print(f"Banco: {url.hostname}:{url.port or 5432}{url.path}")
    if url.hostname not in HOSTS_LOCAIS and not args.confirmar:
        raise SystemExit("Esse banco não é local. Se for mesmo o destino, repita o comando com --confirmar.")

    try:
        async with async_session_factory() as session:
            if args.limpar:
                n = (await session.execute(delete(Interacao).where(_filtro_carga()))).rowcount
                await session.commit()
                print(f"{n} anotações removidas.")
                return
            if (await session.execute(select(Interacao.id).where(_filtro_carga()).limit(1))).first():
                raise SystemExit("Os registros da carga já têm anotações. Para refazer, rode antes com --limpar.")

            leads = (
                await session.execute(
                    select(Lead)
                    .where(Lead.dados_extras["carga"].as_string() == MARCA)
                    .options(selectinload(Lead.oportunidades))
                    .order_by(Lead.created_at)
                )
            ).scalars().all()
            if not leads:
                raise SystemExit("Nenhum lead da carga de demonstração. Rode antes: python -m scripts.carga_demo")

            rng = random.Random(args.semente)
            agora = datetime.now(BRT)
            notas: list[Interacao] = []
            for lead in leads:
                ops = sorted(lead.oportunidades, key=lambda o: o.created_at)
                if not ops:
                    notas += anotacoes_sem_oportunidade(rng, lead, agora)
                for op in ops:
                    notas += anotacoes_da_oportunidade(rng, lead, op, agora)
            session.add_all(notas)
            await session.commit()

            hoje = agora.date()
            com_retorno = [n.retorno_em for n in notas if n.retorno_em]
            print(f"{len(notas)} anotações criadas para {len(leads)} leads, "
                  f"de {min(n.data for n in notas):%d/%m/%Y} a {max(n.data for n in notas):%d/%m/%Y}.")
            print(f"Retornos agendados: {len(com_retorno)} "
                  f"({sum(d < hoje for d in com_retorno)} atrasados, "
                  f"{sum(hoje <= d <= _somar_uteis(hoje, 5) for d in com_retorno)} nos próximos 5 dias úteis).")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
