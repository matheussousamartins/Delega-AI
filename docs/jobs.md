# Jobs do Delega AI

Este projeto tem dois jobs internos:

- `POST /jobs/reminders`: encontra tarefas perto do prazo ou atrasadas e envia lembretes.
- `POST /jobs/notifications`: reprocessa notificacoes pendentes no `notification_outbox`, com retry.

Os dois endpoints exigem o header:

```txt
x-job-secret: <REMINDER_JOB_SECRET>
```

## Variaveis

Configure no `.env`:

```env
REMINDER_JOB_SECRET=use-um-segredo-longo
EVOLUTION_SEND_ENABLED=true
```

`REMINDER_JOB_SECRET` e um segredo criado por voce. Ele nao vem de nenhum painel externo. Use um valor longo e exclusivo em producao.

## Rodar localmente

Com a API ligada em `localhost:8010`:

```powershell
$env:REMINDER_JOB_SECRET="seu-segredo-local"
.\scripts\run_jobs.ps1 -Secret $env:REMINDER_JOB_SECRET
```

Para rodar continuamente a cada 60 segundos:

```powershell
$env:REMINDER_JOB_SECRET="seu-segredo-local"
.\scripts\run_jobs.ps1 -Secret $env:REMINDER_JOB_SECRET -Loop -IntervalSeconds 60
```

Somente lembretes:

```powershell
.\scripts\run_jobs.ps1 -Secret $env:REMINDER_JOB_SECRET -RemindersOnly
```

Somente retry de notificacoes:

```powershell
.\scripts\run_jobs.ps1 -Secret $env:REMINDER_JOB_SECRET -NotificationsOnly
```

## Teste manual com curl

```powershell
curl.exe -X POST "http://localhost:8010/jobs/reminders" `
  -H "Content-Type: application/json" `
  -H "x-job-secret: seu-segredo-local" `
  -d "{}"
```

```powershell
curl.exe -X POST "http://localhost:8010/jobs/notifications" `
  -H "Content-Type: application/json" `
  -H "x-job-secret: seu-segredo-local" `
  -d "{}"
```

## Producao

No deploy, configure um scheduler externo para chamar:

```txt
POST https://sua-api.com/jobs/reminders
POST https://sua-api.com/jobs/notifications
```

Headers:

```txt
Content-Type: application/json
x-job-secret: <REMINDER_JOB_SECRET>
```

Payload:

```json
{}
```

Frequencia recomendada:

- `/jobs/reminders`: a cada 1 minuto no inicio dos testes; depois pode ir para 5 minutos se quiser reduzir chamadas.
- `/jobs/notifications`: a cada 1 minuto para retry rapido de notificacoes.

## Observacoes

- Localmente, os jobs so rodam enquanto seu servidor e o script estiverem ligados.
- Com ngrok free, a URL publica pode mudar. Por isso o cron definitivo deve ficar para o ambiente de deploy com URL fixa.
- Se `EVOLUTION_SEND_ENABLED=false`, o client de envio nao manda mensagens reais.
