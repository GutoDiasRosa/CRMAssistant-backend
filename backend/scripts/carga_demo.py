"""
Carga de dados de demonstração: leads e oportunidades espalhados ao longo de 2026.

Para carregar ou remover tudo de uma vez (equipe, leads, oportunidades e agenda), use
scripts/dados_demo.py. Este módulo cuida só da equipe, dos leads e das oportunidades.

A carga grava direto no banco para poder definir as datas: os registros nascem em dias úteis
e horário comercial, de 1º de janeiro até agora, com mais volume nos meses recentes.
Oportunidades antigas já aparecem ganhas ou perdidas (com data de fechamento); as recentes
ainda estão nas primeiras etapas.

Os registros NÃO vão para o RD Station (ficam sem rd_lead_id / rd_deal_id, como qualquer
registro criado com a integração desconectada) e levam dados_extras["carga"] = "demo-2026",
o que permite removê-los depois com --limpar.

Uso (dentro de backend/, com o .venv ativo):
    python -m scripts.carga_demo                      # ~140 leads, usa os vendedores ativos
    python -m scripts.carga_demo --criar-vendedores   # cria também a equipe de demonstração
    python -m scripts.carga_demo --leads 80 --etapas "Sem contato,Contato feito,Apresentação,Proposta enviada"
    python -m scripts.carga_demo --limpar             # remove a carga (os usuários ficam)

O banco é o de POSTGRES_URL. Se não for local, é preciso repetir o comando com --confirmar.
"""

import argparse
import asyncio
import random
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import delete, select, text

from app.config import get_settings
from app.core.rd_station_sync.upsert import log_sync
from app.db.database import async_session_factory, engine
from app.db.models import Lead, Oportunidade, Usuario
from app.security import hash_password

MARCA = "demo-2026"
BRT = timezone(timedelta(hours=-3))
INICIO = datetime(2026, 1, 1, tzinfo=BRT)
ETAPAS_PADRAO = "Prospecção,Qualificado,Proposta enviada,Negociação"
HOSTS_LOCAIS = {"localhost", "127.0.0.1", "::1", "db"}

# Equipe de demonstração. Senha só para ambientes de teste: está publicada no README.
SENHA_DEMO = "demo1234"
EQUIPE_DEMO = [
    ("gerente@crmassist.dev", "Rafael Andrade", "GERENTE"),  # vê toda a equipe
    ("sdr@crmassist.dev", "Carlos Eduardo Mendes", "SDR"),  # vê só a própria carteira
    ("sdr2@crmassist.dev", "Juliana Pires", "SDR"),
    ("closer@crmassist.dev", "Tatiane Rocha", "CLOSER"),
    ("closer2@crmassist.dev", "Marcos Vinícius Teixeira", "CLOSER"),
]

