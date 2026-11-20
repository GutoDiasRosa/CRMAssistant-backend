"""
Dados de teste do CRM Assist: carrega ou remove, de uma vez, a equipe de demonstração,
os leads, as oportunidades e a agenda de anotações.

    python -m scripts.dados_demo carregar   # equipe + ~140 leads + oportunidades + agenda
    python -m scripts.dados_demo limpar     # remove leads, oportunidades e anotações da carga

Com Docker, rode dentro do container da API (na raiz do repositório):
    docker compose exec api python -m scripts.dados_demo carregar

Os dados ficam só no banco local: nada é enviado ao RD Station. A equipe de demonstração usa a
senha "demo1234" (veja EQUIPE_DEMO em scripts/carga_demo.py); o "limpar" mantém esses usuários.
Os retornos da agenda são calculados a partir do dia em que a carga roda.

O banco é o de POSTGRES_URL. Se não for local, é preciso acrescentar --confirmar.
"""

import argparse
import asyncio

from scripts import carga_agenda, carga_demo


async def main() -> None:
    p = argparse.ArgumentParser(description="Carrega ou remove os dados de teste do CRM Assist.")
    p.add_argument("acao", choices=["carregar", "limpar"], help="carregar ou limpar os dados de teste")
    p.add_argument("--confirmar", action="store_true", help="obrigatório quando o banco não é local")
    args = p.parse_args()
    extra = ["--confirmar"] if args.confirmar else []

    if args.acao == "carregar":
        print("1/2 Equipe, leads e oportunidades")
        await carga_demo.main(["--criar-vendedores", *extra])
        print("2/2 Agenda (anotações e retornos)")
        await carga_agenda.main(extra)
        print(f"\nPronto. Entre com gerente@crmassist.dev / {carga_demo.SENHA_DEMO} para ver toda a equipe.")
    else:
        # A limpeza da carga_demo também apaga as anotações ligadas aos registros da carga.
        await carga_demo.main(["--limpar", *extra])


if __name__ == "__main__":
    asyncio.run(main())
