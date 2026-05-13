# Delega AI

Delega AI é um agente WhatsApp-first para gestão de tarefas por texto e áudio. Ele transforma mensagens naturais em tarefas estruturadas, com responsável, prazo, cliente, histórico e notificações automáticas.

> Delegue pelo WhatsApp como você já faz. O sistema registra, acompanha e cobra automaticamente.

## Visão Geral

O produto foi desenhado para gestores e times que já usam WhatsApp como principal canal operacional. Em vez de exigir que a equipe abra uma ferramenta separada, o Delega AI interpreta mensagens e áudios, cria tarefas no banco, notifica os envolvidos e permite acompanhar tudo pela própria conversa.

Exemplo:

```text
João, envia a proposta da Nanocare até amanhã às 18h
```

O agente identifica:

- tarefa: `Enviar a proposta`
- responsável: `João`
- cliente: `Nanocare`
- prazo: `amanhã às 18h`
- criador: usuário que enviou a mensagem

## Funcionalidades

### Tarefas

- Criar tarefas por texto ou áudio.
- Delegar para membros do time.
- Criar tarefas para si mesmo.
- Definir prazo, cliente, descrição e prioridade pela mensagem.
- Reagendar, remover prazo, editar título, alterar responsável e alterar cliente.
- Iniciar, concluir e cancelar tarefas.
- Consultar status por tarefa, cliente ou responsável.
- Resolver ambiguidades por resposta numérica ou natural, como `a primeira`.

### Listagens

- `minhas tarefas`: tarefas atribuídas ao usuário.
- `tarefas que eu deleguei`: tarefas criadas pelo usuário para outras pessoas.
- `minhas tarefas e as que deleguei`: visão combinada em seções separadas.
- `tarefas de hoje`, `tarefas atrasadas`, `minhas tarefas concluídas`.
- Filtro por cliente, por exemplo `minhas tarefas da Nanocare`.
- Paginação automática com `próximas tarefas`.

Formato da visão combinada:

```text
📋 Suas tarefas abertas

1. Revisar contrato
   Prazo: hoje às 18:00
   Status: pendente

📤 Tarefas que você delegou

1. Enviar proposta
   Responsável: João
   Prazo: amanhã às 10:00
   Status: em andamento
```

### Colaboração

- Notifica o responsável quando uma tarefa é delegada.
- Notifica o criador quando o responsável inicia, conclui, pede ajuda, pede prazo ou informa que não conseguirá fazer.
- Notifica o responsável quando o criador edita, cancela ou remove prazo.
- Permite responder diretamente a uma mensagem citada; o agente usa o contexto da notificação para identificar a tarefa.

### Gestão do Time

- Convite de colaboradores via WhatsApp.
- Onboarding automático para novos números.
- Correção de nome de empresa, nome de contato e cargo durante fluxos guiados.
- Renomear colaboradores e atualizar cargos.
- Resumo do time, pendências por membro e tarefas atrasadas.

### Automação

- Transcrição de áudio com glossário de membros, clientes e empresa.
- Parsing LLM-first com fallback local determinístico.
- Lembretes de prazo.
- Fila de notificações com retry.
- Indicador de digitação na Evolution API.
- TTS opcional para respostas em áudio.

## Arquitetura

```text
WhatsApp
   │
   ▼
Evolution API
   │ webhook
   ▼
FastAPI
   │
   ├─ identifica usuário, empresa e papel pelo telefone
   │
   ▼
LangGraph
   │
   ├─ parse_message
   │    ├─ OpenAI, quando configurado
   │    └─ parser local como fallback
   │
   ├─ execute_action
   │    ├─ regras de permissão
   │    ├─ store PostgreSQL ou in-memory
   │    └─ notificações pendentes
   │
   ▼
Resposta ao usuário + notificações para terceiros
```

### Garantias de Segurança

- O LLM nunca recebe credenciais.
- O LLM não escolhe `company_id` nem `user_id`.
- O backend injeta contexto confiável após identificar o telefone.
- Queries são parametrizadas via psycopg.
- Dados são isolados por empresa.
- Webhooks e jobs aceitam segredo configurável.
- `.env` não deve ser versionado.

## Stack

| Camada | Tecnologia |
|---|---|
| API | FastAPI + Uvicorn |
| Orquestração | LangGraph |
| IA | OpenAI para parsing, transcrição e TTS opcional |
| WhatsApp | Evolution API |
| Banco | PostgreSQL, Supabase em produção |
| Driver | psycopg 3 + pool |
| Testes | pytest |
| Deploy | Docker |