NOMES = [
    "Ana Paula", "Bruno", "Camila", "Daniel", "Eduarda", "Felipe", "Gabriela", "Henrique", "Isabela",
    "João Pedro", "Karina", "Leonardo", "Mariana", "Nathalia", "Otávio", "Patrícia", "Rafael", "Sabrina",
    "Thiago", "Vanessa", "Wagner", "Yasmin", "André", "Bianca", "Caio", "Débora", "Emerson", "Fernanda",
    "Gustavo", "Helena", "Igor", "Jéssica", "Luciano", "Michele", "Renato", "Simone", "Vinícius", "Cláudio",
    "Rodrigo", "Letícia", "Sérgio", "Aline", "Paulo Henrique", "Cristiane", "Marcelo", "Tânia",
]
SOBRENOMES = [
    "Almeida", "Barbosa", "Cardoso", "Duarte", "Esteves", "Ferreira", "Gomes", "Henriques", "Lacerda",
    "Machado", "Nogueira", "Oliveira", "Pereira", "Queiroz", "Ribeiro", "Souza", "Tavares", "Vieira",
    "Xavier", "Araújo", "Batista", "Carvalho", "Diniz", "Figueiredo", "Guimarães", "Lopes", "Moreira",
    "Nascimento", "Pacheco", "Rezende", "Siqueira", "Teixeira", "Vasconcelos", "Braga", "Campos", "Fonseca",
]
TIPOS_EMPRESA = [
    "Construtora", "Construtora", "Construtora", "Engenharia", "Incorporadora", "Empreiteira",
    "Construções", "Locadora", "Reformas",
]
NOMES_EMPRESA = [
    "Alvorada", "Serra Azul", "Pampulha", "Mantiqueira", "Novo Tempo", "Capital", "Ouro Branco", "Planalto",
    "Vale Verde", "Litoral", "Pedra Forte", "Horizonte Novo", "Três Rios", "Santa Clara", "Belvedere",
    "Aroeira", "Ipê Amarelo", "Monte Sião", "Rio Doce", "Cidade Nova", "Lagoa Santa", "Vila Rica",
    "Primavera", "Atlântica", "Cerrado", "Triângulo", "Jatobá", "Boa Vista", "Paraíso", "União",
    "Solidez", "Prumo", "Alicerce", "Viga Mestra", "Concreta", "Esquadro", "Nível Certo", "Obra Prima",
    "Sete Colinas", "Mirante", "Bandeirantes", "Itacolomi", "Canastra", "Araucária", "Guanabara",
]
# (cidade, UF, DDD, peso): carteira concentrada em MG, com vizinhos.
CIDADES = [
    ("Belo Horizonte", "MG", "31", 9), ("Contagem", "MG", "31", 4), ("Betim", "MG", "31", 3),
    ("Nova Lima", "MG", "31", 2), ("Uberlândia", "MG", "34", 3), ("Juiz de Fora", "MG", "32", 2),
    ("Montes Claros", "MG", "38", 1), ("Divinópolis", "MG", "37", 1), ("São Paulo", "SP", "11", 4),
    ("Campinas", "SP", "19", 2), ("Rio de Janeiro", "RJ", "21", 2), ("Vitória", "ES", "27", 1),
    ("Goiânia", "GO", "62", 1), ("Brasília", "DF", "61", 1),
]
CARGOS = [
    "Comprador", "Compradora", "Gerente de Suprimentos", "Engenheiro de Obras", "Engenheira Civil",
    "Diretor de Obras", "Sócio-proprietário", "Coordenadora de Compras", "Mestre de Obras", "Analista de Suprimentos",
]
ORIGENS = [
    ("Indicação", 5), ("Site", 5), ("Google Ads", 4), ("Prospecção ativa", 6), ("Feira Construir Minas", 2),
    ("Instagram", 2), ("WhatsApp", 3), ("Cliente antigo", 2),
]
# (produto, valor mínimo, valor máximo, peso)
PRODUTOS = [
    ("Andaimes fachadeiros", 15_000, 90_000, 6), ("Escoramento metálico", 20_000, 150_000, 5),
    ("Betoneiras 400L", 4_500, 28_000, 5), ("Formas metálicas para pilares", 18_000, 110_000, 3),
    ("Guincho de coluna", 6_000, 32_000, 3), ("Elevador cremalheira", 120_000, 380_000, 1),
    ("Mini grua", 80_000, 260_000, 1), ("Compactadores de solo", 8_000, 45_000, 4),
    ("Vibradores de concreto", 3_000, 18_000, 4), ("Geradores a diesel", 25_000, 140_000, 2),
    ("Plataforma elevatória", 60_000, 220_000, 2), ("Ferramentas elétricas", 3_500, 22_000, 4),
    ("Cortadora de piso", 5_000, 26_000, 2), ("Locação de andaimes (12 meses)", 12_000, 70_000, 3),
    ("Bomba de concreto rebocável", 90_000, 240_000, 1),
]
MOTIVOS_PERDA = [
    "Preço", "Fechou com concorrente", "Obra adiada", "Sem orçamento", "Sem retorno do cliente", "Prazo de entrega",
]


def _slug(s: str) -> str:
    ascii_ = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return "".join(c for c in ascii_.lower() if c.isalnum())


def _peso(rng: random.Random, itens: list[tuple]):
    return rng.choices(itens, weights=[i[-1] for i in itens])[0]


