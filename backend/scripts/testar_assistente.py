"""
Faz uma bateria de perguntas ao assistente (POST /chat) e mostra as respostas, para conferir
a IA depois de mexer nas ferramentas, no prompt ou nos dados de demonstração.

Usa a equipe de demonstração (scripts/dados_demo.py). Cada pergunta abre uma conversa nova.

Uso (dentro de backend/, com o .venv ativo e a API no ar):
    python -m scripts.testar_assistente
    python -m scripts.testar_assistente --api http://localhost:8001 --perfil gerente
    python -m scripts.testar_assistente --perfil sdr "Quais leads devo priorizar hoje?"
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

from scripts.carga_demo import SENHA_DEMO

USUARIOS = {
    "gerente": "gerente@crmassist.dev",
    "sdr": "sdr@crmassist.dev",
    "closer": "closer@crmassist.dev",
}

PERGUNTAS = {
    "gerente": [
        "Como está o funil hoje?",
        "Quem é o vendedor com melhor desempenho?",
        "Quem é o vendedor com melhor desempenho este ano?",
        "Quanto vendemos em outubro?",
        "Por que perdemos negócios no mês passado?",
        "Quais oportunidades abertas foram criadas nos últimos 30 dias?",
        "Quais retornos estão atrasados?",
        "Qual a previsão do tempo amanhã?",
    ],
    "sdr": [
        "O que tenho na agenda esta semana?",
        "Quais são as oportunidades da Tatiane Rocha?",
        "Como estou indo este mês?",
    ],
    "closer": [
        "Quanto eu vendi este ano?",
        "Quem devo ligar hoje?",
    ],
}


def chamar(api: str, caminho: str, corpo: dict, token: str | None = None) -> dict:
    cabecalhos = {"Content-Type": "application/json"}
    if token:
        cabecalhos["Authorization"] = f"Bearer {token}"
    pedido = urllib.request.Request(api + caminho, data=json.dumps(corpo).encode(), headers=cabecalhos)
    with urllib.request.urlopen(pedido, timeout=180) as resposta:
        return json.load(resposta)


def main() -> None:
    p = argparse.ArgumentParser(description="Bateria de perguntas ao assistente do CRM Assist.")
    p.add_argument("--api", default="http://localhost:8000", help="endereço da API")
    p.add_argument("--perfil", choices=sorted(USUARIOS), help="só este perfil (padrão: todos)")
    p.add_argument("perguntas", nargs="*", help="perguntas próprias (exige --perfil)")
    args = p.parse_args()
    if args.perguntas and not args.perfil:
        p.error("informe --perfil para fazer perguntas próprias")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    for perfil in [args.perfil] if args.perfil else list(USUARIOS):
        try:
            token = chamar(args.api, "/auth/login", {"email": USUARIOS[perfil], "password": SENHA_DEMO})["access_token"]
        except urllib.error.URLError as e:
            raise SystemExit(f"Não consegui entrar como {USUARIOS[perfil]} em {args.api}: {e}") from e
        print(f"\n{'=' * 20} {perfil.upper()} ({USUARIOS[perfil]}) {'=' * 20}")
        for pergunta in args.perguntas or PERGUNTAS[perfil]:
            inicio = time.monotonic()
            resposta = chamar(args.api, "/chat", {"mensagem": pergunta}, token)
            print(f"\n### {pergunta}  [{time.monotonic() - inicio:.1f}s]\n{resposta['response_text']}")


if __name__ == "__main__":
    main()