## Requisitos

- Python 3.11+
- PostgreSQL ou Supabase para ambiente persistente
- Evolution API para testes reais via WhatsApp
- Chave OpenAI para LLM/transcrição em produção
- ngrok ou URL pública para receber webhooks localmente

Sem `DATABASE_URL`, o projeto sobe com store em memória. Isso é útil para testes rápidos de API, mas os dados somem ao reiniciar.

## Configuração Local

### 1. Instalar dependências

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -e ".[dev]"
```

### 2. Configurar ambiente

Copie `.env.example` para `.env` e preencha o que for necessário:

```env
APP_ENV=local

DATABASE_URL=postgresql://postgres:[senha]@[host]:5432/postgres?sslmode=require

OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4.1-mini
OPENAI_TRANSCRIPTION_MODEL=gpt-4o-mini-transcribe

EVOLUTION_API_BASE_URL=https://sua-instancia.evolution.com
EVOLUTION_INSTANCE_NAME=Delega AI
EVOLUTION_API_KEY=sua-chave
EVOLUTION_SEND_ENABLED=true
EVOLUTION_TYPING_ENABLED=true

REMINDER_JOB_SECRET=troque-este-valor
EVOLUTION_WEBHOOK_SECRET=troque-este-valor
OBSERVE_API_KEY=local-test
```

### 3. Criar banco

Para um banco novo, execute no SQL Editor do Supabase:

```sql
-- conteúdo de sql/schema.sql
```

Para bancos já existentes, aplique migrations incrementais:

```powershell
$env:PYTHONPATH="src"
python scripts\apply_migrations.py
```

### 4. Rodar a API

```powershell
.\.venv\Scripts\activate
uvicorn whatsapp_task_agent.api:app --reload --host 0.0.0.0 --port 8000
```

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

## Teste Pelo WhatsApp

Para testar pelo WhatsApp real, a Evolution API precisa alcançar sua máquina local.

Em um terminal, rode a API:

```powershell
uvicorn whatsapp_task_agent.api:app --reload --host 0.0.0.0 --port 8000
```

Em outro terminal:

```powershell
ngrok http 8000
```

Configure o webhook da Evolution API para:

```text
https://SEU-ID.ngrok-free.app/webhooks/evolution
```

Depois envie uma mensagem para o número conectado. Se o telefone ainda não existir no banco, o onboarding começa automaticamente.

## Teste Via API Local

O endpoint `/webhooks/whatsapp` é útil para testes sem Evolution. Ele exige `OBSERVE_API_KEY`.

```powershell
$headers = @{ "x-observe-key" = "local-test" }