def _momento(rng: random.Random, agora: datetime) -> datetime:
    """Dia útil (raramente sábado) em horário comercial, com volume crescendo ao longo do ano."""
    total = (agora - INICIO).days
    while True:
        dia = INICIO + timedelta(days=rng.randint(0, total))
        peso = 0.55 + 0.45 * (dia - INICIO).days / max(total, 1)
        peso *= {5: 0.08, 6: 0.0}.get(dia.weekday(), 1.0)
        if rng.random() >= peso:
            continue
        m = dia.replace(hour=rng.choice([8, 9, 9, 10, 10, 11, 13, 14, 14, 15, 16, 17]), minute=rng.randint(0, 59),
                        second=rng.randint(0, 59))
        if m < agora:
            return m


def _horario_comercial(rng: random.Random, m: datetime, agora: datetime) -> datetime:
    """Empurra para um dia útil em horário comercial, sem passar de agora."""
    while m.weekday() >= 5:
        m += timedelta(days=1)
    m = m.replace(hour=rng.randint(8, 17), minute=rng.randint(0, 59), second=rng.randint(0, 59))
    return min(m, agora - timedelta(minutes=rng.randint(5, 240)))


def gerar(rng: random.Random, n_leads: int, etapas: list[str], vendedores: list[Usuario], agora: datetime):
    """Monta os Leads e Oportunidades (ainda fora da sessão)."""
    sdrs = [u for u in vendedores if u.perfil == "SDR"] or vendedores
    closers = [u for u in vendedores if u.perfil == "CLOSER"] or vendedores
    # Cada vendedor tem um ritmo e uma taxa de fechamento próprios (a performance não fica igual).
    ritmo = {u.id: rng.uniform(0.75, 1.3) for u in vendedores}
    talento = {u.id: rng.uniform(0.75, 1.3) for u in vendedores}
    ultima = len(etapas) - 1

    def escolher(grupo: list[Usuario]) -> Usuario:
        return rng.choices(grupo, weights=[ritmo[u.id] for u in grupo])[0]

    leads: list[Lead] = []
    ops: list[Oportunidade] = []
    empresas_usadas: set[str] = set()
    emails_usados: set[str] = set()

    while len(leads) < n_leads:
        empresa = f"{rng.choice(TIPOS_EMPRESA)} {rng.choice(NOMES_EMPRESA)}"
        if empresa in empresas_usadas:
            continue
        nome, sobrenome = rng.choice(NOMES), rng.choice(SOBRENOMES)
        email = f"{_slug(nome.split()[0])}.{_slug(sobrenome)}@{_slug(empresa)}.com.br"
        if email in emails_usados:
            continue
        empresas_usadas.add(empresa)
        emails_usados.add(email)
        cidade, uf, ddd, _ = _peso(rng, CIDADES)
        criado = _momento(rng, agora)
        sdr = escolher(sdrs)
        lead = Lead(
            usuario_id=sdr.id,
            nome=f"{nome} {sobrenome}",
            email=email,
            telefone=f"({ddd}) 9{rng.randint(7000, 9999)}-{rng.randint(0, 9999):04d}",
            status="Lead",
            dados_extras={
                "carga": MARCA,
                "empresa": empresa,
                "cargo": rng.choice(CARGOS),
                "cidade": cidade,
                "uf": uf,
                "origem": _peso(rng, ORIGENS)[0],
            },
            created_at=criado,
            updated_at=criado,
        )
        leads.append(lead)

        # 12% ainda sem oportunidade; 73% com uma; 15% voltam a comprar meses depois.
        sorteio = rng.random()
        n_ops = 0 if sorteio < 0.12 else (1 if sorteio < 0.85 else 2)
        op_criada = criado
        for i in range(n_ops):
            if i == 0:
                op_criada = _horario_comercial(rng, criado + timedelta(days=rng.choice([0, 0, 0, 1, 2, 3, 5, 8])), agora)
                op_criada = max(op_criada, criado)
            else:
                op_criada = op_criada + timedelta(days=rng.randint(35, 150))
                if op_criada >= agora - timedelta(days=1):
                    break
                op_criada = _horario_comercial(rng, op_criada, agora)
            produto, vmin, vmax, _ = _peso(rng, PRODUTOS)
            valor = round(rng.triangular(vmin, vmax, vmin + (vmax - vmin) * 0.3) / 500) * 500
            idade = (agora - op_criada).total_seconds() / 86400
            ciclo = rng.triangular(12, 85, 35) * (1.4 if valor > 100_000 else 1.0)
            closer = escolher(closers)
            p_ganho = min(0.5, 0.31 * talento[closer.id])
            destino = rng.choices(["won", "lost", "aberta"], weights=[p_ganho, 0.42, 0.58 - p_ganho])[0]
            extras = {"carga": MARCA, "produto": produto}

            if destino != "aberta" and idade >= ciclo:
                status = destino
                atualizado = _horario_comercial(rng, op_criada + timedelta(days=ciclo), agora)
                if status == "won":
                    etapa_i = ultima
                else:
                    etapa_i = rng.choices(range(len(etapas)), weights=[len(etapas) - j for j in range(len(etapas))])[0]
                    extras["motivo_perda"] = rng.choice(MOTIVOS_PERDA)
            else:
                progresso = min(1.0, idade / ciclo) * rng.uniform(0.5, 1.0)
                etapa_i = min(ultima, int(progresso * len(etapas)))
                # Negócio aberto há muito tempo costuma estar parado.
                status = "paused" if idade > 120 and rng.random() < 0.55 else "ongoing"
                atualizado = op_criada + timedelta(days=rng.uniform(0, min(idade, max(ciclo, 20))))
                if status == "ongoing" and idade > 20:
                    atualizado = agora - timedelta(days=rng.uniform(0, 18))
                atualizado = _horario_comercial(rng, atualizado, agora)
            atualizado = max(atualizado, op_criada)

            # O SDR prospecta e qualifica; da proposta em diante (ou com a venda fechada) o closer assume.
            dono = closer if (etapa_i >= max(1, ultima - 1) or status == "won") else sdr
            op = Oportunidade(
                lead=lead,
                usuario_id=dono.id,
                nome=f"{empresa} - {produto}",
                etapa_funil=etapas[etapa_i],
                valor=valor,
                status=status,
                dados_extras=extras,
                created_at=op_criada,
                updated_at=atualizado,
            )
            if status in ("won", "lost"):
                op.fechada_em = atualizado  # depois do status: o modelo marcaria a data de agora
            ops.append(op)
            lead.updated_at = max(lead.updated_at, atualizado)
            if status == "won":
                lead.status = "Cliente"
    return leads, ops


