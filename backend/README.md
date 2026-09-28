# CRM Assist — Backend

API do **CRM Assist**, um assistente de CRM com inteligência artificial para equipes de vendas B2B, desenvolvido como Trabalho de Conclusão de Curso (TCC) no COTEMIG.

O backend se integra ao **RD Station CRM**, mantém uma cópia local dos leads e oportunidades em PostgreSQL e expõe um **chatbot em linguagem natural** que responde perguntas sobre funil, leads, desempenho do time e relatórios, sempre respeitando o perfil de acesso de cada usuário.

---

## Sumário

- [Contexto do problema](#contexto-do-problema)
- [Funcionalidades](#funcionalidades)
- [Arquitetura](#arquitetura)
- [Modelo de dados](#modelo-de-dados)
- [Stack](#stack)
- [Como rodar](#como-rodar)
- [Variáveis de ambiente](#variáveis-de-ambiente)
- [Endpoints](#endpoints)
- [Perfis e permissões](#perfis-e-permissões)
- [Estrutura de pastas](#estrutura-de-pastas)
- [Testes](#testes)
- [Status do desenvolvimento](#status-do-desenvolvimento)

---

## Contexto do problema

O cenário do TCC é uma distribuidora B2B de equipamentos para construção civil. A equipe comercial (SDRs, closers, gerente e diretor) depende de **uma única analista** com domínio técnico do RD Station para extrair qualquer dado ou relatório. Isso gera gargalo, atrasos e retrabalho.

O CRM Assist remove esse gargalo: cada pessoa do time consulta os dados do CRM **diretamente, em linguagem natural**, e visualiza o funil de vendas sem precisar operar o RD Station.

---

## Funcionalidades

| Requisito | Descrição | Onde está no backend |
|---|---|---|
| **RF01** | Autenticação por e-mail e senha (JWT) | `routes/auth.py`, `security.py` |
| **RF02** | Integração com o RD Station via OAuth 2.0 | `routes/integracao_rd.py` |
| **RF03** | Sincronização automática de leads e oportunidades por webhook | `routes/webhooks_rd.py`, `core/rd_station_sync/` |
| **RF04** | Funil de vendas em formato kanban | `GET /api/crm/kanban` |
| **RF05** | Chatbot em linguagem natural (texto) | `POST /chat`, `services/crm_assistant_orchestrator_service.py` |
| **RF06** | Resumo de leads, indicadores, performance do time e relatórios por período | Ferramentas do chat em `core/agents/crm_tools.py` |
| **RF07** | Controle de acesso por perfil | `domain/repositories/crm.py`, `dependencies.py` |
| **LGPD** | Exclusão dos dados pessoais do próprio usuário | `DELETE /auth/me/lgpd` |

---

## Arquitetura

A API é organizada em camadas com responsabilidades separadas: rotas enxutas, um serviço orquestrador para o assistente, módulos de domínio e repositórios que encapsulam o acesso ao banco.

```
            ┌──────────────┐        ┌───────────────────────┐
            │  Front-end   │        │      RD Station       │
            │ React + Vite │        │  (OAuth 2.0 + webhook)│
            └──────┬───────┘        └───────────┬───────────┘
                   │ HTTPS + JWT                │ eventos de lead / oportunidade
┌──────────────────▼────────────────────────────▼────────────────────┐
│                     Rotas FastAPI  (app/routes/)                   │
│  /auth · /chat · /api/crm · /oauth/rd · /webhooks/rd-station       │
└──────────┬───────────────────────────────────────────┬─────────────┘
           │                                           │
┌──────────▼──────────────────────────┐  ┌─────────────▼─────────────┐
│ CrmAssistantOrchestratorService     │  │  rd_station_sync          │
│ (app/services/)                     │  │  (app/core/)              │
│  1. registra a mensagem             │  │  normalize → upsert       │
│  2. classifica a intenção           │  │  → log em sincronizacao   │
│  3. busca contexto nos repositórios │  │  (processado em           │
│  4. gera a resposta com o LLM       │  │   BackgroundTasks)        │
└──────────┬──────────────────────────┘  └─────────────┬─────────────┘
           │                                           │
┌──────────▼───────────────────────────────────────────▼─────────────┐
│       Repositórios  (app/domain/repositories/), aplicam o RF07     │
└──────────────────────────────────┬─────────────────────────────────┘
                                   │ SQLAlchemy 2 async (asyncpg)
                         ┌─────────▼─────────┐
                         │   PostgreSQL 16   │
                         └───────────────────┘
```

### Fluxo do chat (`POST /chat`)

1. O usuário autenticado envia uma mensagem e, opcionalmente, o `sessionId` de uma conversa existente.
2. O orquestrador cria ou recupera a conversa, carrega as últimas 20 mensagens como histórico e grava a nova mensagem na tabela `mensagem`.
3. A pergunta vai para o **Claude** (SDK oficial `anthropic`) junto com o histórico e um conjunto de **ferramentas** (`core/agents/crm_tools.py`). O modelo decide quais consultar:

   | Ferramenta | Retorna |
   |---|---|
   | `resumo_carteira` | Totais de leads e oportunidades, quantidade e valor por etapa |
   | `listar_oportunidades` | Oportunidades filtradas por etapa e/ou nome |
   | `detalhes_oportunidade` | Uma oportunidade com contato e histórico de interações |
   | `buscar_leads` | Leads por nome ou e-mail e suas oportunidades |
   | `performance_vendedores` | Quantidade e valor de oportunidades por vendedor |

4. As ferramentas consultam o banco pelos repositórios, que **já filtram pelo perfil do usuário** (RF07). Um SDR não consegue ver dados de outra pessoa nem pedindo pelo chat.
5. O modelo responde em português com base nos dados retornados, com instrução para não inventar números. Se ele recusar um pedido por política de segurança, a API refaz o pedido no modelo de *fallback* recomendado (`fallbacks: "default"`).
6. A resposta é gravada na conversa e devolvida ao front. Sem `ANTHROPIC_API_KEY`, o chat responde com mensagens simples de *fallback*.

### Fluxo da sincronização (`POST /webhooks/rd-station`)

1. O RD Station envia um evento de conversão ou atualização.
2. A API valida o segredo compartilhado (`X-Webhook-Secret`) e, quando presente, a assinatura HMAC-SHA256 (`X-RD-Signature`).
3. A requisição é respondida na hora com **202 Accepted**. O processamento acontece em segundo plano, o que evita *timeout* no RD Station.
4. O payload é normalizado e os registros são gravados com **upsert** pelos IDs do RD (`rd_lead_id`, `rd_deal_id`). Assim, um mesmo evento reenviado não gera duplicatas (idempotência).
5. Cada execução fica registrada na tabela `sincronizacao` (sucesso, ignorado ou erro) para auditoria.

---

## Modelo de dados

A migração inicial está em `alembic/versions/001_initial_schema.py`.

| Tabela | Finalidade |
|---|---|
| `usuario` | Membros da equipe, com perfil (`SDR`, `CLOSER`, `GERENTE`, `DIRETOR`, `ANALISTA`, `ADMIN`) |
| `leads` | Leads sincronizados do RD Station, vinculados ao vendedor responsável |
| `oportunidade` | Negociações, com etapa do funil, valor e status |
| `interacao` | Histórico de contatos e anotações de uma oportunidade |
| `conversa_chatbot` | Conversas do usuário com o assistente |
| `mensagem` | Mensagens de cada conversa (`user` / `assistant`) |
| `relatorio` | Relatórios gerados (parâmetros e resultado em JSON) |
| `sincronizacao` | Log de cada evento recebido do RD Station |
| `rd_station_token` | Tokens OAuth do RD Station |

```
usuario 1───* leads 1───* oportunidade 1───* interacao
   │                          ▲
   ├──────────────────────────┘ (responsável)
   ├───* conversa_chatbot 1───* mensagem
   └───* relatorio
```

---

## Stack

| Camada | Tecnologia |
|---|---|
| Linguagem | Python 3.11+ |
| Framework web | FastAPI + Uvicorn |
| Banco de dados | PostgreSQL 16 |
| ORM e migrações | SQLAlchemy 2 (async, asyncpg) + Alembic |
| Autenticação | JWT (python-jose) + bcrypt (passlib) |
| IA | Claude (SDK oficial `anthropic`) com tool use |
| Integração HTTP | httpx |
| Testes e qualidade | pytest, ruff |
| Infraestrutura | Docker e Docker Compose |

---

## Como rodar

### Com Docker (recomendado)

Na **raiz do repositório**:

```bash
docker compose up --build
```

Dois serviços são iniciados:

| Serviço | Porta | Descrição |
|---|---|---|
| `db` | 5432 | PostgreSQL 16 |
| `api` | 8000 | API FastAPI (aplica as migrações ao iniciar) |

- API: <http://localhost:8000>
- Documentação interativa (Swagger): <http://localhost:8000/docs>

Para habilitar a IA e o RD Station, defina as variáveis no terminal ou num arquivo `.env` na raiz antes de subir os serviços. Veja [Variáveis de ambiente](#variáveis-de-ambiente).

### Localmente (sem Docker)

Pré-requisitos: Python 3.11+ e um PostgreSQL acessível.

Crie o usuário `crm` e o banco `crmassistant` (pede a senha do superusuário `postgres`):

```bash
psql -U postgres -h localhost -f backend/scripts/criar_banco_local.sql
```

> No Windows, o `psql` fica em `C:\Program Files\PostgreSQL\<versão>\bin\psql.exe`.

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/macOS
pip install -r requirements.txt
copy .env.example .env          # Windows  (cp no Linux/macOS)
```

Ajuste `POSTGRES_URL` e `JWT_SECRET` no `.env` e inicie a API:

```bash
uvicorn app.main:app --reload --port 8000
```

Para ter usuários (incluindo um administrador) e dados de exemplo, rode o script de demonstração. As credenciais estão no início do arquivo `scripts/seed_dev.py`:

```bash
python -m scripts.seed_dev
```

As migrações (`alembic upgrade head`) rodam automaticamente na inicialização. Para desativar, defina `SKIP_ALEMBIC_ON_STARTUP=1`.

> Também há um `pyproject.toml` para quem prefere o [Poetry](https://python-poetry.org/): `poetry install`.

---

## Variáveis de ambiente

| Variável | Obrigatória | Padrão | Descrição |
|---|---|---|---|
| `POSTGRES_URL` | Sim | `postgresql+asyncpg://crm:crm@localhost:5432/crmassistant` | Conexão com o banco |
| `JWT_SECRET` | Sim | `change-me` | Chave de assinatura dos tokens. **Troque em produção** |
| `CORS_ORIGINS` | Não | `*` | Origens permitidas, separadas por vírgula (ex.: `http://localhost:5173`) |
| `ANTHROPIC_API_KEY` | Não | — | Chave da API do Claude. Sem ela, o chat usa respostas de *fallback* |
| `ANTHROPIC_MODEL` | Não | `claude-opus-5` | Modelo usado pelo assistente |
| `ANTHROPIC_EFFORT` | Não | `low` | Nível de esforço do modelo (`low`, `medium`, `high`...). `low` mantém o chat rápido |
| `RD_CLIENT_ID` | Para o RF02 | — | Client ID do app no RD Station |
| `RD_CLIENT_SECRET` | Para o RF02 | — | Client secret do app no RD Station |
| `RD_REDIRECT_URI` | Para o RF02 | `http://localhost:8000/oauth/rd/callback` | URL de retorno do OAuth |
| `RD_OAUTH_AUTHORIZE_URL` | Não | `https://api.rd.services/auth/dialog` | Endpoint de autorização do RD |
| `RD_OAUTH_TOKEN_URL` | Não | `https://api.rd.services/auth/token` | Endpoint de token do RD |
| `RD_WEBHOOK_SECRET` | Recomendada | — | Segredo do webhook. **Se ficar vazia, o webhook aceita qualquer chamada (use só em desenvolvimento)** |
| `SKIP_ALEMBIC_ON_STARTUP` | Não | — | `1` para não rodar as migrações ao iniciar (usado nos testes) |

---

## Endpoints

Os endpoints marcados com 🔒 exigem o header `Authorization: Bearer <token>`. Os marcados com 👑 são exclusivos do perfil `ADMIN`: não existe cadastro público, e todo usuário é criado por um administrador.

| Método | Caminho | Descrição |
|---|---|---|
| GET | `/health` | Verificação de saúde da API |
| POST | `/auth/login` | Login (retorna JWT) |
| GET | `/auth/me` 🔒 | Dados do usuário autenticado (nome, e-mail, perfil) |
| GET | `/usuarios` 🔒 👑 | Lista os usuários |
| POST | `/usuarios` 🔒 👑 | Cadastra um usuário com perfil |
| DELETE | `/auth/me/lgpd` 🔒 | Exclui o usuário e as conversas dele; desvincula leads e oportunidades |
| POST | `/chat` 🔒 | Envia uma mensagem ao assistente |
| GET | `/api/crm/kanban` 🔒 | Oportunidades agrupadas por etapa do funil |
| GET | `/api/crm/metricas` 🔒 | Totais e valor somado de leads e oportunidades |
| GET | `/api/crm/sincronizacoes` 🔒 | Últimas sincronizações recebidas do RD Station |
| GET | `/oauth/rd/authorize` | Redireciona para a autorização no RD Station |
| GET | `/oauth/rd/callback` | Recebe o `code` do RD e armazena os tokens |
| GET | `/oauth/rd/status` 🔒 | Indica se o RD Station já está conectado |
| POST | `/webhooks/rd-station` | Recebe eventos de leads e oportunidades do RD Station |

### Exemplos

**Login**

```http
POST /auth/login
Content-Type: application/json

{ "email": "carlos@empresa.com", "password": "********" }
```

```json
{ "access_token": "eyJhbGciOi...", "token_type": "bearer" }
```

**Chat**

```http
POST /chat
Authorization: Bearer eyJhbGciOi...
Content-Type: application/json

{ "mensagem": "Como está meu funil esta semana?", "sessionId": null }
```

```json
{
  "detected_intent": "consulta_funil",
  "response_text": "Você tem 12 oportunidades abertas, distribuídas em 4 etapas...",
  "sessionId": "5f0c7c1e-8a4b-4d1e-9f3a-2b6c1d7e8f90"
}
```

Envie o `sessionId` retornado nas mensagens seguintes para continuar a mesma conversa.

**Kanban**

```json
{
  "Qualificação": [
    { "id": "…", "rd_deal_id": "…", "nome": "Obra Residencial Alfa", "valor": 45000.0, "status": "open", "usuario_id": "…", "lead_id": "…" }
  ],
  "Proposta": [ … ]
}
```

**Webhook do RD Station**

No RD Station, configure a URL pública `https://<seu-dominio>/webhooks/rd-station` e envie o mesmo valor de `RD_WEBHOOK_SECRET` no header `X-Webhook-Secret`. O payload aceito é um objeto JSON com `lead` e/ou `deal`, diretamente ou dentro de `data`. O mapeamento dos campos está em `app/core/rd_station_sync/normalize.py`.

---

## Perfis e permissões

O controle de acesso (RF07) é aplicado nos **repositórios**. Assim, qualquer consulta (kanban, métricas ou contexto do chatbot) já chega filtrada.

| Perfil | Visibilidade |
|---|---|
| `SDR` | Apenas os próprios leads e oportunidades |
| `CLOSER` | Apenas os próprios leads e oportunidades |
| `GERENTE` | Todos os dados da equipe |
| `DIRETOR` | Todos os dados da equipe |
| `ANALISTA` | Todos os dados da equipe |
| `ADMIN` | Todos os dados da equipe e gestão de usuários |

---

## Estrutura de pastas

```
backend/
├── alembic/                    # Migrações do banco
│   └── versions/001_initial_schema.py
├── app/
│   ├── core/
│   │   ├── agents/
│   │   │   ├── crm_tools.py         # Ferramentas que o Claude usa para consultar o CRM
│   │   │   └── intent_router_crm.py   # Classificação simples de intenção
│   │   └── rd_station_sync/           # Integração com o RD Station
│   │       ├── normalize.py           #   extrai e mapeia campos do payload
│   │       ├── process.py             #   fluxo de processamento do evento
│   │       ├── upsert.py              #   gravação idempotente + log
│   │       └── webhook_auth.py        #   validação de segredo e assinatura
│   ├── db/
│   │   ├── database.py         # Engine e sessão assíncronas
│   │   └── models.py           # Modelos SQLAlchemy (DER)
│   ├── domain/repositories/
│   │   └── crm.py              # LeadRepository, OportunidadeRepository (RF07)
│   ├── middleware/
│   │   └── request_logging.py  # Log de método, rota, status e tempo de resposta
│   ├── routes/                 # Endpoints HTTP
│   ├── services/
│   │   └── crm_assistant_orchestrator_service.py
│   ├── config.py               # Configurações (Pydantic Settings)
│   ├── dependencies.py         # Usuário autenticado e regras de perfil
│   ├── security.py             # JWT e hash de senha
│   └── main.py                 # Criação da aplicação e ciclo de vida
├── tests/
├── Dockerfile
├── pyproject.toml
└── requirements.txt
```

---

## Testes

```powershell
cd backend
$env:SKIP_ALEMBIC_ON_STARTUP="1"
python -m pytest -q
```

```bash
# Linux/macOS
cd backend
SKIP_ALEMBIC_ON_STARTUP=1 python -m pytest -q
```

---

## Status do desenvolvimento

**Implementado**

- Autenticação JWT com perfis e exclusão de dados (LGPD)
- Modelo de dados completo com migração Alembic
- Webhook do RD Station com validação, processamento assíncrono, idempotência e log
- Fluxo OAuth 2.0 com o RD Station e armazenamento de tokens
- Endpoints de kanban e métricas com filtro por perfil
- Chat com histórico de conversas, classificação de intenção e resposta via Claude

**Próximos passos**

- [ ] Integração com o front-end (React + Vite)
- [ ] Recuperação de senha (RF01)
- [ ] Gráficos de indicadores gerados pelo chat (RF06)
- [ ] Relatório de vendas por período no chat (RF06), que depende de datas de fechamento vindas do RD Station
- [ ] Renovação automática do token OAuth (refresh token)
- [ ] Sincronização periódica como alternativa ao webhook (RNF de até 5 minutos)
- [ ] Ampliar a cobertura de testes

---

## Autor

**Augusto Dias Rosa**, TCC, COTEMIG, 2026.