$body = @{
  from_phone = "+5511999999999"
  message = "minhas tarefas"
  provider_message_id = "local-1"
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8000/webhooks/whatsapp" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $body
```

## Reset do Banco

### Limpar dados e manter schema

Use antes de testes reais ou de um novo ciclo de onboarding.

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

### Recriar tudo

Use quando houver mudança estrutural no schema.

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

Depois execute novamente `sql/schema.sql`.

## Jobs

| Endpoint | Frequência sugerida | Função |
|---|---:|---|
| `POST /jobs/reminders` | 5 min | Envia lembretes de tarefas próximas, vencidas ou atrasadas |
| `POST /jobs/notifications` | 1 min | Reprocessa notificações que falharam |

Header:

```text
x-job-secret: valor-de-REMINDER_JOB_SECRET
```

Rodando local:

```powershell
$env:REMINDER_JOB_SECRET="seu-segredo"
.\scripts\run_jobs.ps1 -Secret $env:REMINDER_JOB_SECRET -Loop -IntervalSeconds 60
```

## Deploy

O projeto está preparado para Docker. Em produção, configure as variáveis no provedor e use uma URL HTTPS pública como webhook da Evolution API.

### Railway ou Render

1. Faça push para o GitHub.
2. Crie um serviço a partir do repositório.
3. Use o `Dockerfile`.
4. Configure as variáveis de ambiente.
5. Aponte o webhook da Evolution para `https://sua-url/webhooks/evolution`.
6. Configure os jobs de lembrete/notificação.

### VPS

```powershell
docker build -t delega-ai .
docker run -d --env-file .env -p 8000:8000 delega-ai
```

Use Nginx ou Caddy como reverse proxy com TLS.

## Variáveis de Ambiente

| Variável | Obrigatória em produção | Padrão | Descrição |
|---|---|---|---|
| `APP_ENV` | Não | `local` | Ambiente da aplicação |
| `DATABASE_URL` | Sim | vazio | PostgreSQL/Supabase |
| `OPENAI_API_KEY` | Sim para LLM/audio | vazio | Chave OpenAI |
| `OPENAI_MODEL` | Não | `gpt-4.1-mini` | Modelo de parsing |
| `OPENAI_TRANSCRIPTION_MODEL` | Não | `gpt-4o-mini-transcribe` | Modelo de transcrição |
| `AUDIO_MAX_BYTES` | Não | `10485760` | Limite de áudio recebido |
| `EVOLUTION_API_BASE_URL` | Sim | vazio | URL base da Evolution API |
| `EVOLUTION_INSTANCE_NAME` | Sim | vazio | Instância conectada ao WhatsApp |
| `EVOLUTION_API_KEY` | Sim | vazio | Chave da Evolution API |
| `EVOLUTION_SEND_ENABLED` | Sim | `false` | Envia mensagens reais quando `true` |
| `EVOLUTION_TYPING_ENABLED` | Não | `true` | Habilita indicador de digitação |
| `EVOLUTION_TYPING_DELAY_MS` | Não | `500` | Delay curto de typing |
| `EVOLUTION_TYPING_THINKING_DELAY_MS` | Não | `1500` | Delay de processamento |
| `EVOLUTION_WEBHOOK_SECRET` | Recomendado | vazio | Segredo do webhook Evolution |
| `REMINDER_JOB_SECRET` | Sim para jobs | vazio | Segredo dos endpoints de job |
| `OBSERVE_API_KEY` | Recomendado | vazio | Protege endpoints de observabilidade e teste local |
| `MIN_EXECUTION_CONFIDENCE` | Não | `65` | Confiança mínima para executar ação |
| `DB_POOL_MIN_SIZE` | Não | `2` | Tamanho mínimo do pool PostgreSQL |
| `DB_POOL_MAX_SIZE` | Não | `10` | Tamanho máximo do pool PostgreSQL |
| `PENDING_CHOICE_TTL_MINUTES` | Não | `30` | Expiração de escolhas pendentes |
| `PENDING_DRAFT_TTL_HOURS` | Não | `4` | Expiração de rascunhos de tarefa |
| `PENDING_INVITE_TTL_HOURS` | Não | `24` | Expiração de rascunhos de convite |
| `OVERDUE_REMINDER_MAX_AGE_DAYS` | Não | `7` | Janela máxima para lembrete de atraso |
| `QUIET_HOURS_ENABLED` | Não | `false` | Habilita horário silencioso |
| `QUIET_HOURS_START` | Não | `22` | Início do horário silencioso |
| `QUIET_HOURS_END` | Não | `7` | Fim do horário silencioso |
| `QUIET_HOURS_TIMEZONE` | Não | `America/Sao_Paulo` | Timezone do horário silencioso |
| `TTS_ENABLED` | Não | `false` | Habilita resposta em áudio |
| `TTS_VOICE` | Não | `nova` | Voz usada no TTS |
| `CONVERSATION_HISTORY_LIMIT` | Não | `10` | Mensagens recentes usadas como contexto |

## Estrutura

```text
.
├── src/whatsapp_task_agent/
│   ├── api.py              # FastAPI, webhooks e jobs
│   ├── graph.py            # LangGraph principal
│   ├── parser.py           # Parsing LLM-first + fallback local
│   ├── tools.py            # Ações de negócio
│   ├── store.py            # Store em memória
│   ├── postgres_store.py   # Store PostgreSQL
│   ├── evolution.py        # Normalização de payloads Evolution
│   ├── evolution_client.py # Cliente Evolution API
│   ├── reminder_graph.py   # Fluxo de lembretes
│   ├── transcription.py    # Transcrição de áudio
│   ├── tts.py              # Text-to-speech
│   ├── schemas.py          # Modelos Pydantic
│   ├── settings.py         # Configuração por ambiente
│   └── logger.py           # Logging estruturado
├── sql/
│   ├── schema.sql
│   ├── seed.sql
│   └── migrations/
├── scripts/
├── tests/
├── docs/
├── Dockerfile
├── pyproject.toml
└── README.md
```

## Testes

```powershell
pytest -q
```

Status atual:

```text
139 passed
```

## Observabilidade

- `GET /health`: status básico da API.
- `GET /observe/messages`: últimas mensagens processadas, protegido por `OBSERVE_API_KEY`.
- Logs estruturados com contexto de ambiente, ação e falhas relevantes.

## Licença

Projeto privado. Uso e distribuição dependem de autorização do proprietário.
