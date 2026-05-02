# Delega AI

**Micro-SaaS WhatsApp-first para gestão de tarefas por voz e texto.**

Transforme mensagens e áudios do WhatsApp em tarefas com prazo, responsável e acompanhamento automático — sem abrir nenhum app de gestão.

---

## O que é

O Delega AI é um agente de IA que vive dentro do WhatsApp. Gestores e equipes podem criar, delegar e acompanhar tarefas simplesmente enviando uma mensagem ou um áudio, exatamente como já fazem hoje. O agente interpreta a intenção, estrutura a tarefa no banco e notifica todos os envolvidos automaticamente.

**Proposta de valor central:**

> "Delega pelo WhatsApp como você já faz, mas agora tudo fica registrado, tem prazo e cobra sozinho."

---

## Funcionalidades implementadas

### Criação e delegação
- Criar tarefa por texto ou áudio transcrito (ex: *"João, envia a proposta da Nanocare até sexta às 18h"*)
- Delegar tarefa para qualquer membro do time cadastrado
- Definir prazo, cliente e prioridade diretamente na mensagem
- Remover prazo de uma tarefa existente
- Cancelar tarefa (apenas quem criou pode cancelar)

### Gestão de tarefas
- Editar responsável, cliente ou título de uma tarefa existente
- Reagendar prazo por comando de voz ou texto
- Marcar tarefa como iniciada ou concluída
- Consultar status de qualquer tarefa por nome, responsável ou cliente
- Renomear colaborador do time (ex: *"muda o nome do Leo para Leonardo"*)

### Respostas do responsável à tarefa recebida
- Aceitar e iniciar tarefa (ex: *"aceito, pode deixar comigo"*)
- Informar que não conseguirá realizar (ex: *"não vou conseguir fazer essa tarefa"*)
- Pedir ajuda ou informar que está travado (ex: *"preciso de ajuda com isso"*)
- Solicitar mais prazo (ex: *"preciso de mais prazo até sexta"*)
- Pedir que a tarefa seja repassada (ex: *"isso não é comigo, manda para outra pessoa"*)
- Perguntar os detalhes de uma tarefa recebida (ex: *"qual era mesmo a tarefa?"*)
- Responder diretamente a uma notificação citada — o agente identifica a tarefa pelo contexto da mensagem citada

### Listas e histórico
- *"Minhas tarefas"* — lista tarefas pendentes do usuário
- *"Tarefas de hoje"*, *"tarefas atrasadas"*, *"todas as tarefas"*
- *"Minhas tarefas concluídas"* — histórico ordenado por data de conclusão
- *"Minhas tarefas da Nanocare"* — filtro por cliente na lista pessoal
- *"Tarefas que eu deleguei"* — visão de tarefas criadas por você para o time
- Paginação automática: 10 tarefas por página, basta responder *"próximas tarefas"*

### Visão do gestor
- Resumo diário do time (tarefas abertas, atrasadas, concluídas hoje)
- Pendências por membro
- Tarefas atrasadas do time

### Notificações automáticas
- Notifica o responsável quando uma tarefa é delegada a ele
- Notifica o criador quando o responsável conclui, inicia ou reagenda
- Notifica o responsável quando o criador edita, cancela ou remove prazo
- Lembretes automáticos de prazo (1h antes, no vencimento, após atraso)
- Fila de notificações com retry automático em caso de falha de entrega

### Onboarding
- Novo usuário é identificado pelo número de telefone
- Se não tiver cadastro, o agente inicia o onboarding: coleta nome, empresa e cargo diretamente pelo WhatsApp
- Funciona sem nenhuma intervenção manual

### Outros
- Transcrição de áudio com glossário customizado (nomes do time, clientes)
- Resposta em áudio via TTS (configurável)
- Indicador "digitando..." no WhatsApp durante o processamento
- Convite de colaboradores para o workspace via WhatsApp
- Arquitetura LLM-first: OpenAI GPT-4.1-mini classifica toda intenção; parser local determinístico é usado apenas como fallback (sem chave OpenAI ou em caso de erro)

