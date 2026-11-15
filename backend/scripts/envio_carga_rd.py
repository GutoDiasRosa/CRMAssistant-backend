"""
Envia ao RD Station CRM os leads e oportunidades da carga de demonstração (carga_demo).

Cada oportunidade vira uma negociação no RD, com o contato (lead) dela, o valor, a etapa do funil e o
status (ganha, perdida, pausada ou em andamento). Leads sem oportunidade vão só como contato.
As anotações (agenda) não são enviadas, e no RD tudo aparece criado na data do envio.

Usa o mesmo código das telas (envio.enviar_oportunidade / enviar_lead), então precisa do RD Station
conectado NESTE banco (tela Integração RD). Os ids do RD ficam gravados nos registros, por isso o
script pode ser repetido: o que já foi enviado é pulado e um envio interrompido continua de onde parou.

As etapas locais são ligadas às do funil do RD pelo --mapa, depois pelo nome; as que sobrarem vão
para a etapa na mesma posição do funil, sem voltar para antes de uma etapa já ligada. Cada oportunidade
passa a ter o nome da etapa do RD também no CRM Assist (é o que o webhook do RD devolveria).

Uso (dentro de backend/, com o .venv ativo):
    python -m scripts.envio_carga_rd --simular      # mostra o que seria enviado, sem enviar
    python -m scripts.envio_carga_rd --limite 3     # envia só 3 oportunidades, para conferir no RD
    python -m scripts.envio_carga_rd                # envia o restante
    python -m scripts.envio_carga_rd --funil "Funil Padrão"
    python -m scripts.envio_carga_rd --mapa "Prospecção=Sem contato;Qualificado=Contato feito"

O banco é o de POSTGRES_URL. Se não for local, é preciso repetir o comando com --confirmar.
"""

import argparse
import asyncio
import unicodedata
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.core.rd_station_sync import envio, integracao
from app.core.rd_station_sync.integracao import RdStationErro
from app.db.database import async_session_factory, engine
from app.db.models import Lead, Oportunidade
from scripts.carga_demo import HOSTS_LOCAIS, MARCA

PAUSA = 0.5  # segundos entre envios (cada um faz 2 a 4 chamadas à API do RD)
ESPERA_LIMITE = 20  # segundos de espera quando o RD limita as requisições