async def _garantir_vendedores(session) -> None:
    for email, nome, perfil in EQUIPE_DEMO:
        existe = (await session.execute(select(Usuario.id).where(Usuario.email == email))).scalar_one_or_none()
        if existe is None:
            session.add(Usuario(email=email, nome=nome, perfil=perfil, hashed_password=hash_password(SENHA_DEMO)))
            print(f"Usuário criado: {email} ({perfil}), senha {SENHA_DEMO}")
    await session.flush()


async def limpar(session) -> tuple[int, int]:
    marca_op = Oportunidade.dados_extras["carga"].as_string() == MARCA
    marca_lead = Lead.dados_extras["carga"].as_string() == MARCA
    ids_ops = select(Oportunidade.id).where(marca_op)
    ids_leads = select(Lead.id).where(marca_lead)
    # Anotações feitas em cima da carga (a coluna lead_id só existe a partir da migração 007).
    await session.execute(text("DELETE FROM interacao WHERE oportunidade_id IN ("
                               "SELECT id FROM oportunidade WHERE dados_extras->>'carga' = :m)"), {"m": MARCA})
    tem_lead_id = (
        await session.execute(
            text("SELECT 1 FROM information_schema.columns WHERE table_name = 'interacao' AND column_name = 'lead_id'")
        )
    ).first()
    if tem_lead_id:
        await session.execute(
            text("DELETE FROM interacao WHERE lead_id IN (SELECT id FROM leads WHERE dados_extras->>'carga' = :m)"),
            {"m": MARCA},
        )
    # Oportunidades criadas depois, pela tela, em leads da carga: ficam, mas sem o contato.
    await session.execute(
        Oportunidade.__table__.update().where(Oportunidade.lead_id.in_(ids_leads), ~Oportunidade.id.in_(ids_ops)).values(lead_id=None)
    )
    n_ops = (await session.execute(delete(Oportunidade).where(marca_op))).rowcount
    n_leads = (await session.execute(delete(Lead).where(marca_lead))).rowcount
    return n_leads, n_ops