---

## Arquitetura

```
WhatsApp (usuário)
       │
       ▼
Evolution API  ──── webhook ────►  FastAPI
                                      │
                              identifica usuário
                              e empresa (DB)
                                      │
                                      ▼
                                  LangGraph
                              ┌─────────────────────────┐
                              │  parse_message           │
                              │  (LLM + fallback local)  │
                              │         │                │
                              │         ▼                │
                              │  execute_action          │
                              │  (regras de negócio)     │
                              │         │                │
                              │         ▼                │
                              │  format_reply            │
                              └─────────────────────────┘
                                      │
                              ┌───────┴────────┐
                              │                │
                        resposta ao        notificações
                        usuário            para terceiros
                              │                │
                              ▼                ▼
                        Evolution API    Evolution API
                        (send_text)      (send_text)
```

**Princípios de segurança da arquitetura:**

- O agente de IA nunca recebe credenciais, nunca gera SQL e nunca escolhe `company_id` sozinho
- O backend injeta `company_id`, `user_id`, papel do usuário e timezone após identificar o número
- Isolamento total entre empresas no banco de dados
- LLM-first: toda intenção passa pelo GPT antes de ser executada — o parser local é fallback para indisponibilidade da API

---

## Stack

| Camada | Tecnologia |
|---|---|
| WhatsApp | Evolution API |
| Backend / API | FastAPI + Uvicorn |
| Orquestração de IA | LangGraph |
| Parsing / NLP | Parser local determinístico + OpenAI GPT-4.1-mini |
| Transcrição de áudio | OpenAI Whisper (gpt-4o-mini-transcribe) |
| Banco de dados | PostgreSQL (Supabase em produção) |
| Driver banco | psycopg3 (psycopg[binary]) |
| Linguagem | Python 3.12 |
| Deploy | Docker (Railway / Render / Fly.io) |

---

## Rodando localmente

### 1. Pré-requisitos

