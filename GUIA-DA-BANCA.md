# Guia da banca: rodando o CRM Assist localmente

Este guia leva do arquivo entregue até o sistema aberto no navegador, em cerca de 10 minutos. No meio do caminho você escolhe se quer começar com a base vazia ou com dados de teste.

O projeto tem duas partes, cada uma numa pasta:

| Pasta | O que é |
|---|---|
| `CRMAssistant-backend/` | API (Python, FastAPI) e banco PostgreSQL, que sobem juntos pelo Docker |
| `CRMAssistant/` | Front-end (React + Vite), que roda com o Node.js |

---

## 1. Pré-requisitos

| Ferramenta | Versão | Para quê |
|---|---|---|
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) | recente, **aberto e rodando** | API e banco de dados |
| [Node.js](https://nodejs.org/) | 18 ou superior (recomendado 20) | Front-end |
| Chave da API da Anthropic | opcional | Respostas do assistente com IA |

As portas **5432** (banco), **8000** (API) e **5173** (front-end) precisam estar livres. Se você já tem um PostgreSQL instalado na máquina, pare o serviço dele antes ou veja [Problemas comuns](#6-problemas-comuns).

---

## 2. Subir a API e o banco

Abra um terminal na pasta `CRMAssistant-backend/`.

**Opcional, para o assistente responder com IA:** crie nessa pasta um arquivo `.env` com a sua chave:

```env
ANTHROPIC_API_KEY=sua-chave-aqui
```

Sem a chave, o sistema funciona normalmente e o assistente responde em "modo sem IA": reconhece as perguntas diretas (vendas e perdas num período, desempenho do time, funil, agenda e o resumo de um lead ou de uma oportunidade) e responde com os dados do banco, em formato fixo. Perguntas livres exigem a chave.

Suba os serviços:

```bash
docker compose up --build -d
```

A primeira vez leva alguns minutos (download das imagens e instalação das dependências). A API cria as tabelas e o administrador sozinha. Para saber se terminou, acompanhe o log até aparecer `Application startup complete`:

```bash
docker compose logs -f api
```

(Ctrl + C sai do log sem parar a API.) Confira em <http://localhost:8000/health>: deve aparecer `{"status":"ok"}`.

---

## 3. Escolher a base: vazia ou com dados de teste

Os dados de teste são uma empresa fictícia, uma distribuidora de equipamentos para construção civil, com uma equipe de vendas e um ano de movimento. Eles ficam **só no seu banco local**: nada é enviado para fora.

### Opção A: começar com a base vazia

Não faça nada. Você entra como administrador e cadastra usuários (tela **Usuários**), leads e oportunidades pela própria interface. As telas de indicadores e o assistente começam sem números.

### Opção B: carregar os dados de teste (recomendado para avaliar)

Ainda na pasta `CRMAssistant-backend/`:

```bash
docker compose exec api python -m scripts.dados_demo carregar
```

Em poucos segundos o comando cria:

- uma equipe de 5 pessoas: 1 gerente, 2 SDRs (prospectores) e 2 closers;
- cerca de 140 leads e 140 oportunidades, de janeiro até hoje, com vendas ganhas e perdidas em datas coerentes;
- a agenda: cerca de 750 anotações de contatos (ligações, e-mails, visitas, reuniões) e os próximos retornos de cada vendedor.

A saída termina com `Pronto. Entre com gerente@crmassist.dev / demo1234 ...`.

### Mudou de ideia?

| Quero... | Comando (na pasta `CRMAssistant-backend/`) |
|---|---|
| Remover os dados de teste e manter o resto | `docker compose exec api python -m scripts.dados_demo limpar` |
| Atualizar a agenda (os retornos contam a partir do dia da carga) | `docker compose exec api python -m scripts.carga_agenda --limpar` e depois `docker compose exec api python -m scripts.carga_agenda` |
| Apagar tudo e voltar ao banco zerado | `docker compose down -v` e depois `docker compose up -d` |

O `limpar` remove leads, oportunidades e anotações da carga, inclusive anotações que você tenha feito neles pela tela. Os 5 usuários de demonstração continuam existindo; o `docker compose down -v` apaga até eles.

---

## 4. Subir o front-end

Abra outro terminal na pasta `CRMAssistant/` e crie o arquivo de configuração a partir do exemplo (ele já aponta para a API em `http://localhost:8000`):

```bash
# Windows (PowerShell ou cmd)
copy .env.example .env

# Linux ou macOS
cp .env.example .env
```

Instale as dependências e inicie:

```bash
npm install
npm run dev
```

Abra <http://localhost:5173>.

---

## 5. Entrar e avaliar

| Usuário | Senha | Perfil | Disponível |
|---|---|---|---|
| `admin@crmassist.dev` | `admin123` | Administrador: vê tudo, cadastra usuários e configura o RD Station | sempre |
| `gerente@crmassist.dev` | `demo1234` | Gerente: vê toda a equipe | só com os dados de teste |
| `sdr@crmassist.dev` | `demo1234` | SDR: vê só a própria carteira | só com os dados de teste |
| `closer@crmassist.dev` | `demo1234` | Closer: vê só a própria carteira | só com os dados de teste |

A senha do administrador vem do `docker-compose.yml` (`ADMIN_EMAIL` / `ADMIN_PASSWORD`).

### Um roteiro curto de avaliação (com os dados de teste)

1. **Início**, como gerente: indicadores do mês (novas oportunidades, ganhas e perdidas). Troque o período para "Este ano".
2. **Funil**: o kanban por etapa. Busque uma empresa pelo nome e use **Filtros** (responsável, situação, faixa de valor).
3. **Leads**: abra um lead e clique em **Pedir resumo ao assistente**. O chat abre com a pergunta e o ID do registro prontos.
4. **Assistente**: pergunte, por exemplo, "Quanto vendemos em outubro?", "Quem é o vendedor com melhor desempenho este ano?" ou "Por que perdemos negócios este ano?".
5. **Agenda**: os retornos de hoje e os atrasados. Registre uma anotação na tela de um lead com retorno para amanhã e veja ela aparecer na agenda do dia seguinte.
6. **Performance do time**: prospectados, qualificados e fechados por vendedor.
7. **Controle de acesso**: saia e entre como `sdr@crmassist.dev`. As telas mostram só a carteira dele e, no assistente, "Quais são as oportunidades da Tatiane Rocha?" não traz nada de outra pessoa. Depois entre como `closer@crmassist.dev`: o funil mostra só os negócios dela e a tela **Leads**, os contatos desses negócios, que ela pode abrir e anotar.

Sem a chave da Anthropic (passo 2), os itens 3 e 4 respondem em modo sem IA: as perguntas acima funcionam, com respostas em formato fixo. Com a chave, o assistente conversa livremente.

### Integração com o RD Station (opcional)

A sincronização com o RD Station CRM exige uma conta do RD Station CRM (plano Basic ou superior) e um aplicativo criado na App Store do RD. Ela pode ser configurada pela tela **Integração RD**, entrando como administrador. Rodando localmente, o RD não consegue enviar webhooks para o seu computador: o envio do CRM Assist para o RD funciona, mas a volta só funciona com a API publicada na internet. Os detalhes estão no [README do backend](backend/README.md#fluxo-da-sincronização-post-webhooksrd-station).

---

## 6. Problemas comuns

| Sintoma | Causa provável e solução |
|---|---|
| `docker compose` diz que não encontra o Docker | O Docker Desktop não está aberto. Abra e espere ele ficar pronto. |
| Erro de porta em uso (`5432` ou `8000`) | Outro programa usa a porta, geralmente um PostgreSQL instalado. Pare esse serviço ou, no `docker-compose.yml`, troque `"5432:5432"` por `"55432:5432"`. |
| `dados_demo` falha logo depois do `docker compose up` | A API ainda está subindo. Espere `Application startup complete` no log e rode de novo. |
| `A carga já está neste banco` | Os dados de teste já foram carregados. Para refazer, rode `limpar` antes. |
| "Não foi possível conectar ao servidor" ao entrar | A API não está no ar ou o arquivo `.env` do front não foi criado. Confira <http://localhost:8000/health> e o passo 4. |
| O assistente responde "(Modo sem IA)" | Falta a `ANTHROPIC_API_KEY` no `.env` do backend. Crie o arquivo e rode `docker compose up -d` de novo. |
| Quase todos os retornos da agenda aparecem atrasados | A carga foi feita há alguns dias. Atualize a agenda com o comando da tabela do passo 3. |

Para parar tudo: `docker compose down` na pasta do backend (os dados ficam guardados) e Ctrl + C no terminal do front-end.