def _chave(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return " ".join(s.lower().split())


def mapear_etapas(locais: list[str], etapas_rd: list[dict], escolhidas: dict[str, str] | None = None) -> dict[str, dict]:
    """Etapa local -> etapa do RD (`locais` em ordem de funil).

    Vale primeiro o `escolhidas` (nome local -> nome no RD), depois o mesmo nome; o resto vai para
    a posição equivalente no funil do RD, sem voltar para antes da etapa anterior já ligada.
    """
    por_nome = {_chave(e["nome"]): i for i, e in enumerate(etapas_rd)}
    escolhidas = {_chave(k): _chave(v) for k, v in (escolhidas or {}).items()}
    for local, rd in escolhidas.items():
        if rd not in por_nome:
            raise SystemExit(f"Etapa '{rd}' do --mapa não existe no funil do RD: {[e['nome'] for e in etapas_rd]}")
    ultima = len(etapas_rd) - 1
    mapa, anterior = {}, 0
    for i, nome in enumerate(locais):
        indice = por_nome.get(escolhidas.get(_chave(nome), _chave(nome)))
        if indice is None:
            proporcional = round(i * ultima / max(len(locais) - 1, 1))
            indice = max(proporcional, anterior)
        mapa[nome] = etapas_rd[indice]
        anterior = indice
    return mapa


def ler_mapa(texto: str | None) -> dict[str, str]:
    """"Prospecção=Sem contato;Qualificado=Contato feito" -> {"Prospecção": "Sem contato", ...}."""
    pares = {}
    for item in (texto or "").split(";"):
        if item.strip():
            local, _, rd = item.partition("=")
            if not rd.strip():
                raise SystemExit(f"Item do --mapa sem '=': {item}")
            pares[local.strip()] = rd.strip()
    return pares


def _ordem_local(nome: str) -> int:
    n = _chave(nome)
    for i, chave in enumerate(("prospec", "qualif", "proposta", "negocia")):
        if chave in n:
            return i
    return 99


async def _com_repeticao(session, enviar, entidade_id) -> None:
    for tentativa in range(1, 5):
        try:
            await enviar(session, entidade_id)
            return
        except RdStationErro as e:
            await session.rollback()
            if "limitou" not in str(e) or tentativa == 4:
                raise
            print(f"  RD limitou as chamadas; aguardando {ESPERA_LIMITE * tentativa}s...")
            await asyncio.sleep(ESPERA_LIMITE * tentativa)


async def main() -> None:
    p = argparse.ArgumentParser(description="Envia a carga de demonstração ao RD Station CRM.")
    p.add_argument("--simular", action="store_true", help="mostra o que seria enviado, sem enviar")
    p.add_argument("--limite", type=int, default=None, help="envia no máximo N oportunidades")
    p.add_argument("--funil", default=None, help="nome do funil do RD (padrão: o primeiro)")
    p.add_argument("--mapa", default=None, help='ligação de etapas: "Local=Etapa no RD;Outra=Etapa no RD"')
    p.add_argument("--confirmar", action="store_true", help="obrigatório quando o banco não é local")
    args = p.parse_args()

    url = urlsplit(get_settings().postgres_url)
    print(f"Banco: {url.hostname}:{url.port or 5432}{url.path}")
    if url.hostname not in HOSTS_LOCAIS and not args.confirmar:
        raise SystemExit("Esse banco não é local. Se for mesmo o destino, repita o comando com --confirmar.")

    try:
        async with async_session_factory() as session:
            ops = (
                await session.execute(
                    select(Oportunidade)
                    .where(Oportunidade.dados_extras["carga"].as_string() == MARCA, Oportunidade.rd_deal_id.is_(None))
                    .options(selectinload(Oportunidade.lead))
                    .order_by(Oportunidade.created_at)
                )
            ).scalars().all()
            leads_sem_op = (
                await session.execute(
                    select(Lead)
                    .where(
                        Lead.dados_extras["carga"].as_string() == MARCA,
                        Lead.rd_lead_id.is_(None),
                        ~Lead.oportunidades.any(),
                    )
                    .order_by(Lead.created_at)
                )
            ).scalars().all()
            ja_enviadas = (
                await session.execute(
                    select(Oportunidade.id).where(
                        Oportunidade.dados_extras["carga"].as_string() == MARCA, Oportunidade.rd_deal_id.is_not(None)
                    )
                )
            ).all()
            if args.limite is not None:
                ops = ops[: args.limite]
                leads_sem_op = []
            print(f"A enviar: {len(ops)} oportunidades (com os contatos delas) e {len(leads_sem_op)} contatos "
                  f"sem oportunidade. Já enviadas antes: {len(ja_enviadas)}.")
            if not ops and not leads_sem_op:
                return

            conectado = await integracao.obter_token(session) is not None
            config = await integracao.obter_config(session)
            if not conectado or (config is not None and not config.ativo):
                motivo = "não está conectado" if not conectado else "está desativado"
                if not args.simular:
                    raise SystemExit(f"O RD Station {motivo} neste banco. Conecte pela tela Integração RD e repita.")
                print(f"(O RD Station {motivo} neste banco: sem as etapas do RD na simulação.)")

            locais = sorted({op.etapa_funil for op in ops if op.etapa_funil}, key=_ordem_local)
            mapa: dict[str, dict] = {}
            if conectado:
                access = await integracao.access_token_valido(session)
                await session.commit()
                funis = await envio.listar_funis(access)
                if not funis:
                    raise SystemExit("O RD Station não tem nenhum funil de vendas.")
                funil = next((f for f in funis if args.funil and _chave(f["nome"]) == _chave(args.funil)), None)
                if args.funil and funil is None:
                    raise SystemExit(f"Funil '{args.funil}' não encontrado. Funis: {', '.join(f['nome'] for f in funis)}")
                funil = funil or funis[0]
                if not funil["etapas"]:
                    raise SystemExit(f"O funil '{funil['nome']}' não tem etapas.")
                mapa = mapear_etapas(locais, funil["etapas"], ler_mapa(args.mapa))
                print(f"Funil do RD: {funil['nome']}")
            for nome in locais:
                destino = mapa[nome]["nome"] if nome in mapa else "?"
                print(f"  {nome} -> {destino} ({sum(op.etapa_funil == nome for op in ops)} oportunidades)")
            por_status: dict[str, int] = {}
            for op in ops:
                por_status[op.status or "?"] = por_status.get(op.status or "?", 0) + 1
            print("Status:", ", ".join(f"{k}={v}" for k, v in sorted(por_status.items())))

            if args.simular:
                for op in ops[:5]:
                    print(f"  ex.: {op.nome} | {op.lead.nome if op.lead else '-'} | R$ {op.valor} | {op.status}")
                print("Simulação: nada foi enviado.")
                return

            ids_ops = [(op.id, op.nome) for op in ops]
            for op in ops:
                if op.etapa_funil in mapa:
                    destino = mapa[op.etapa_funil]
                    op.rd_stage_id = str(destino["id"])
                    op.etapa_funil = destino["nome"]  # o funil local passa a usar as etapas do RD
            await session.commit()
            ids_leads = [(lead.id, lead.nome) for lead in leads_sem_op]

            enviados, falhas = 0, []
            for i, (op_id, nome) in enumerate(ids_ops, 1):
                try:
                    await _com_repeticao(session, envio.enviar_oportunidade, op_id)
                    enviados += 1
                except RdStationErro as e:
                    falhas.append(f"{nome}: {e}")
                    if "token" in str(e) or "403" in str(e):
                        break  # sem acesso, as próximas falhariam do mesmo jeito
                if i % 10 == 0 or i == len(ids_ops):
                    print(f"  {i}/{len(ids_ops)} oportunidades processadas")
                await asyncio.sleep(PAUSA)
            for lead_id, nome in ids_leads if not falhas else []:
                try:
                    await _com_repeticao(session, envio.enviar_lead, lead_id)
                    enviados += 1
                except RdStationErro as e:
                    falhas.append(f"{nome}: {e}")
                await asyncio.sleep(PAUSA)

            print(f"Enviados ao RD: {enviados}. Falhas: {len(falhas)}.")
            for f in falhas[:10]:
                print("  -", f)
            if falhas:
                print("Rode o comando de novo para tentar o que faltou (o que já foi enviado é pulado).")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
