# CRM Assist

Assistente de CRM com inteligência artificial para equipes de vendas B2B, integrado ao **RD Station**. É o projeto de Trabalho de Conclusão de Curso (TCC) de Augusto Dias Rosa no COTEMIG.

Com o CRM Assist, cada membro do time comercial consulta funil, leads, desempenho e relatórios **em linguagem natural**, sem depender de quem domina o RD Station.

## Componentes

| Parte | Tecnologia | Onde |
|---|---|---|
| Backend (API) | Python, FastAPI, PostgreSQL, Claude (SDK Anthropic) | [`backend/`](backend/README.md) |
| Front-end | React, Vite, TypeScript, Tailwind | repositório [CRMAssistant](https://github.com/GutoDiasRosa/CRMAssistant) |

## Início rápido

```bash
docker compose up --build
```

- API: <http://localhost:8000>
- Documentação: <http://localhost:8000/docs>

Detalhes de arquitetura, endpoints e configuração estão no [README do backend](backend/README.md).