- Python 3.11+
- Conta no Supabase (banco gratuito)
- Conta na Evolution API (para WhatsApp)
- Chave OpenAI (para NLP e transcrição)
- [ngrok](https://ngrok.com) instalado

### 2. Instalar dependências

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e .
```

### 3. Configurar variáveis de ambiente

Copie `.env.example` para `.env` e preencha:

```env
APP_ENV=local

# Banco (Supabase)
DATABASE_URL=postgresql://postgres:[senha]@[host]:5432/postgres?sslmode=require

# Evolution API
EVOLUTION_API_BASE_URL=https://sua-instancia.evolution.com
EVOLUTION_INSTANCE_NAME=Delega AI
EVOLUTION_API_KEY=sua-chave
EVOLUTION_SEND_ENABLED=true
EVOLUTION_TYPING_ENABLED=true

# OpenAI
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4.1-mini
OPENAI_TRANSCRIPTION_MODEL=gpt-4o-mini-transcribe
```

### 4. Criar o banco de dados

No **SQL Editor do Supabase**, execute o conteúdo de [`sql/schema.sql`](sql/schema.sql).

> Não é necessário rodar o `seed.sql` para testes reais. Basta iniciar o servidor e enviar uma mensagem pelo WhatsApp — o onboarding cria seu usuário e empresa automaticamente.

### 5. Iniciar o servidor

```powershell
uvicorn whatsapp_task_agent.api:app --reload --host 0.0.0.0 --port 8000
```

### 6. Expor para o WhatsApp com ngrok

Em outro terminal:

```powershell
ngrok http 8000
```

Copie a URL gerada (ex: `https://xxxx.ngrok-free.app`) e configure como webhook na Evolution API:

```
https://xxxx.ngrok-free.app/webhooks/evolution
```

### 7. Primeiro uso

Envie qualquer mensagem para o número WhatsApp vinculado à instância Evolution. O bot responderá iniciando o onboarding e perguntando seu nome, empresa e cargo.

---

## Resetar o banco (para novos testes)

### Opção A — Limpar só os dados (uso diário)

Mantém as tabelas intactas. Mais rápido, não precisa recriar nada depois.

Execute no **SQL Editor do Supabase**:

```sql
truncate table
    leads,
    onboarding_sessions,
    notification_outbox,
    pending_invite_drafts,
    pending_task_drafts,
    pending_task_choices,
    task_reminders,
    task_events,
    task_comments,
    tasks,
    invites,
    clients,
    company_members,
    whatsapp_messages,
    users,
    companies
restart identity cascade;
```

Depois é só iniciar o servidor e fazer o onboarding pelo WhatsApp normalmente.

---

### Opção B — Recriar tudo do zero (após mudança de schema)

Use quando houver novas migrations ou quiser garantir que o schema está 100% atualizado.

**Passo 1 — Remover estrutura:**
```sql
drop table if exists leads cascade;
drop table if exists onboarding_sessions cascade;
drop table if exists notification_outbox cascade;
drop table if exists pending_invite_drafts cascade;
drop table if exists pending_task_drafts cascade;
drop table if exists pending_task_choices cascade;
drop table if exists task_reminders cascade;
drop table if exists task_events cascade;
drop table if exists task_comments cascade;
drop table if exists tasks cascade;
drop table if exists invites cascade;
drop table if exists clients cascade;
drop table if exists company_members cascade;
drop table if exists users cascade;
drop table if exists companies cascade;
drop table if exists whatsapp_messages cascade;

drop type if exists app_role cascade;
drop type if exists task_status cascade;
drop type if exists task_priority cascade;
drop type if exists message_direction cascade;
drop type if exists invite_status cascade;
```

**Passo 2 — Recriar:** execute o conteúdo de [`sql/schema.sql`](sql/schema.sql).

**Passo 3 — Cadastrar-se:** envie uma mensagem pelo WhatsApp e siga o onboarding.

---

## Deploy em produção

### Opção recomendada: Railway

1. Faça push do repositório para o GitHub
2. Crie um projeto no [Railway](https://railway.app) e conecte o repositório
3. O Railway detecta o `Dockerfile` automaticamente
4. Configure as variáveis de ambiente no painel do Railway (mesmas do `.env`)
5. O Railway gera uma URL HTTPS pública — configure essa URL como webhook na Evolution API

### Opção alternativa: Render

1. Crie um serviço Web no [Render](https://render.com)
2. Conecte o repositório GitHub
3. Runtime: **Docker**
4. Configure as env vars no painel
5. Use a URL gerada como webhook

### Opção avançada: VPS com Docker

```powershell
docker build -t delega-ai .
docker run -d --env-file .env -p 8000:8000 delega-ai
```

Use Nginx como reverse proxy com SSL (Certbot/Let's Encrypt).

> Em qualquer opção de produção, o banco permanece no Supabase. Só o servidor FastAPI precisa de hospedagem.

---

## Jobs agendados

O sistema tem dois endpoints de job que precisam ser chamados periodicamente:

| Endpoint | Frequência sugerida | Função |
|---|---|---|
| `POST /jobs/reminders` | A cada 5 min | Envia lembretes de prazo (1h antes, no vencimento, após atraso) |
| `POST /jobs/notifications` | A cada 1 min | Reprocessa notificações com falha de entrega |

Configure um cron job no Railway/Render ou use qualquer scheduler externo (ex: cron-job.org).

Header obrigatório:
```
x-job-secret: [valor de REMINDER_JOB_SECRET no .env]
```

Para rodar localmente:
```powershell
$env:REMINDER_JOB_SECRET="seu-segredo"
.\scripts\run_jobs.ps1 -Secret $env:REMINDER_JOB_SECRET -Loop -IntervalSeconds 60
```

---

## Estrutura do projeto

```
delega-ai/
├── src/whatsapp_task_agent/
│   ├── api.py              # FastAPI: webhooks, jobs, roteamento
│   ├── graph.py            # LangGraph: grafo principal do agente
│   ├── reminder_graph.py   # LangGraph: grafo de lembretes
│   ├── parser.py           # Parser NLP local + integração LLM
│   ├── tools.py            # Ações de negócio (criar, completar, listar...)
│   ├── store.py            # Store in-memory (desenvolvimento)
│   ├── postgres_store.py   # Store PostgreSQL (produção)
│   ├── evolution_client.py # Cliente HTTP da Evolution API
│   ├── evolution.py        # Normalização de payloads da Evolution
│   ├── transcription.py    # Transcrição de áudio com Whisper
│   ├── tts.py              # Text-to-speech (resposta em áudio)
│   ├── schemas.py          # Modelos Pydantic
│   ├── settings.py         # Configurações via env vars
│   └── logger.py           # Logging estruturado
├── sql/
│   ├── schema.sql          # Schema completo do banco
│   ├── seed.sql            # Dados de teste (opcional)
│   └── migrations/         # Migrations incrementais
├── tests/                  # 111 testes automatizados
├── scripts/                # Utilitários locais
├── docs/
│   └── jobs.md             # Runbook dos jobs
├── Dockerfile
├── pyproject.toml
└── .env.example
```

---

## Variáveis de ambiente

| Variável | Obrigatória | Padrão | Descrição |
|---|---|---|---|
| `DATABASE_URL` | Sim (prod) | — | Connection string PostgreSQL |
| `OPENAI_API_KEY` | Sim | — | Chave OpenAI (NLP + transcrição) |
| `OPENAI_MODEL` | Não | `gpt-4.1-mini` | Modelo para parsing estruturado |
| `OPENAI_TRANSCRIPTION_MODEL` | Não | `gpt-4o-mini-transcribe` | Modelo de transcrição |
| `EVOLUTION_API_BASE_URL` | Sim (prod) | — | URL base da Evolution API |
| `EVOLUTION_INSTANCE_NAME` | Sim (prod) | — | Nome da instância WhatsApp |
| `EVOLUTION_API_KEY` | Sim (prod) | — | Chave da Evolution API |
| `EVOLUTION_SEND_ENABLED` | Não | `false` | Habilita envio real de mensagens |
| `EVOLUTION_TYPING_ENABLED` | Não | `true` | Mostra "digitando..." no WhatsApp |
| `EVOLUTION_TYPING_DELAY_MS` | Não | `1500` | Duração do typing antes da resposta |
| `EVOLUTION_TYPING_THINKING_DELAY_MS` | Não | `4000` | Duração do typing durante processamento |
| `EVOLUTION_WEBHOOK_SECRET` | Não | — | Segredo para validar webhooks recebidos |
| `REMINDER_JOB_SECRET` | Não | — | Segredo para autenticar chamadas dos jobs |
| `OBSERVE_API_KEY` | Não | — | Chave para endpoint de observabilidade |
| `APP_ENV` | Não | `local` | Ambiente (`local` / `production`) |
| `MIN_EXECUTION_CONFIDENCE` | Não | `65` | Confiança mínima para executar ação |
| `PENDING_CHOICE_TTL_MINUTES` | Não | `30` | Expiração de escolhas pendentes |
| `TTS_ENABLED` | Não | `false` | Habilita respostas em áudio |
| `QUIET_HOURS_ENABLED` | Não | `false` | Bloqueia notificações em horário silencioso |

---

## Testes

```powershell
python -m pytest tests/ -q
```

111 testes cobrindo parser, store, ferramentas, grafo e cliente Evolution.

---

## Segurança

- Todas as queries ao banco usam parâmetros (`%s`, `%(name)s`) — sem SQL injection
- `company_id` e `user_id` são sempre injetados pelo backend após identificação pelo telefone — o agente de IA nunca os escolhe
- Webhook da Evolution valida token via `hmac.compare_digest` (timing-safe)
- Variáveis sensíveis (`OPENAI_API_KEY`, `DATABASE_URL`, etc.) nunca são logadas
- `.env` está no `.gitignore` — nunca vai para o repositório
