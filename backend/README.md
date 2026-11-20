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
- [Dados de teste (opcional)](#dados-de-teste-opcional)
- [Deploy (Render + Neon)](#deploy-render--neon-plano-gratuito)
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
| **RF03** | Sincronização de leads e oportunidades nos dois sentidos: webhook do RD → CRM Assist e envio CRM Assist → RD | `routes/webhooks_rd.py`, `routes/oportunidades.py`, `core/rd_station_sync/` |
| **RF04** | Funil de vendas em formato kanban | `GET /api/crm/kanban` |
| **RF05** | Chatbot em linguagem natural (texto) | `POST /chat`, `services/crm_assistant_orchestrator_service.py` |
| **RF06** | Resumo de leads, indicadores, performance do time e relatórios por período | `GET /api/crm/metricas`, `GET /api/crm/performance` e ferramentas do chat em `core/agents/crm_tools.py` |
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
   | `resumo_carteira` | Funil separado por situação: em aberto (por etapa), ganhas e perdidas, com período opcional |
   | `listar_oportunidades` | Oportunidades filtradas por etapa, nome, situação e período |
   | `vendas_periodo` | Vendas ganhas (ou perdidas, com o motivo) num período, pela data de fechamento; sem período, o mês atual |
   | `detalhes_oportunidade` | Uma oportunidade (pelo ID ou pelo nome) com contato e anotações |
   | `detalhes_lead` | Um lead (pelo ID ou pelo nome) com todas as oportunidades e anotações |
   | `buscar_leads` | Leads por nome ou e-mail e suas oportunidades |
   | `consultar_anotacoes` | Anotações dos vendedores sobre os clientes (texto, autor, data e retorno) |
   | `agenda_retornos` | Próximos contatos agendados, separando os do próprio usuário dos marcados por colegas |
   | `performance_vendedores` | Os números da tela Performance do time por vendedor e período (mês atual por padrão) |

   Períodos: vendas ganhas e perdidas contam pela data de fechamento (`fechada_em`); oportunidades em aberto e leads novos, pela data de criação.

4. As ferramentas consultam o banco pelos repositórios, que **já filtram pelo perfil do usuário** (RF07). Um SDR não consegue ver dados de outra pessoa nem pedindo pelo chat.
5. O modelo responde em português com base nos dados retornados, com instrução para não inventar números. Se ele recusar um pedido por política de segurança, a API refaz o pedido no modelo de *fallback* recomendado (`fallbacks: "default"`).
6. A resposta é gravada na conversa e devolvida ao front. Sem `ANTHROPIC_API_KEY`, o chat responde com mensagens simples de *fallback*.

### Fluxo da sincronização (`POST /webhooks/rd-station`)

A integração usa a **API v2 do RD Station CRM** (`https://api.rd.services/crm/v2`), com um aplicativo próprio do CRM na RD Station App Store.

0. Ao concluir a autorização OAuth na tela **Integração RD**, a própria API cria no RD Station CRM os webhooks `crm_deal_created`, `crm_deal_updated`, `crm_deal_deleted`, `crm_contact_created` e `crm_contact_updated` apontando para esta rota, com um token secreto na URL (`?token=...`).
1. O RD Station CRM envia o evento (`event_name` + `document`).
2. A API confere o token da URL; sem ele (ou com um token antigo), responde 401.
3. A requisição é respondida na hora com **202 Accepted**. O processamento acontece em segundo plano, o que evita *timeout* no RD Station.
4. O payload é normalizado e os registros são gravados com **upsert** pelos IDs do RD (`rd_lead_id` = contato, `rd_deal_id` = negociação). Assim, um mesmo evento reenviado não gera duplicatas (idempotência). Negociação excluída no RD é excluída aqui. Como o webhook de negociação não traz os contatos, a API busca o primeiro contato da negociação para ligá-la ao lead.
5. Cada execução fica registrada na tabela `sincronizacao` (sucesso, ignorado ou erro) para auditoria.

### Fluxo do envio (`POST/PUT /api/crm/oportunidades`)

1. A oportunidade criada ou editada no CRM Assist é gravada primeiro no banco local.
2. Com o RD conectado e a integração ativa, a API cria ou atualiza o contato (`/contacts`) e a negociação (`/deals`) no RD Station CRM, com etapa (`stage_id`), valor (`one_time_price`), status e responsável (usuário do RD com o mesmo e-mail; sem correspondência, o primeiro usuário ativo do RD, porque o RD exige um responsável). O id do contato é gravado antes de criar a negociação, para uma falha no meio não duplicar o contato no reenvio.
3. Os ids devolvidos pelo RD são gravados no registro local. Se o webhook do próprio RD chegar antes e criar uma cópia, as duas são unidas.
4. Se o RD recusar, a oportunidade continua salva, a tela mostra o motivo e a falha fica em `sincronizacao` (fonte `envio_rd`). Salvar de novo tenta outra vez.

O token do RD Station CRM dura 2 horas e cada renovação troca também o `refresh_token`; a renovação é feita com a linha do token travada, para que duas requisições simultâneas não invalidem uma à outra.

---

## Modelo de dados

As migrações estão em `alembic/versions/` (001 a 008) e rodam sozinhas quando a API sobe.

| Tabela | Finalidade |
|---|---|
| `usuario` | Membros da equipe, com perfil (`SDR`, `CLOSER`, `GERENTE`, `DIRETOR`, `ANALISTA`, `ADMIN`) |
| `leads` | Leads (contatos do RD Station CRM), vinculados ao vendedor responsável |
| `oportunidade` | Negociações, com etapa do funil, valor, status e data de fechamento (`fechada_em`, gravada ao ganhar ou perder) |
| `interacao` | Anotações dos vendedores (agenda): ligadas a lead e/ou oportunidade, com data do contato e próximo retorno |
| `conversa_chatbot` | Conversas do usuário com o assistente |
| `mensagem` | Mensagens de cada conversa (`user` / `assistant`) |
| `relatorio` | Relatórios gerados (parâmetros e resultado em JSON) |
| `sincronizacao` | Log de cada evento recebido do RD Station |
| `rd_station_token` | Tokens OAuth do RD Station |
| `rd_station_config` | Configuração cadastrada pela tela de integração: Client ID/Secret, usuário responsável, token e webhooks criados no RD |

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

Ajuste `POSTGRES_URL`, `JWT_SECRET`, `ADMIN_EMAIL` e `ADMIN_PASSWORD` no `.env` e inicie a API:

```bash
uvicorn app.main:app --reload --port 8000
```

O administrador vem das variáveis **`ADMIN_EMAIL`** e **`ADMIN_PASSWORD`** (mínimo 6 caracteres). A cada inicialização a API cria esse usuário ou, se ele já existir, garante o perfil `ADMIN`, a conta ativa e a senha da variável. Sem as duas variáveis, nenhum administrador é criado (aparece um aviso no log).

> Bancos criados antes dessa mudança têm o admin antigo `admin@crmassist.dev` com a senha `admin`. Se `ADMIN_EMAIL` for outro e-mail, esse admin antigo é desativado automaticamente enquanto ainda tiver a senha `admin`.

Para ter uma equipe e dados de exemplo, veja [Dados de teste (opcional)](#dados-de-teste-opcional).

As migrações (`alembic upgrade head`) rodam automaticamente na inicialização. Para desativar, defina `SKIP_ALEMBIC_ON_STARTUP=1`.

> Também há um `pyproject.toml` para quem prefere o [Poetry](https://python-poetry.org/): `poetry install`.

---

## Dados de teste (opcional)

O banco começa vazio, só com o administrador. Para avaliar o sistema com dados, há uma carga de demonstração que cria:

- uma equipe de 5 usuários (gerente, 2 SDRs e 2 closers);
- cerca de 140 leads e 140 oportunidades espalhados por 2026, com vendas ganhas e perdidas em datas coerentes;
- a agenda: cerca de 750 anotações (ligações, e-mails, visitas...) e os próximos retornos.

Nada é enviado ao RD Station. Com Docker, na raiz do repositório:

```bash
docker compose exec api python -m scripts.dados_demo carregar
```

Sem Docker, dentro de `backend/` com o `.venv` ativo: `python -m scripts.dados_demo carregar`.

Para remover leads, oportunidades e anotações da carga (os usuários de demonstração ficam):

```bash
docker compose exec api python -m scripts.dados_demo limpar
```

| E-mail | Perfil | Vê |
|---|---|---|
| `gerente@crmassist.dev` | Gerente | Toda a equipe |
| `sdr@crmassist.dev`, `sdr2@crmassist.dev` | SDR | Só a própria carteira |
| `closer@crmassist.dev`, `closer2@crmassist.dev` | Closer | Só a própria carteira |

A senha de todos é `demo1234`. Use esses dados só em ambientes de teste.

O passo a passo completo, do zero até o sistema aberto no navegador, está no [Guia da banca](../GUIA-DA-BANCA.md).

| Script (`backend/scripts/`) | Para que serve |
|---|---|
| `dados_demo.py` | Carrega ou remove todos os dados de teste de uma vez |
| `carga_demo.py` | Só a equipe, os leads e as oportunidades (`--leads`, `--etapas`, `--limpar`) |
| `carga_agenda.py` | Só a agenda; os retornos contam a partir do dia em que roda (`--limpar` e roda de novo para atualizá-los) |
| `testar_assistente.py` | Faz uma bateria de perguntas ao chat como gerente, SDR e closer (exige a carga e a chave da Anthropic) |
| `criar_banco_local.sql` | Cria o usuário e o banco num PostgreSQL local, sem Docker |

---

## Deploy (Render + Neon, plano gratuito)

A versão publicada usa a API no Render, o front-end na Vercel e o banco no Neon, todos no plano gratuito. O arquivo [`render.yaml`](../render.yaml) é um *Blueprint* do Render para criar a API (e, se preferir, também o front como site estático). O banco fica no Neon porque o Postgres grátis do Render expira em 30 dias.

| Parte | Onde | Endereço | Custo |
|---|---|---|---|
| Front-end | Vercel, repositório `CRMAssistant` (branch `main`) | domínio do projeto na Vercel | grátis |
| API (Dockerfile) | Render, pasta `backend/` deste repositório | `https://crmassistant-backend.onrender.com` | grátis (750 h/mês) |
| PostgreSQL | Neon, região AWS us-east-1 | interno | grátis (0,5 GB) |

Cada `git push` na `main` gera um novo deploy do serviço correspondente. As migrações rodam sozinhas quando a API sobe.

1. No [Neon](https://neon.com), crie um projeto na região **AWS US East (N. Virginia)** e copie a *connection string* **direta** (sem `-pooler` no host).
2. No [Render](https://render.com), conecte o GitHub com acesso aos dois repositórios e crie um *Blueprint* a partir deste repositório. Na criação, informe `POSTGRES_URL` (a string do Neon), `ADMIN_EMAIL` e `ADMIN_PASSWORD`; `ANTHROPIC_API_KEY` pode ficar vazia por enquanto.
3. Confira as URLs geradas. Se o Render acrescentou um sufixo aos nomes, ajuste `FRONTEND_URL`/`CORS_ORIGINS` na API e `VITE_API_URL` no front.
4. Entre com `ADMIN_EMAIL` / `ADMIN_PASSWORD`. Para trocar o e-mail ou a senha do administrador, altere as variáveis em **Environment** no painel do Render e faça um novo deploy (o serviço reinicia e aplica os valores). Trocar o e-mail cria um novo administrador; o anterior continua existindo.
5. No plano grátis a API hiberna após 15 minutos sem acesso e leva cerca de 1 minuto para voltar. Para receber webhooks sem atraso, agende no [cron-job.org](https://cron-job.org) (grátis) um `GET <endereço da API>/health` a cada 10 minutos.
6. Entre no sistema e abra **Integração RD**. Crie um aplicativo para o **RD Station CRM** na App Store do RD (credenciais do RD Station Marketing não servem) e cadastre nele a URL de callback exibida na tela, que já é a pública. O webhook é criado sozinho ao final da autorização. A API do CRM exige o plano Basic, Pro ou Advanced do RD Station CRM.

> Em produção, a carga de demonstração cria usuários com uma senha publicada neste README. Se usar, troque essas senhas ou desative os usuários depois.

---

## Variáveis de ambiente

| Variável | Obrigatória | Padrão | Descrição |
|---|---|---|---|
| `POSTGRES_URL` | Sim | `postgresql+asyncpg://crm:crm@localhost:5432/crmassistant` | Conexão com o banco. Aceita também a URL crua dos provedores, como a do Neon (`postgresql://...?sslmode=require&channel_binding=require`) |
| `JWT_SECRET` | Sim | `change-me` | Chave de assinatura dos tokens. **Troque em produção** |
| `CORS_ORIGINS` | Não | `*` | Origens permitidas, separadas por vírgula (ex.: `http://localhost:5173`) |
| `ANTHROPIC_API_KEY` | Não | — | Chave da API do Claude. Sem ela, o chat usa respostas de *fallback* |
| `ANTHROPIC_MODEL` | Não | `claude-opus-5` | Modelo usado pelo assistente |
| `ANTHROPIC_EFFORT` | Não | `low` | Nível de esforço do modelo (`low`, `medium`, `high`...). `low` mantém o chat rápido |
| `ADMIN_EMAIL` | Sim | — | E-mail do administrador, aplicado a cada inicialização da API |
| `ADMIN_PASSWORD` | Sim | — | Senha do administrador (mínimo 6 caracteres), aplicada a cada inicialização da API |
| `FRONTEND_URL` | Não | `http://localhost:5173` | Front-end para onde o callback OAuth devolve o usuário |
| `API_PUBLIC_URL` | Não | `RENDER_EXTERNAL_URL` | Endereço público da API, usado nas URLs de webhook e callback. No Render é preenchido sozinho |
| `SKIP_ALEMBIC_ON_STARTUP` | Não | — | `1` para não rodar as migrações ao iniciar (usado nos testes) |

As credenciais do RD Station (Client ID e Client Secret) não usam variáveis de ambiente: são cadastradas pela tela **Integração RD** e guardadas na tabela `rd_station_config`.

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
| GET | `/api/crm/kanban` 🔒 | Oportunidades por etapa do funil. Filtros: `usuario_id`, `inicio`/`fim` (criação), `busca` (empresa ou contato), `situacao` (`abertas`, `em_andamento`, `pausadas`, `ganhas`, `perdidas`), `valor_min`/`valor_max` |
| GET | `/api/crm/metricas` 🔒 | Indicadores do período (`inicio`/`fim`): leads e oportunidades criados, ganhas e perdidas pela data de fechamento |
| GET | `/api/crm/performance` 🔒 | Por vendedor no período: prospectados, qualificados, convertidos, fechados e perdidos |
| GET | `/api/crm/vendedores` 🔒 | Vendedores para os filtros (quem não vê a equipe recebe só a si mesmo) |
| GET | `/api/crm/sincronizacoes` 🔒 | Últimas sincronizações recebidas do RD Station |
| GET | `/api/crm/leads` 🔒 | Lista os leads (RF07). Filtros: `busca` (nome, e-mail ou telefone), `usuario_id`, `criado_de`/`criado_ate`, `oportunidades` (`com_aberta`, `com_ganha`, `sem`), `rd` (`sincronizado`, `local`) |
| GET | `/api/crm/leads/{id}` 🔒 | Detalhe do lead com as oportunidades dele (RF07) |
| POST | `/api/crm/leads` 🔒 | Cria um lead e o envia ao RD Station CRM como contato |
| PUT | `/api/crm/leads/{id}` 🔒 | Atualiza um lead e envia a alteração ao RD Station CRM |
| GET | `/api/crm/anotacoes` 🔒 | Anotações por lead, oportunidade, período ou autor (RF07) |
| GET | `/api/crm/anotacoes/retornos` 🔒 | Retornos agendados até uma data (`ate`), incluindo os atrasados |
| POST | `/api/crm/anotacoes` 🔒 | Registra uma anotação (tipo, texto, data do contato e retorno opcional) |
| PUT/DELETE | `/api/crm/anotacoes/{id}` 🔒 | Altera ou exclui uma anotação (autor ou admin) |
| GET | `/api/crm/etapas` 🔒 | Funis e etapas do RD Station CRM (ou, sem RD, as etapas já usadas no CRM Assist) |
| GET | `/api/crm/oportunidades/{id}` 🔒 | Detalhe da oportunidade, com contato, responsável e interações (RF07) |
| POST | `/api/crm/oportunidades` 🔒 | Cria uma oportunidade e a envia ao RD Station CRM |
| PUT | `/api/crm/oportunidades/{id}` 🔒 | Atualiza uma oportunidade e envia a alteração ao RD Station CRM |
| GET | `/oauth/rd/conexao` 🔒 admin | Status, credenciais (sem o secret), URL de callback e webhooks criados |
| PUT | `/oauth/rd/configuracao` 🔒 admin | Ativa/desativa a integração e define o usuário responsável |
| PUT | `/oauth/rd/credenciais` 🔒 admin | Salva Client ID e Client Secret do app RD Station |
| POST | `/oauth/rd/authorize-url` 🔒 admin | Gera a URL de autorização do RD com `state` assinado |
| GET | `/oauth/rd/callback` | Recebe o `code` do RD, armazena os tokens, cria os webhooks no RD e volta para `FRONTEND_URL/integracao-rd` |
| POST | `/oauth/rd/webhook` 🔒 admin | Cria (ou recria) os webhooks no RD Station |
| POST | `/oauth/rd/testar` 🔒 admin | Testa a conexão criando (ou atualizando) um contato de teste (`teste.crmassist@example.com`) no RD Station CRM |
| DELETE | `/oauth/rd/conexao` 🔒 admin | Desconecta o RD Station (remove os webhooks e apaga os tokens) |
| GET | `/oauth/rd/status` 🔒 | Indica se o RD Station já está conectado |
| POST | `/webhooks/rd-station` | Recebe eventos de negociações e contatos do RD Station CRM |

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
    { "id": "…", "rd_deal_id": "…", "nome": "Construtora Alfa - Andaimes", "valor": 45000.0, "status": "ongoing", "usuario_id": "…", "lead_id": "…", "lead_nome": "João Silva" }
  ],
  "Proposta": [ … ]
}
```

**Webhook do RD Station**

Não há cadastro manual: ao concluir a autorização, a API cria os webhooks pela API do RD Station CRM (`POST https://api.rd.services/crm/v2/webhooks`) com a URL `https://<seu-dominio>/webhooks/rd-station?token=<token>`. O RD só alcança endereços públicos, então em desenvolvimento local o webhook não é criado (a tela explica o motivo). O payload é o envelope do RD Station CRM: `{"event_name": "crm_deal_updated", "document": {...}}`. O mapeamento dos campos está em `app/core/rd_station_sync/normalize.py`.

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
│   └── versions/               # 001 a 008
├── app/
│   ├── core/
│   │   ├── agents/
│   │   │   ├── crm_tools.py         # Ferramentas que o Claude usa para consultar o CRM
│   │   │   └── intent_router_crm.py   # Classificação simples de intenção
│   │   └── rd_station_sync/           # Integração com o RD Station
│   │       ├── normalize.py           #   extrai e mapeia campos do payload
│   │       ├── process.py             #   fluxo de processamento do evento
│   │       ├── upsert.py              #   gravação idempotente + log
│   │       ├── integracao.py          #   OAuth, token e webhooks na API do RD Station CRM
│   │       └── envio.py               #   envio de contatos e negociações para o RD
│   ├── db/
│   │   ├── database.py         # Engine e sessão assíncronas
│   │   └── models.py           # Modelos SQLAlchemy (DER)
│   ├── domain/repositories/
│   │   ├── crm.py              # LeadRepository, OportunidadeRepository (RF07)
│   │   └── anotacoes.py        # AnotacaoRepository: anotações e agenda (RF07)
│   ├── middleware/
│   │   └── request_logging.py  # Log de método, rota, status e tempo de resposta
│   ├── routes/                 # Endpoints HTTP
│   ├── services/
│   │   └── crm_assistant_orchestrator_service.py
│   ├── config.py               # Configurações (Pydantic Settings)
│   ├── dependencies.py         # Usuário autenticado e regras de perfil
│   ├── security.py             # JWT e hash de senha
│   └── main.py                 # Criação da aplicação e ciclo de vida
├── scripts/                    # Dados de teste e utilitários (ver "Dados de teste")
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
- Modelo de dados com migrações Alembic, incluindo a data de fechamento das oportunidades
- Fluxo OAuth 2.0 com o RD Station CRM, com renovação automática do token
- Webhook do RD Station criado automaticamente, com token, processamento assíncrono, idempotência e log
- Leads e oportunidades criados e editados no CRM Assist e enviados ao RD Station CRM
- Anotações dos vendedores e agenda de retornos
- Kanban, métricas e performance do time por período, com filtro por perfil
- Busca e filtros avançados nas listagens de leads e do funil
- Chat com histórico, ferramentas de consulta (vendas por período, desempenho, agenda, resumo de lead e oportunidade pelo ID) e resposta via Claude
- Integração com o front-end (React + Vite), publicada na Vercel
- Carga de dados de demonstração opcional

**Próximos passos**

- [ ] Recuperação de senha (RF01)
- [ ] Gráficos de indicadores gerados pelo chat (RF06)
- [ ] Sincronização periódica como alternativa ao webhook (RNF de até 5 minutos)
- [ ] Ampliar a cobertura de testes

---

## Autor

**Augusto Dias Rosa**, TCC, COTEMIG, 2026.