async def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Carga de leads e oportunidades de demonstração ao longo de 2026.")
    p.add_argument("--leads", type=int, default=140, help="quantidade de leads (padrão: 140)")
    p.add_argument("--etapas", default=ETAPAS_PADRAO, help="etapas do funil, em ordem, separadas por vírgula")
    p.add_argument("--semente", type=int, default=2026, help="semente do sorteio (mesma semente = mesmos dados)")
    p.add_argument("--criar-vendedores", action="store_true", help="cria o time de demonstração (SDRs e closers)")
    p.add_argument("--limpar", action="store_true", help="remove a carga e sai")
    p.add_argument("--confirmar", action="store_true", help="obrigatório quando o banco não é local")
    args = p.parse_args(argv)

    etapas = [e.strip() for e in args.etapas.split(",") if e.strip()]
    if len(etapas) < 2:
        p.error("informe pelo menos duas etapas")

    url = urlsplit(get_settings().postgres_url)
    print(f"Banco: {url.hostname}:{url.port or 5432}{url.path}")
    if url.hostname not in HOSTS_LOCAIS and not args.confirmar:
        raise SystemExit("Esse banco não é local. Se for mesmo o destino, repita o comando com --confirmar.")

    try:
        async with async_session_factory() as session:
            if args.limpar:
                n_leads, n_ops = await limpar(session)
                await session.commit()
                print(f"Carga removida: {n_leads} leads e {n_ops} oportunidades.")
                return

            ja_existe = (
                await session.execute(select(Lead.id).where(Lead.dados_extras["carga"].as_string() == MARCA).limit(1))
            ).first()
            if ja_existe:
                raise SystemExit(
                    "A carga já está neste banco. Para refazer, remova antes: "
                    "python -m scripts.dados_demo limpar (ou carga_demo --limpar)."
                )

            if args.criar_vendedores:
                await _garantir_vendedores(session)
            ativos = (
                (await session.execute(select(Usuario).where(Usuario.ativo.is_(True)).order_by(Usuario.email)))
                .scalars()
                .all()
            )
            vendedores = [u for u in ativos if u.perfil in ("SDR", "CLOSER")] or list(ativos)
            if not vendedores:
                raise SystemExit("Nenhum usuário ativo no banco. Rode com --criar-vendedores.")
            if not any(u.perfil in ("SDR", "CLOSER") for u in vendedores):
                print("Aviso: não há SDR/CLOSER ativos; tudo ficará com os usuários existentes "
                      "(use --criar-vendedores para um time de demonstração).")

            agora = datetime.now(BRT)
            leads, ops = gerar(random.Random(args.semente), args.leads, etapas, vendedores, agora)
            session.add_all(leads)
            session.add_all(ops)
            await log_sync(
                session,
                fonte="carga_demo",
                event_type="carga_demonstracao",
                rd_entity_id=None,
                status="success",
                detalhe={"leads": len(leads), "oportunidades": len(ops)},
            )
            await session.commit()

            por_status: dict[str, int] = {}
            for op in ops:
                por_status[op.status] = por_status.get(op.status, 0) + 1
            print(f"{len(leads)} leads e {len(ops)} oportunidades criados, de "
                  f"{min(l.created_at for l in leads):%d/%m/%Y} a {max(o.created_at for o in ops):%d/%m/%Y}.")
            print("Oportunidades por status:", ", ".join(f"{k}={v}" for k, v in sorted(por_status.items())))
            print("Responsáveis:", ", ".join(u.nome for u in vendedores))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
