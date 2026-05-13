import json
import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any
from unicodedata import normalize
from zoneinfo import ZoneInfo

from openai import OpenAI, OpenAIError
from pydantic import ValidationError

from whatsapp_task_agent.logger import get_logger
from whatsapp_task_agent.phone import normalize_phone
from whatsapp_task_agent.schemas import Action, Intent, ParsedCommand
from whatsapp_task_agent.settings import settings

logger = get_logger(__name__)

APP_TIMEZONE = ZoneInfo("America/Sao_Paulo")

_WEEKDAY_PT = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]

TASK_VERBS = [
    "abrir",
    "acompanhar",
    "adicionar",
    "ajustar",
    "ajusta",
    "alterar",
    "analisar",
    "aprovar",
    "arquivar",
    "atualizar",
    "atualiza",
    "avisar",
    "cadastrar",
    "cancelar",
    "checar",
    "cobrar",
    "conferir",
    "confere",
    "configurar",
    "confirmar",
    "corrigir",
    "criar",
    "dar",
    "de",
    "definir",
    "deletar",
    "desenvolver",
    "editar",
    "enviar",
    "envia",
    "escrever",
    "executar",
    "fazer",
    "faz",
    "fechar",
    "fecha",
    "finalizar",
    "gerar",
    "implementar",
    "iniciar",
    "integrar",
    "levantar",
    "ligar",
    "liga",
    "mandar",
    "manda",
    "marcar",
    "montar",
    "organizar",
    "olhar",
    "olhada",
    "olhadinha",
    "pagar",
    "paga",
    "preencher",
    "preparar",
    "prepara",
    "publicar",
    "reagendar",
    "remarcar",
    "remover",
    "resolver",
    "responder",
    "revisar",
    "revisa",
    "rodar",
    "subir",
    "testar",
    "validar",
    "verificar",
    "verifica",
]

PERSONAL_TASK_MARKERS = [
    "quero que voce",
    "queria que voce",
    "gostaria que voce",
    "preciso que voce",
    "pode",
    "consegue",
    "colocar na minha lista",
    "coloca na minha lista",
    "nao posso esquecer de",
    "nao posso esquecer",
    "me lembra de",
    "me lembre de",
    "lembra de",
    "lembre de",
    "salva pra mim",
    "salvar pra mim",
    "anota pra mim",
    "anotar pra mim",
    "tenho que",
    "preciso",
    "da uma olhada",
    "dar uma olhada",
    "de uma olhada",
    "da uma olhadinha",
    "dar uma olhadinha",
    "de uma olhadinha",
]

VERB_MAP = {
    "ajusta": "ajustar",
    "atualiza": "atualizar",
    "confere": "conferir",
    "envia": "enviar",
    "fecha": "fechar",
    "faz": "fazer",
    "liga": "ligar",
    "manda": "mandar",
    "paga": "pagar",
    "prepara": "preparar",
    "revisa": "revisar",
    "verifica": "verificar",
}


def _build_system_prompt(
    context: dict | None = None,
    history: list[dict] | None = None,
    team_members: list[dict] | None = None,
) -> str:
    now = datetime.now(APP_TIMEZONE)
    tomorrow = now + timedelta(days=1)

    upcoming: dict[str, str] = {}
    for d in range(1, 8):
        day = now + timedelta(days=d)
        name = _WEEKDAY_PT[day.weekday()]
        if name not in upcoming:
            upcoming[name] = day.strftime("%d/%m/%Y")

    lines = [
        "Você é o Delega AI, assistente inteligente de gestão de tarefas via WhatsApp.",
        "",
        f"DATA/HORA ATUAL: {_WEEKDAY_PT[now.weekday()]}, {now.strftime('%d/%m/%Y às %H:%M')} (horário de Brasília)",
        f"Amanhã: {tomorrow.strftime('%d/%m/%Y')} ({_WEEKDAY_PT[tomorrow.weekday()]})",
        "Próximas referências de dia da semana:",
    ]
    for name, date_str in upcoming.items():
        lines.append(f"  próxima {name} = {date_str}")
    lines.append("")

    if context:
        role_pt = {"owner": "Dono/CEO", "admin": "Administrador", "manager": "Gerente", "member": "Colaborador"}.get(
            context.get("role", "member"), "Colaborador"
        )
        lines += [
            "USUÁRIO ATUAL:",
            f"  Nome: {context['user_name']}",
            f"  Empresa: {context['company_name']}",
            f"  Papel: {role_pt}",
            "",
        ]

    if team_members:
        lines.append("MEMBROS DO TIME:")
        for m in team_members:
            role_pt = {"owner": "Dono", "admin": "Admin", "manager": "Gerente", "member": "Colaborador"}.get(
                m.get("role", "member"), "Colaborador"
            )
            lines.append(f"  - {m['name']} ({role_pt})")
        lines.append("")

    if history:
        lines.append("HISTÓRICO RECENTE DA CONVERSA:")
        for msg in history[-8:]:
            prefix = "Usuário" if msg["direction"] == "inbound" else "Delega AI"
            lines.append(f"  {prefix}: {msg['body']}")
        lines.append("")

    lines += [
        "AÇÕES DISPONÍVEIS:",
        "",
        "1. create_task — Criar nova tarefa",
        "   Params: title* (texto), due_date (ISO 8601: YYYY-MM-DDTHH:MM:SS), assignee_name, client_name, description, priority (low/medium/high)",
        "",
        "2. list_my_tasks — Listar tarefas do PRÓPRIO usuário (atribuídas a ele e/ou que ele delegou)",
        "   Params: filter (pending/today/overdue/done/all/date), date (YYYY-MM-DD quando filter=date), view (my/delegated/all), client_name",
        "   view='my' (padrão) = apenas tarefas atribuídas ao próprio usuário",
        "   view='delegated' = apenas tarefas que ele delegou ao time",
        "   view='all' = AMBAS: tarefas atribuídas a ele + tarefas que ele delegou",
        "   Triggers de view='all': 'minhas tarefas e as que deleguei', 'tudo que tenho', 'resumo geral', 'todas minhas tarefas e delegadas', 'me mostra tudo', 'tanto as minhas quanto as que passei para o time'",
        "   NUNCA use list_my_tasks para consultar tarefas de outra pessoa — use task_status com member_name",
        "",
        "3. complete_task — Concluir tarefa",
        "   Params: task_reference (título ou fragmento)",
        "",
        "4. start_task — Marcar tarefa como em andamento / aceitar tarefa recebida",
        "   Params: task_reference (título ou fragmento, opcional)",
        "   Triggers: 'iniciei', 'comecei', 'estou trabalhando em', 'em andamento', 'iniciando'",
        "   Triggers (aceite sem referência): 'ok', 'ta comigo', 'vou fazer', 'vou pedir', 'vou mandar', 'deixa comigo', 'pode contar comigo', 'beleza', 'combinado', 'perfeito', 'aceito', 'ja to nisso', 'ja comecei'",
        "",
        "5. reschedule_task — Reagendar tarefa",
        "   Params: task_reference, due_date (ISO 8601: YYYY-MM-DDTHH:MM:SS)",
        "",
        "5b. edit_task — Editar campos de uma tarefa existente (responsável, cliente ou título)",
        "   Params: task_reference*, new_assignee_name, new_client_name, new_title",
        "   Triggers: 'muda o responsável para X', 'troca o cliente para Y', 'muda o título para Z'",
        "",
        "5c. cancel_task — Cancelar/excluir tarefa (APENAS quem criou pode cancelar)",
        "   Params: task_reference*",
        "   Triggers: 'cancelar tarefa X', 'excluir tarefa X', 'apagar tarefa X', 'remover tarefa X'",
        "",
        "   NOTA: Para remover o prazo de uma tarefa, use reschedule_task com clear_due_date=true",
        "   Triggers: 'remove o prazo da tarefa X', 'tira o prazo', 'deixa sem prazo a tarefa X'",
        "",
        "6. task_status — Consultar andamento/status de tarefas por responsável, cliente ou referência",
        "   Params: task_reference, member_name, client_name, status_filter (open/done/all)",
        "   Triggers: 'Leo já fez o deploy?', 'como está a tarefa da Nanocare?', 'o que o Leo tem pendente?'",
        "",
        "7. team_summary — Visão geral do time (owners/managers)",
        "   Params: period (today/week), view (summary/team_tasks/team_overdue/team_done_today/member_tasks), member_name",
        "",
        "8. invite_user — Convidar colaborador",
        "   Params: name, phone (apenas dígitos com DDD), job_title, role (member/manager)",
        "",
        "9. edit_member — Renomear/corrigir colaborador do time ou atualizar cargo/função",
        "   Params para nome: current_name* (nome atual), new_name* (novo nome)",
        "   Params para cargo: current_name ou target_self=true, new_job_title*",
        "   Triggers: 'muda o nome do Leo para Leonardo', 'renomeia colaborador Ana para Ana Paula', 'corrige o colaborador Leo para Leonardo'",
        "   Triggers de cargo: 'alterar cargo do Leo para Desenvolvedor', 'corrigir função da Ana para Gestora Comercial', 'alterar meu cargo para CEO'",
        "",
        "10. edit_client — Renomear/corrigir cliente da empresa em todas as tarefas",
        "   Params: current_name* (nome atual), new_name* (novo nome)",
        "   Triggers: 'renomear cliente Derry para Dairy', 'corrigir cliente Derry para Dairy', 'cliente Derry agora é Dairy'",
        "",
        "11. cannot_do_task — Usuário informa que não conseguirá realizar uma tarefa",
        "   Params: task_reference (fragmento do título, opcional)",
        "   Triggers: 'não vou conseguir', 'não vai dar', 'impossível', 'não tenho como', 'não é comigo', 'não consigo fazer'",
        "",
        "12. needs_help_task — Usuário pede ajuda ou está travado em uma tarefa",
        "   Params: task_reference (opcional)",
        "   Triggers: 'preciso de ajuda', 'travei', 'não sei como fazer', 'me ajuda com isso', 'tenho dificuldade'",
        "",
        "13. reassign_task — Usuário pede para repassar/transferir uma tarefa para outra pessoa",
        "   Params: task_reference (opcional)",
        "   Triggers: 'passa para outra pessoa', 'repassa isso', 'transfere para o time', 'melhor outra pessoa', 'não é comigo isso'",
        "",
        "INSTRUÇÕES CRÍTICAS:",
        "- Você é especialista em português brasileiro informal, incluindo áudios transcritos (sem pontuação, gírias, abreviações)",
        "- SEMPRE resolva datas relativas para ISO 8601 completo usando as datas de referência acima",
        "- Se não houver horário especificado, use 09:00:00 para manhã, 18:00:00 como padrão geral",
        "- Use o histórico para resolver referências como 'aquela tarefa', 'isso', 'ela', 'o mesmo prazo'",
        "- Se intenção clara mas parâmetros opcionais ausentes: retorne o que tiver, null nos ausentes",
        "- Em create_task, due_date é opcional: se o usuário não informou prazo ou pediu 'sem prazo', retorne due_date=null; não invente prazo",
        "- Convite só é invite_user quando o usuário pedir explicitamente para convidar/adicionar/chamar uma pessoa ao workspace, ou quando o evento vier como contato compartilhado; a palavra 'contato' dentro de uma tarefa NÃO é convite",
        "- Team summary só é team_summary quando o usuário pedir explicitamente resumo/tarefas/pendências do time inteiro; a palavra 'time' dentro do título de uma tarefa NÃO é consulta",
        "- Perguntas sobre andamento/status de uma tarefa, pessoa específica ou cliente são task_status, não create_task e não team_summary",
        "- 'minhas tarefas', 'o que tenho para fazer', 'minhas pendências' = list_my_tasks com view='my' (SOMENTE tarefas atribuídas ao usuário)",
        "- 'tarefas que eu deleguei', 'o que eu passei para o time' = list_my_tasks com view='delegated'",
        "- 'minhas tarefas e as que deleguei', 'tudo que tenho', 'me mostra tudo', 'tanto as minhas quanto as delegadas', 'resumo geral das minhas tarefas' = list_my_tasks com view='all'",
        "- 'tarefas do Leo', 'o que o Leo tem?', 'pendências da Ana', 'listar tarefas do Matheus' = task_status com member_name, NUNCA list_my_tasks",
        "- Se a consulta menciona o nome de outro membro do time, use task_status com member_name — nunca list_my_tasks",
        "- Exemplos de task_status: 'Leo já fez o deploy?', 'como está a tarefa da Nanocare?', 'o que o Leo tem pendente?', 'tarefas do Leo', 'status do deploy', 'a Nanocare está pendente?'",
        "- Em task_status, use status_filter='open' para pendências e status_filter='all' para perguntas do tipo 'já fez?', 'já terminou?', 'como está?'",
        "- 'feito', 'já finalizei', 'terminei', 'concluí', 'já entreguei', 'pronto, finalizei', 'finalizei essa tarefa', 'finalizei isso', 'terminei essa', 'essa tarefa foi feita' = complete_task (NÃO start_task); task_reference vazio quando a referência for demonstrativo ('essa tarefa', 'isso', 'ela', 'esta') — deixe o sistema resolver pelo contexto da mensagem citada",
        "- Em reschedule_task, cancel_task e edit_task: task_reference VAZIO quando a referência for demonstrativo ('essa tarefa', 'isso', 'ela', 'esta', 'desta tarefa', 'aquela tarefa') — o sistema resolve pelo corpo da mensagem citada; NÃO popule task_reference pelo histórico da conversa quando o usuário usou um demonstrativo na mensagem atual",
        "- 'quais as tarefas que eu finalizei', 'o que eu terminei', 'o que eu já finalizei', 'tarefas que eu finalizei', 'me mostra o que já fiz' = list_my_tasks com filter='done'",
        "- Para nomes do time, aceite variações de transcrição próximas: 'Mateus' pode ser 'Matheus' se houver um Matheus nos membros",
        "- O responsável pode aparecer no começo, meio ou fim: 'Matheus, ...', 'quero que o Matheus ...', 'passa para o Matheus ...', 'essa tarefa é para o Matheus'; preencha assignee_name quando houver um membro claro",
        "- Se mais de um membro for mencionado e não houver responsável claro, deixe assignee_name=null e reduza a confidence",
        "- Pedidos naturais como 'quero que você dê uma olhadinha...', 'consegue ver...', 'pode checar...' são create_task quando direcionados a alguém",
        "- Se intenção ambígua ou nenhuma ação aplicável: retorne action=null com confidence baixo",
        "- 'pra mim' / 'para mim' em create_task = não há assignee_name (tarefa do próprio usuário), e NÃO é client_name",
        "- NUNCA invente dados não presentes na mensagem ou contexto",
        "- Título da tarefa: capitalize corretamente, não inclua data/hora no título",
        "- Confidence: 95=explícito e inequívoco, 85=implícito mas claro, 70=provável, 50=ambíguo",
        "",
        "FILLERS CONVERSACIONAIS EM ÁUDIOS — IGNORE COMPLETAMENTE:",
        "- Frases de confirmação no FINAL de mensagens de voz NÃO são a tarefa: 'por favor', 'tá bom?', 'ta bom?', 'ok?', 'certo?', 'né?', 'né não?', 'pode ser?', 'beleza?', 'pode?', 'tudo bem?', 'combinado?', 'tá?', 'sim?', 'correto?'",
        "- Frases introdutórias NÃO são a tarefa: 'lembra de', 'lembrar de', 'não esquece de', 'não esquece que', 'pode fazer', 'pode checar', 'preciso que você', 'quero que você'",
        "- O título deve ser APENAS a ação concreta: verbo + objeto + contexto relevante",
        "- NUNCA use o filler de confirmação como título, mesmo que seja a última coisa dita na mensagem",
        "",
        "RESOLUÇÃO DE HORÁRIOS COLOQUIAIS (muito comum em áudios):",
        "- 'fim do dia' / 'final do dia' / 'fim da tarde' → 18:00",
        "- 'de manhã' / 'pela manhã' / 'cedo' → 09:00",
        "- 'depois do almoço' / 'pós-almoço' → 14:00",
        "- 'meio-dia' / 'meio dia' → 12:00",
        "- 'à tarde' / 'de tarde' → 15:00",
        "- 'à noite' / 'de noite' → 20:00",
        "- 'daqui X horas/minutos' → use DATA/HORA ATUAL acima como base",
        "- 'semana que vem' / 'próxima semana' → segunda-feira da próxima semana, 09:00",
        "",
        "EXEMPLOS IMPORTANTES DE DESAMBIGUAÇÃO:",
        '- "Lembrar o Leo de enviar a proposta da Nanocare até amanhã" = create_task; assignee_name=Leo, client_name=Nanocare, due_date=amanhã',
        '- "Mateus, ajustar o Delega AI para receber um contato e salvar no time" = create_task; contato/time fazem parte da tarefa',
        '- "Matheus, quero que você dê uma olhadinha nas tasks da Derry para entender o que está pendente" = create_task; client_name="Derry"; due_date=null',
        '- "Leo, lembra de amanhã dar uma olhada no código da SpaceX para ver ali os bugs que eles sinalizaram, tá bom?" = create_task; title="Dar uma olhada no código da SpaceX"; assignee_name="Leo"; client_name="SpaceX"; IGNORAR "tá bom?" — é filler conversacional de confirmação, NÃO é o título',
        '- "João, pode conferir o contrato da Nanocare até sexta, certo?" = create_task; title="Conferir o contrato da Nanocare"; assignee_name="João"; client_name="Nanocare"; IGNORAR "certo?" — é filler',
        '- "Leo, na segunda, pode fazer o deploy da Nova Tech, por favor" = create_task; title="Fazer o deploy"; assignee_name="Leo"; client_name="Nova Tech"; due_date=segunda; IGNORAR "por favor"',
        '- "quero convidar um colaborador" = action=null se faltam nome/telefone; explique depois no fallback',
        '- "compartilhei um contato" em texto comum = action=null; contato compartilhado real vem pelo evento contactMessage',
        "",
        "FORMATO DE RESPOSTA — retorne APENAS este JSON (sem texto antes ou depois):",
        '{"intent": "task|query|command|other", "action": "create_task|list_my_tasks|complete_task|start_task|reschedule_task|edit_task|cancel_task|task_status|team_summary|invite_user|edit_member|edit_client|cannot_do_task|needs_help_task|reassign_task|null", "params": {}, "confidence": 0}',
        "",
        "Exemplos de params por ação:",
        'create_task: {"title": "Revisar proposta", "due_date": "2026-05-01T10:00:00", "assignee_name": "João", "client_name": "Alpha", "priority": "high"}',
        'list_my_tasks (tarefas finalizadas): {"filter": "done", "view": "my"}',
        'list_my_tasks (minhas tarefas): {"filter": "pending", "view": "my"}',
        'list_my_tasks (delegadas): {"filter": "pending", "view": "delegated"}',
        'list_my_tasks (minhas + delegadas): {"filter": "pending", "view": "all"}',
        'list_my_tasks (tarefas do Leo) — ERRADO, use task_status: {"member_name": "Leo", ...}',
        'complete_task (com referência explícita): {"task_reference": "Revisar proposta"}',
        'complete_task (feito/finalizei sem referência explícita): {}',
        'start_task: {"task_reference": "Revisar proposta"}',
        'reschedule_task: {"task_reference": "Revisar proposta", "due_date": "2026-05-04T09:00:00"}',
        'edit_task: {"task_reference": "Revisar proposta", "new_assignee_name": "Leo", "new_client_name": "Nanocare", "new_title": null}',
        'cancel_task: {"task_reference": "Revisar proposta"}',
        'reschedule_task (remover prazo): {"task_reference": "Revisar proposta", "clear_due_date": true}',
        'task_status: {"task_reference": "deploy", "member_name": "Leo", "client_name": "Nanocare", "status_filter": "open"}',
        'team_summary: {"period": "today", "view": "summary"}',
        'invite_user: {"name": "João Silva", "phone": "11999990001", "job_title": "Vendedor", "role": "member"}',
        'edit_client: {"current_name": "Derry", "new_name": "Dairy"}',
        'edit_member: {"current_name": "Leo", "new_name": "Leonardo"}',
        'edit_member: {"current_name": "Leo", "new_job_title": "Desenvolvedor"}',
        'edit_member: {"target_self": true, "new_job_title": "CEO"}',
    ]

    return "\n".join(lines)


def parse_message(
    message: str,
    context: dict | None = None,
    history: list[dict] | None = None,
    team_members: list[dict] | None = None,
) -> ParsedCommand:
    if settings.openai_api_key:
        result = _parse_with_llm_safely(message, context=context, history=history, team_members=team_members)
        if result is not None:
            return result
    local = _parse_locally(message)
    return _apply_confidence_floor(local)


def parse_due_date(message: str) -> str | None:
    return _guess_due_date(_normalize_text(message))


def _parse_with_llm(
    message: str,
    context: dict | None = None,
    history: list[dict] | None = None,
    team_members: list[dict] | None = None,
) -> ParsedCommand:
    system_prompt = _build_system_prompt(context=context, history=history, team_members=team_members)
    client = OpenAI(api_key=settings.openai_api_key)
    response = client.chat.completions.create(
        model=settings.openai_model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message},
        ],
    )
    raw = response.choices[0].message.content or "{}"
    data = json.loads(raw)
    action = data.get("action")
    if action in (None, "null", "", "none"):
        action = None
    return ParsedCommand(
        intent=data.get("intent") or "other",
        action=action,
        params=data.get("params") or {},
        confidence=int(data.get("confidence") or 0),
    )


def _parse_with_llm_safely(
    message: str,
    context: dict | None = None,
    history: list[dict] | None = None,
    team_members: list[dict] | None = None,
) -> ParsedCommand | None:
    if not settings.openai_api_key:
        return None
    try:
        result = _sanitize_llm_parsed(
            _parse_with_llm(message, context=context, history=history, team_members=team_members),
            original_message=message,
        )
        result = _fill_addressed_assignee_from_team(message, result, team_members)
        result = _fill_task_status_member_from_team(message, result, team_members)
        result = _apply_confidence_floor(result)
        logger.info("llm_parse_ok", extra={"action": result.action, "confidence": result.confidence})
        return result
    except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("llm_parse_invalid_output", extra={"exc_type": type(exc).__name__})
        return None
    except OpenAIError as exc:
        logger.error("llm_parse_openai_error", extra={"exc_type": type(exc).__name__}, exc_info=True)
        return None
    except Exception as exc:
        logger.error("llm_parse_failed", extra={"exc_type": type(exc).__name__}, exc_info=True)
        return None


def _parse_iso_or_text_date(value: str) -> str | None:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=APP_TIMEZONE)
        else:
            dt = dt.astimezone(APP_TIMEZONE)
        return dt.isoformat()
    except ValueError:
        return parse_due_date(value)

def _apply_confidence_floor(result: ParsedCommand) -> ParsedCommand:
    if result.action is not None and result.confidence < settings.min_execution_confidence:
        return ParsedCommand(intent=Intent.other, action=None, params={}, confidence=result.confidence)
    return result



def _sanitize_llm_parsed(parsed: ParsedCommand, original_message: str | None = None) -> ParsedCommand:
    if parsed.action is None:
        return ParsedCommand(intent=Intent.other, action=None, params={}, confidence=parsed.confidence)

    params = dict(parsed.params or {})
    allowed: dict[Action, set[str]] = {
        Action.create_task: {"title", "description", "assignee_name", "client_name", "due_date", "priority"},
        Action.list_my_tasks: {"filter", "date", "view", "client_name"},
        Action.complete_task: {"task_reference"},
        Action.start_task: {"task_reference"},
        Action.reschedule_task: {"task_reference", "due_date", "clear_due_date"},
        Action.edit_task: {"task_reference", "new_title", "new_assignee_name", "new_client_name"},
        Action.cancel_task: {"task_reference"},
        Action.task_status: {"task_reference", "member_name", "client_name", "status_filter"},
        Action.team_summary: {"period", "view", "member_name"},
        Action.invite_user: {"name", "phone", "job_title", "role"},
        Action.edit_member: {"current_name", "new_name", "new_job_title", "target_self"},
        Action.edit_client: {"current_name", "new_name"},
        Action.cannot_do_task: {"task_reference"},
        Action.needs_help_task: {"task_reference"},
        Action.reassign_task: {"task_reference"},
        Action.task_details: {"task_reference"},
    }
    params = {key: _clean_llm_value(value) for key, value in params.items() if key in allowed[parsed.action]}

    if parsed.action == Action.create_task:
        normalized_original = _normalize_audio_artifacts(_normalize_text(original_message or ""))
        if not params.get("client_name") and normalized_original:
            params["client_name"] = _guess_client_name(normalized_original)
        title = _normalize_task_title(_strip_trailing_filler(str(params.get("title") or "").strip()))
        if params.get("client_name"):
            title = _normalize_task_title(_remove_client_reference(title, str(params["client_name"])).strip())
        if _is_bad_task_title(title) and normalized_original:
            repaired_title = _clean_task_title(normalized_original, client_name=params.get("client_name"))
            if not _is_bad_task_title(repaired_title):
                title = repaired_title
        params["title"] = title
        if not params["title"]:
            return ParsedCommand(intent=Intent.other, action=None, params={}, confidence=20)
        if params.get("due_date"):
            params["due_date"] = _parse_iso_or_text_date(str(params["due_date"]))
        params["priority"] = _normalize_priority(params.get("priority"))

    if parsed.action == Action.reschedule_task and params.get("due_date"):
        params["due_date"] = _parse_iso_or_text_date(str(params["due_date"]))

    if parsed.action == Action.list_my_tasks:
        task_filter = str(params.get("filter") or "pending").lower()
        if task_filter not in {"today", "overdue", "all", "pending", "date", "done"}:
            task_filter = "pending"
        params["filter"] = task_filter
        view = str(params.get("view") or "my").lower()
        if view not in {"my", "delegated", "all"}:
            view = "my"
        params["view"] = view

    if parsed.action == Action.team_summary:
        view = str(params.get("view") or "summary").lower()
        if view not in {"summary", "team_tasks", "team_overdue", "team_done_today", "member_tasks"}:
            view = "summary"
        params["view"] = view
        params["period"] = str(params.get("period") or "today").lower()

    if parsed.action == Action.task_status:
        status_filter = str(params.get("status_filter") or "open").lower()
        if status_filter not in {"open", "done", "all"}:
            status_filter = "open"
        params["status_filter"] = status_filter

    # Strip demonstrative task references ("essa tarefa", "isso", etc.) so execute nodes
    # can rely on empty task_reference to trigger quoted_message_body resolution.
    if parsed.action in {
        Action.reschedule_task, Action.cancel_task, Action.edit_task,
        Action.complete_task, Action.start_task,
    }:
        params["task_reference"] = _strip_demonstrative_ref(params.get("task_reference"))

    if parsed.action == Action.invite_user and params.get("phone"):
        params["phone"] = _normalize_phone(str(params["phone"]))
    if parsed.action == Action.invite_user and not params.get("phone"):
        return ParsedCommand(intent=Intent.other, action=None, params={}, confidence=min(parsed.confidence, 40))

    return ParsedCommand(
        intent=parsed.intent,
        action=parsed.action,
        params=params,
        confidence=parsed.confidence,
    )


def _clean_llm_value(value: Any) -> Any:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned if cleaned and cleaned.lower() != "null" else None
    return value


def _fill_addressed_assignee_from_team(
    message: str,
    parsed: ParsedCommand,
    team_members: list[dict] | None,
) -> ParsedCommand:
    if parsed.action != Action.create_task or not team_members:
        return parsed

    candidate = parsed.params.get("assignee_name")
    if not candidate and "," in message:
        candidate = message.split(",", 1)[0]

    resolved_name = _resolve_team_member_name(str(candidate), team_members) if candidate else None
    if not resolved_name:
        resolved_name = _find_unique_team_member_mention(message, team_members)
    if not resolved_name:
        return parsed

    params = dict(parsed.params)
    params["assignee_name"] = resolved_name
    return ParsedCommand(
        intent=parsed.intent,
        action=parsed.action,
        params=params,
        confidence=parsed.confidence,
    )


def _fill_task_status_member_from_team(
    message: str,
    parsed: ParsedCommand,
    team_members: list[dict] | None,
) -> ParsedCommand:
    if parsed.action != Action.task_status or not team_members:
        return parsed

    params = dict(parsed.params)
    candidate = params.get("member_name")
    resolved_name = _resolve_team_member_name(str(candidate), team_members) if candidate else None
    if not resolved_name:
        resolved_name = _find_unique_team_member_mention(message, team_members)
    if not resolved_name:
        return parsed

    params["member_name"] = resolved_name
    return ParsedCommand(
        intent=parsed.intent,
        action=parsed.action,
        params=params,
        confidence=parsed.confidence,
    )


def _resolve_team_member_name(candidate: str, team_members: list[dict]) -> str | None:
    normalized_candidate = _normalize_text(candidate)
    normalized_candidate = re.sub(r"^(?:para|pra|pro|pelo|pela|com|o|a)\s+", "", normalized_candidate).strip()
    if not normalized_candidate or len(normalized_candidate.split()) > 3:
        return None

    best_name = None
    best_score = 0.0
    for member in team_members:
        name = str(member.get("name") or "")
        normalized_name = _normalize_text(name)
        first_name = normalized_name.split(" ", 1)[0]
        score = max(
            SequenceMatcher(None, normalized_candidate, normalized_name).ratio(),
            SequenceMatcher(None, normalized_candidate, first_name).ratio(),
        )
        if normalized_candidate == normalized_name or normalized_candidate == first_name:
            score = 1.0
        if score > best_score:
            best_name = name
            best_score = score

    return best_name if best_name and best_score >= 0.86 else None


def _find_unique_team_member_mention(message: str, team_members: list[dict]) -> str | None:
    normalized_message = f" {_normalize_text(message)} "
    matches: list[str] = []

    for member in team_members:
        name = str(member.get("name") or "").strip()
        normalized_name = _normalize_text(name)
        if not normalized_name:
            continue

        name_parts = [part for part in normalized_name.split() if len(part) >= 3]
        aliases = [normalized_name, *name_parts[:1]]
        if any(_contains_name_alias(normalized_message, alias) for alias in aliases):
            matches.append(name)

    unique_matches = list(dict.fromkeys(matches))
    if len(unique_matches) != 1:
        return None
    return unique_matches[0]


def _contains_name_alias(normalized_message: str, alias: str) -> bool:
    if not alias:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", normalized_message) is not None

def _normalize_priority(value: Any) -> str | None:
    if not value:
        return None
    normalized = str(value).lower().strip()
    if normalized in {"low", "baixa"}:
        return "low"
    if normalized in {"medium", "media", "média"}:
        return "medium"
    if normalized in {"high", "alta"}:
        return "high"
    return None


def _parse_locally(message: str) -> ParsedCommand:
    text = _normalize_audio_artifacts(_normalize_text(message))

    if text.startswith("convidar ") or text.startswith("convide "):
        return ParsedCommand(
            intent=Intent.command,
            action=Action.invite_user,
            params=_parse_invite_params(text),
            confidence=82,
        )

    team_query_params = _detect_team_query_params(text)
    if team_query_params is not None:
        return ParsedCommand(
            intent=Intent.query,
            action=Action.team_summary,
            params=team_query_params,
            confidence=86,
        )

    # "tarefas do Leo", "pendências da Ana", "o que o Leo tem?" → task_status with member_name
    # Must be checked BEFORE status_query_params and task_query_params to avoid misrouting
    _member_task_match = re.search(
        r"\b(?:tarefas|pendencias|pendentes|atividades)\s+d[aoe]\s+([A-Za-zÀ-ÿ]{3,})\b"
        r"|\bo\s+que\s+(?:o|a)\s+([A-Za-zÀ-ÿ]{3,})\s+tem\b"
        r"|\bpendencias\s+(?:do|da|de)\s+([A-Za-zÀ-ÿ]{3,})\b",
        text,
    )
    if _member_task_match:
        _member_name = next(g for g in _member_task_match.groups() if g).capitalize()
        return ParsedCommand(
            intent=Intent.query,
            action=Action.task_status,
            params={"member_name": _member_name, "status_filter": "open"},
            confidence=82,
        )

    status_query_params = _detect_task_status_params(text)
    if status_query_params is not None:
        return ParsedCommand(
            intent=Intent.query,
            action=Action.task_status,
            params=status_query_params,
            confidence=86,
        )

    task_query_params = _detect_task_query_params(text)
    if task_query_params is not None:
        return ParsedCommand(
            intent=Intent.query,
            action=Action.list_my_tasks,
            params=task_query_params,
            confidence=88,
        )

    lembrar_params = _detect_lembrar_task(text)
    if lembrar_params is not None:
        return ParsedCommand(
            intent=Intent.task,
            action=Action.create_task,
            params=lembrar_params,
            confidence=82,
        )

    # Received-task reply intents — must be checked before _looks_like_task to avoid
    # misrouting "preciso de ajuda" / "isso não é comigo, manda para outra pessoa" etc.
    _cannot_do_patterns = [
        r"\bnao\s+(vou|consigo|conseguirei)\b",
        r"\bnao\s+posso\s+(fazer|realizar|entregar|cumprir|completar)\b",
        r"\bnao\s+vai\s+dar\b",
        r"\bnao\s+e\s+comigo\b",
        r"\bnao\s+tenho\s+como\b",
        r"\bimpossivel\b",
    ]
    if any(re.search(p, text) for p in _cannot_do_patterns):
        return ParsedCommand(intent=Intent.command, action=Action.cannot_do_task, params={}, confidence=82)

    _needs_help_patterns = [
        r"\bpreciso\s+de\s+ajuda\b",
        r"\bme\s+ajuda\b",
        r"\btravei\b",
        r"\bnao\s+sei\s+(fazer|resolver|como)\b",
        r"\btenho\s+dificuldade\b",
    ]
    if any(re.search(p, text) for p in _needs_help_patterns):
        return ParsedCommand(intent=Intent.command, action=Action.needs_help_task, params={}, confidence=82)

    _reassign_patterns = [
        r"\bisso\s+nao\s+e\s+comigo\b",
        r"\bmanda\s+(?:para|pra|pro)\s+outra\s+pessoa\b",
        r"\brepassa\s+(?:para|pra|pro|isso)\b",
        r"\bpassa\s+(?:para|pra|pro)\s+outra\s+pessoa\b",
        r"\bmelhor\s+outra\s+pessoa\b",
    ]
    if any(re.search(p, text) for p in _reassign_patterns):
        return ParsedCommand(intent=Intent.command, action=Action.reassign_task, params={}, confidence=82)

    if _contains_any(text, ["mais prazo", "novo prazo", "outro prazo", "preciso de prazo", "preciso remarcar", "preciso reagendar"]):
        due_date = _guess_due_date(text)
        return ParsedCommand(
            intent=Intent.command,
            action=Action.reschedule_task,
            params={"due_date": due_date} if due_date else {},
            confidence=82,
        )

    _task_details_patterns = [
        r"\bqual\s+(era|e|eh)\b",
        r"\bme\s+(explica|explique|manda|mostra|mostre)\b",
        r"\bexplica\s+(melhor|essa|a)\b",
        r"\bnao\s+entendi\b",
        r"\bdetalhes\b",
        r"\btenho\s+duvida\b",
        r"\bduvida\b",
    ]
    if any(re.search(p, text) for p in _task_details_patterns):
        return ParsedCommand(intent=Intent.query, action=Action.task_details, params={}, confidence=78)

    # Pure acknowledgement (no task reference in message) → start_task without reference
    if _is_task_acknowledgement_local(text):
        ref = _extract_start_task_reference(text)
        return ParsedCommand(
            intent=Intent.command,
            action=Action.start_task,
            params={"task_reference": ref} if ref else {},
            confidence=80,
        )

    edit_task_params = _detect_edit_task_params(text)
    if edit_task_params is not None:
        return ParsedCommand(
            intent=Intent.command,
            action=Action.edit_task,
            params=edit_task_params,
            confidence=82,
        )

    edit_client_params = _detect_edit_client_params(text)
    if edit_client_params is not None:
        return ParsedCommand(
            intent=Intent.command,
            action=Action.edit_client,
            params=edit_client_params,
            confidence=85,
        )

    edit_member_job_title_params = _detect_edit_member_job_title_params(text)
    if edit_member_job_title_params is not None:
        return ParsedCommand(
            intent=Intent.command,
            action=Action.edit_member,
            params=edit_member_job_title_params,
            confidence=85,
        )

    edit_member_params = _detect_edit_member_params(text)
    if edit_member_params is not None:
        return ParsedCommand(
            intent=Intent.command,
            action=Action.edit_member,
            params=edit_member_params,
            confidence=85,
        )

    if _looks_like_task(text):
        client_name = _guess_client_name(text)
        return ParsedCommand(
            intent=Intent.task,
            action=Action.create_task,
            params={
                "title": _clean_task_title(text, client_name=client_name),
                "description": None,
                "assignee_name": _guess_assignee(text),
                "client_name": client_name,
                "due_date": _guess_due_date(text),
                "priority": _guess_priority(text),
            },
            confidence=70,
        )

    _complete_verbs = ["concluir", "conclui", "feito", "finalizar", "finalizei", "terminei", "entregue", "entregou", "entregamos", "ja fiz", "ja terminei", "ja finalizei", "ja conclui"]
    if _contains_command_word(text, _complete_verbs):
        _raw_ref = _after_any(text, _complete_verbs)
        # Demonstratives like "essa tarefa", "isso", "ela" are not real task references —
        # strip them so acknowledge_task_completion resolves via quoted message body
        _DEMONSTRATIVES = {"essa tarefa", "essa", "isso", "ela", "este", "esta", "aqui", "a tarefa"}
        _ref = _raw_ref.strip() if _raw_ref else ""
        if _ref.lower() in _DEMONSTRATIVES or not _ref:
            _ref = ""
        return ParsedCommand(
            intent=Intent.command,
            action=Action.complete_task,
            params={"task_reference": _ref} if _ref else {},
            confidence=75,
        )

    if _contains_command_word(text, ["iniciar", "iniciei", "comecei", "comecando"]) or _contains_any(text, ["em andamento", "estou trabalhando em", "iniciando"]):
        return ParsedCommand(
            intent=Intent.command,
            action=Action.start_task,
            params={"task_reference": _after_any(text, ["iniciar", "iniciei", "comecei", "comecando", "em andamento", "estou trabalhando em", "iniciando"])},
            confidence=80,
        )

    cancel_task_params = _detect_cancel_task_params(text)
    if cancel_task_params is not None:
        return ParsedCommand(
            intent=Intent.command,
            action=Action.cancel_task,
            params=cancel_task_params,
            confidence=85,
        )

    clear_due_date_params = _detect_clear_due_date_params(text)
    if clear_due_date_params is not None:
        return ParsedCommand(
            intent=Intent.command,
            action=Action.reschedule_task,
            params={**clear_due_date_params, "clear_due_date": True},
            confidence=85,
        )

    due_update_params = _detect_due_update_params(text)
    if due_update_params is not None:
        return ParsedCommand(
            intent=Intent.command,
            action=Action.reschedule_task,
            params=due_update_params,
            confidence=82,
        )

    if _contains_command_word(text, ["reagendar", "remarcar"]):
        _ref = _strip_demonstrative_ref(_after_any(text, ["reagendar", "remarcar"]))
        return ParsedCommand(
            intent=Intent.command,
            action=Action.reschedule_task,
            params={
                **( {"task_reference": _ref} if _ref else {} ),
                "due_date": _guess_due_date(text),
            },
            confidence=65,
        )

    if _contains_any(text, ["resumo", "relatorio do time", "relatorio da equipe"]):
        return ParsedCommand(
            intent=Intent.query,
            action=Action.team_summary,
            params={"period": "today", "view": "summary"},
            confidence=75,
        )

    return ParsedCommand(intent=Intent.other, action=None, params={}, confidence=20)


def _looks_like_task(text: str) -> bool:
    explicit_markers = ["criar tarefa", "nova tarefa", "delegar", "preciso que", *PERSONAL_TASK_MARKERS]
    has_due_hint = any(marker in text for marker in ["hoje", "amanha", "amanh", "ate"])
    addressed_to_person = "," in text and _has_task_verb(text)
    natural_addressed_request = "," in text and _has_natural_task_request(text)
    leading_person_task = _leading_assignee_candidate(text) is not None and _has_task_verb(text)
    return (
        any(marker in text for marker in explicit_markers)
        or addressed_to_person
        or natural_addressed_request
        or leading_person_task
        or (has_due_hint and _has_task_verb(text))
    )


def _has_task_verb(text: str) -> bool:
    return any(re.search(rf"\b{re.escape(verb)}\b", text) for verb in TASK_VERBS)


def _has_natural_task_request(text: str) -> bool:
    natural_markers = [
        "quero que voce",
        "queria que voce",
        "gostaria que voce",
        "preciso que voce",
        "pode",
        "consegue",
        "da uma olhada",
        "dar uma olhada",
        "de uma olhada",
        "da uma olhadinha",
        "dar uma olhadinha",
        "de uma olhadinha",
        "olhadinha",
    ]
    work_terms = ["task", "tasks", "tarefa", "tarefas", "pendente", "pendentes", "contrato", "proposta", "api", "cliente"]
    return _contains_any(text, natural_markers) and (_has_task_verb(text) or _contains_any(text, work_terms))


def _detect_task_query_params(text: str) -> dict | None:
    view = _detect_task_query_view(text)
    specific_date = _guess_specific_date(text)
    params: dict | None = None
    if specific_date is not None and (_looks_like_task_query(text) or view is not None):
        params = {"filter": "date", "date": specific_date.strftime("%Y-%m-%d")}

    if params is None and _contains_any(text, ["atrasada", "atrasadas", "atrasado", "atrasados", "em atraso"]):
        params = {"filter": "overdue"}

    today_terms = [
        "tarefas de hoje",
        "pra hoje",
        "para hoje",
        "vence hoje",
        "vencem hoje",
        "agenda de hoje",
        "hoje pra fazer",
        "hoje para fazer",
    ]
    if params is None and (_contains_any(text, today_terms) or (view is not None and re.search(r"\bhoje\b", text))):
        params = {"filter": "today"}

    all_terms = [
        "todas minhas tarefas",
        "todas as minhas tarefas",
        "todas tarefas",
        "todas as tarefas",
        "minhas tarefas todas",
    ]
    if params is None and _contains_any(text, all_terms):
        params = {"filter": "all"}

    done_terms = [
        "minhas tarefas concluidas",
        "tarefas que conclui",
        "tarefas que eu conclui",
        "tarefas que finalizei",
        "tarefas que eu finalizei",
        "tarefas que terminei",
        "tarefas que eu terminei",
        "tarefas que ja conclui",
        "tarefas que ja finalizei",
        "tarefas que ja terminei",
        "historico de tarefas",
        "historico das tarefas",
        "tarefas concluidas",
        "minhas concluidas",
        "o que conclui",
        "o que eu conclui",
        "o que finalizei",
        "o que eu finalizei",
        "o que terminei",
        "o que eu terminei",
        "o que ja fiz",
        "o que ja finalizei",
        "o que ja terminei",
        "tarefas feitas",
        "minhas tarefas feitas",
        "delegadas concluidas",
        "delegadas finalizadas",
        "tarefas delegadas concluidas",
        "tarefas delegadas finalizadas",
        "tarefas que deleguei concluidas",
        "tarefas que deleguei finalizadas",
        "ja conclui",
        "ja finalizei",
        "ja terminei",
    ]
    if params is None and _contains_any(text, done_terms):
        params = {"filter": "done"}

    client_filter = _detect_list_client_filter(text)
    if params is None and client_filter is not None:
        params = {"filter": "pending", "client_name": client_filter}

    pending_terms = [
        "minhas tarefas",
        "listar tarefas",
        "listar minhas tarefas",
        "mostrar minhas tarefas",
        "mostra minhas tarefas",
        "me mostra minhas tarefas",
        "minhas pendencias",
        "pendencias",
        "tarefas pendentes",
        "pendentes",
        "o que tenho",
        "o que eu tenho",
        "o que tenho pra fazer",
        "o que tenho para fazer",
        "o que eu tenho pra fazer",
        "o que eu tenho para fazer",
        "quais tarefas tenho",
        "quais tarefas eu tenho",
        "tenho algo pendente",
        "tenho alguma pendencia",
        "tenho algo pra fazer",
        "tenho algo para fazer",
    ]
    if params is None and _contains_any(text, pending_terms):
        params = {"filter": "pending"}

    if params is None and (_looks_like_task_query(text) or view is not None):
        params = {"filter": "pending"}

    if params is None:
        return None
    if view is not None:
        params["view"] = view
    return params


def _detect_task_query_view(text: str) -> str | None:
    # Combined view: user explicitly wants both their own tasks AND delegated ones.
    combined_terms = [
        "minhas tarefas e as que deleguei",
        "minhas tarefas e delegadas",
        "tarefas minhas e delegadas",
        "minhas e as delegadas",
        "tanto as minhas quanto",
        "tanto minhas quanto",
        "me mostra tudo",
        "mostra tudo",
        "tudo que tenho",
        "resumo geral das minhas",
        "minha visao geral",
        "minha visão geral",
        "visao geral das minhas tarefas",
        "visão geral das minhas tarefas",
    ]
    if _contains_any(text, combined_terms):
        return "all"

    # Also detect combined intent when both "minha"/"minhas" and delegated terms appear together.
    if re.search(r"\b(minhas?|minha)\b", text) and re.search(r"\b(delegue[io]|delegadas?|passei\s+para\s+o\s+time)\b", text):
        return "all"

    delegated_terms = [
        "tarefas que eu deleguei",
        "tarefas que deleguei",
        "o que eu deleguei",
        "o que deleguei",
        "delegadas por mim",
        "tarefas delegadas",
        "que eu criei para",
        "que criei para",
        "tarefas criadas por mim",
        "tarefas que eu criei",
    ]
    if _contains_any(text, delegated_terms):
        return "delegated"
    return None


def _detect_list_client_filter(text: str) -> str | None:
    match = re.search(
        r"\b(?:minhas\s+)?(?:tarefas|pendencias|pendentes)\s+(?:do|da|de|para)\s+(?:cliente\s+)?([a-z][a-z0-9 -]{1,40})$"
        r"|\btarefas\s+(?:que\s+(?:eu\s+)?deleguei|delegadas?)\s+(?:do|da|de|para)\s+(?:cliente\s+)?([a-z][a-z0-9 -]{1,40})$",
        text,
    )
    if not match:
        return None
    candidate = next(group for group in match.groups() if group).strip()
    if candidate in {"time", "equipe", "hoje", "amanha", "semana", "todos", "todas"}:
        return None
    return _title_ascii_name(candidate)


def _detect_lembrar_task(text: str) -> dict | None:
    match = re.search(
        r"\blembrar\s+(?:o|a)\s+([a-z][a-z0-9]{1,20})\s+de\s+(.+)$",
        text,
    )
    if not match:
        return None
    assignee_name = _title_ascii_name(match.group(1).strip())
    task_body = match.group(2).strip()
    due_date = _guess_due_date(task_body)
    client_name = _guess_client_name(task_body)
    title = _clean_task_title(task_body, client_name=client_name)
    return {
        "title": title or _normalize_task_title(task_body),
        "description": None,
        "assignee_name": assignee_name,
        "client_name": client_name,
        "due_date": due_date,
        "priority": _guess_priority(task_body),
    }


def _detect_team_query_params(text: str) -> dict | None:
    if _contains_any(text, ["atrasadas do time", "atrasados do time", "tarefas atrasadas do time"]):
        return {"period": "today", "view": "team_overdue"}

    if _contains_any(
        text,
        [
            "concluidas hoje",
            "concluidos hoje",
            "feitas hoje",
            "finalizadas hoje",
            "concluidas do time",
            "concluidos do time",
            "tarefas concluidas do time",
            "tarefas feitas do time",
        ],
    ):
        return {"period": "today", "view": "team_done_today"}

    if _contains_any(text, ["tarefas do time", "tarefas da equipe", "pendencias do time"]):
        return {"period": "today", "view": "team_tasks"}

    if _contains_any(text, ["resumo do time", "resumo time", "resumo da equipe"]):
        return {"period": "today", "view": "summary"}

    return None


def _detect_cancel_task_params(text: str) -> dict | None:
    cancel_verbs = [
        "cancelar tarefa",
        "cancela tarefa",
        "cancelar a tarefa",
        "cancela a tarefa",
        "excluir tarefa",
        "exclui tarefa",
        "excluir a tarefa",
        "exclui a tarefa",
        "apagar tarefa",
        "apaga tarefa",
        "apagar a tarefa",
        "apaga a tarefa",
        "remover tarefa",
        "remove tarefa",
        "remover a tarefa",
        "remove a tarefa",
        "deletar tarefa",
        "deleta tarefa",
        "deletar a tarefa",
        "deleta a tarefa",
        "arquivar tarefa",
        "arquiva tarefa",
    ]
    matched_marker = next((m for m in cancel_verbs if m in text), None)
    if not matched_marker:
        return None
    reference = text.split(matched_marker, 1)[1].strip(" .,;:")
    task_ref = _strip_demonstrative_ref(_normalize_task_title(reference) if len(reference) >= 3 else None)
    return {"task_reference": task_ref}


def _detect_clear_due_date_params(text: str) -> dict | None:
    clear_markers = [
        "remove o prazo",
        "remover o prazo",
        "remova o prazo",
        "tira o prazo",
        "tirar o prazo",
        "tire o prazo",
        "apaga o prazo",
        "apagar o prazo",
        "limpa o prazo",
        "limpar o prazo",
        "deixa sem prazo",
        "deixar sem prazo",
        "coloca sem prazo",
        "colocar sem prazo",
    ]
    matched_marker = next((m for m in clear_markers if m in text), None)
    if not matched_marker:
        return None
    remainder = text.split(matched_marker, 1)[1].strip()
    # strip prepositions before the task reference
    remainder = re.sub(r"^(?:da|do|de|na|no)\s+(?:tarefa\s+)?", "", remainder).strip(" .,;:")
    reference = _strip_demonstrative_ref(_normalize_task_title(remainder) if len(remainder) >= 3 else None)
    return {"task_reference": reference}


def _detect_edit_member_params(text: str) -> dict | None:
    patterns = [
        r"\b(?:muda|mudar|altera|alterar|atualiza|atualizar|renomeia|renomear|troca|trocar|corrige|corrigir)\s+o\s+nome\s+d[oa]\s+([a-z][a-z0-9 ]{1,30})\s+(?:para|pra|pro)\s+([a-z][a-z0-9 ]{1,40})",
        r"\b(?:muda|mudar|altera|alterar)\s+nome\s+d[oa]\s+([a-z][a-z0-9 ]{1,30})\s+(?:para|pra|pro)\s+([a-z][a-z0-9 ]{1,40})",
        r"\b(?:renomeia|renomear|corrige|corrigir|muda|mudar|altera|alterar)\s+(?:o\s+)?(?:colaborador|membro|responsavel|responsável)\s+([a-z][a-z0-9 ]{1,30})\s+(?:para|pra|pro)\s+([a-z][a-z0-9 ]{1,40})",
        r"\b([a-z][a-z0-9 ]{1,30})\s+(?:na\s+verdade\s+se\s+chama|agora\s+se\s+chama|agora\s+e|agora\s+é)\s+([a-z][a-z0-9 ]{1,40})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            current_name = _title_ascii_name(match.group(1).strip())
            new_name = _title_ascii_name(match.group(2).strip())
            if current_name and new_name and current_name != new_name:
                return {"current_name": current_name, "new_name": new_name}
    return None


def _detect_edit_member_job_title_params(text: str) -> dict | None:
    self_patterns = [
        r"\b(?:muda|mudar|altera|alterar|atualiza|atualizar|corrige|corrigir|troca|trocar)\s+(?:o\s+)?(?:meu|minha)\s+(?:cargo|funcao|função|perfil)\s+(?:para|pra|pro)\s+(.+)$",
        r"\b(?:meu|minha)\s+(?:cargo|funcao|função|perfil)\s+(?:agora\s+e|agora\s+é|na\s+verdade\s+e|na\s+verdade\s+é|e|é)\s+(.+)$",
    ]
    for pattern in self_patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        new_job_title = _clean_job_title_update_value(match.group(1))
        if new_job_title:
            return {"target_self": True, "new_job_title": new_job_title}

    member_patterns = [
        r"\b(?:muda|mudar|altera|alterar|atualiza|atualizar|corrige|corrigir|troca|trocar)\s+(?:o\s+)?(?:nome\s+d[oa]\s+)?(?:cargo|funcao|função|perfil)\s+d[oa]\s+([a-z][a-z0-9 ]{1,30})\s+(?:para|pra|pro)\s+(.+)$",
        r"\b(?:cargo|funcao|função|perfil)\s+d[oa]\s+([a-z][a-z0-9 ]{1,30})\s+(?:agora\s+e|agora\s+é|na\s+verdade\s+e|na\s+verdade\s+é|e|é)\s+(.+)$",
    ]
    for pattern in member_patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        current_name = _title_ascii_name(match.group(1).strip())
        new_job_title = _clean_job_title_update_value(match.group(2))
        if current_name and new_job_title:
            return {"current_name": current_name, "new_job_title": new_job_title}
    return None


def _clean_job_title_update_value(value: str) -> str | None:
    cleaned = value.strip(" .,:;!?")
    cleaned = re.sub(r"\b(?:ok|certo|por favor|pfv|beleza|ta bom|tá bom)\b.*$", "", cleaned).strip(" .,:;!?")
    if len(cleaned) < 2 or len(cleaned) > 60:
        return None
    if _normalize_text(cleaned) in {"cargo", "funcao", "função", "perfil", "ok", "sim"}:
        return None
    return _title_job_title(cleaned)


def _title_job_title(value: str) -> str:
    acronyms = {"ceo", "cto", "cfo", "coo", "ia", "ai", "rh", "ti", "ux", "ui"}
    lowercase_words = {"de", "da", "do", "das", "dos", "e"}
    words = []
    for index, part in enumerate(value.split()):
        normalized = _normalize_text(part)
        if normalized in acronyms:
            words.append(part.upper())
        elif index > 0 and normalized in lowercase_words:
            words.append(normalized)
        else:
            words.append(part[:1].upper() + part[1:])
    return " ".join(words)


def _detect_edit_client_params(text: str) -> dict | None:
    patterns = [
        r"\b(?:renomeia|renomear|corrige|corrigir|muda|mudar|altera|alterar|troca|trocar)\s+(?:o\s+)?(?:nome\s+d[oa]\s+)?cliente\s+([a-z][a-z0-9 ]{1,40})\s+(?:para|pra|pro)\s+([a-z][a-z0-9 ]{1,40})",
        r"\bcliente\s+([a-z][a-z0-9 ]{1,40})\s+(?:agora\s+e|agora\s+é|na\s+verdade\s+e|na\s+verdade\s+é|se\s+chama)\s+([a-z][a-z0-9 ]{1,40})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            raw_current_name = match.group(1).strip()
            if re.search(r"\btarefa\b", raw_current_name):
                continue
            current_name = _title_ascii_name(raw_current_name)
            new_name = _title_ascii_name(match.group(2).strip())
            if current_name and new_name and current_name != new_name:
                return {"current_name": current_name, "new_name": new_name}
    return None


def _detect_edit_task_params(text: str) -> dict | None:
    assignee_match = re.search(
        r"\b(?:muda|mudar|troca|trocar|altera|alterar|passa|transfere)\s+o\s+(?:responsavel|responsável|dono)\s+(?:para|pra|pro)\s+([a-z][a-z0-9 ]{1,40})(?:\s|$)",
        text,
    )
    if assignee_match:
        new_assignee = _title_ascii_name(assignee_match.group(1).strip())
        reference = text[: assignee_match.start()].strip()
        return {
            "task_reference": _normalize_task_title(reference) if len(reference) >= 3 else None,
            "new_assignee_name": new_assignee,
        }

    client_match = re.search(
        r"\b(?:muda|mudar|troca|trocar|altera|alterar)\s+o\s+cliente\s+(?:para|pra|pro)\s+([a-z][a-z0-9 ]{1,40})(?:\s|$)",
        text,
    )
    if client_match:
        new_client = _title_ascii_name(client_match.group(1).strip())
        reference = text[: client_match.start()].strip()
        return {
            "task_reference": _normalize_task_title(reference) if len(reference) >= 3 else None,
            "new_client_name": new_client,
        }

    client_for_task_match = re.search(
        r"\b(?:muda|mudar|troca|trocar|altera|alterar)\s+o\s+cliente\s+d[aeo]?\s+tarefa\s+(.+?)\s+(?:para|pra|pro)\s+([a-z][a-z0-9 ]{1,40})(?:\s|$)",
        text,
    )
    if client_for_task_match:
        return {
            "task_reference": _normalize_task_title(client_for_task_match.group(1).strip()),
            "new_client_name": _title_ascii_name(client_for_task_match.group(2).strip()),
        }

    title_match = re.search(
        r"\b(?:muda|mudar|renomeia|renomear|troca|trocar)\s+o\s+(?:titulo|título|nome)\s+(?:para|pra|pro)\s+(.+)$",
        text,
    )
    if title_match:
        new_title = _normalize_task_title(title_match.group(1).strip())
        reference = text[: title_match.start()].strip()
        return {
            "task_reference": _normalize_task_title(reference) if len(reference) >= 3 else None,
            "new_title": new_title,
        }

    return None


def _detect_due_update_params(text: str) -> dict | None:
    markers = [
        "coloca prazo",
        "colocar prazo",
        "adiciona prazo",
        "adicionar prazo",
        "define prazo",
        "definir prazo",
        "bota prazo",
        "botar prazo",
        "poe prazo",
        "põe prazo",
        "muda o prazo",
        "mudar o prazo",
        "troca o prazo",
        "trocar o prazo",
    ]
    if not _contains_any(text, markers):
        return None
    due_date = _guess_due_date(text)
    if not due_date:
        return {"task_reference": None, "due_date": None}

    reference = text
    for marker in markers:
        reference = reference.replace(marker, " ")
    reference = _strip_due_and_priority(reference)
    reference = re.sub(r"\b(?:nessa|nesta|nessa tarefa|nesta tarefa|para|pra|pro|a|o|da|do|de)\b", " ", reference)
    reference = re.sub(r"\s+", " ", reference).strip(" .,;:")
    task_ref = _strip_demonstrative_ref(_normalize_task_title(reference) if len(reference) >= 3 else None)
    return {
        "task_reference": task_ref,
        "due_date": due_date,
    }


def _detect_task_status_params(text: str) -> dict | None:
    if _looks_like_task(text):
        return None

    # "tarefas do Leo" / "tarefas da Ana" — list tasks by member using fuzzy match
    member_task_match = re.search(r"\btarefas d[ao]\s+([a-z][a-z0-9 ]{1,40})$", text)
    if member_task_match:
        member_name = member_task_match.group(1).strip()
        if member_name not in {"time", "equipe"}:
            return {"member_name": _title_ascii_name(member_name), "status_filter": "open"}

    status_markers = [
        "status",
        "andamento",
        "como esta",
        "como ta",
        "como ficou",
        "ja fez",
        "ja terminou",
        "ja concluiu",
        "foi feito",
        "foi concluido",
        "esta pronto",
        "ta pronto",
        "pendente",
        "pendentes",
        "pendencia",
        "pendencias",
    ]
    if not _contains_any(text, status_markers):
        return None

    if _looks_like_task_query(text) and not _contains_any(text, ["d[ao]", "do ", "da ", "de "]):
        return None

    params: dict[str, str] = {"status_filter": "open"}
    if _contains_any(text, ["concluido", "concluida", "concluidos", "concluidas", "feito", "feita", "ja fez", "ja terminou", "ja concluiu"]):
        params["status_filter"] = "all"

    client_name = _guess_client_name(text) or _extract_status_client_name(text)
    if client_name:
        params["client_name"] = client_name

    member_name = _extract_status_member_name(text)
    if member_name and _normalize_text(member_name) != _normalize_text(client_name or ""):
        params["member_name"] = member_name

    task_reference = _extract_status_task_reference(text, member_name=member_name, client_name=client_name)
    if task_reference:
        params["task_reference"] = task_reference

    if len(params) == 1 and not _contains_any(text, ["tarefa", "tarefas", "deploy", "cliente"]):
        return None
    return params


def _extract_status_member_name(text: str) -> str | None:
    patterns = [
        r"\bo que (?:o|a)?\s*([a-z][a-z0-9 ]{1,30})\s+tem\s+pendente",
        r"\b(?:do|da|de)\s+([a-z][a-z0-9 ]{1,30})\s+(?:tem|esta|ta|ficou|pendente)",
        r"\b([a-z][a-z0-9]{2,20})\s+ja\s+(?:fez|terminou|concluiu)\b",
        r"\bcomo\s+(?:esta|ta)\s+(?:o|a)\s+([a-z][a-z0-9 ]{1,30})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        candidate = _clean_status_entity(match.group(1))
        if candidate and candidate not in {"tarefa", "tarefas", "cliente", "status"}:
            return _title_ascii_name(candidate)
    return None


def _extract_status_client_name(text: str) -> str | None:
    patterns = [
        r"\btarefa\s+(?:do|da|de)\s+([a-z][a-z0-9 ]{1,40})\b",
        r"\bcliente\s+([a-z][a-z0-9 ]{1,40})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        candidate = re.split(r"\b(?:esta|ta|pendente|concluida|concluido|feita|feito)\b", match.group(1), maxsplit=1)[0]
        candidate = _clean_status_entity(candidate)
        if candidate:
            return _title_ascii_name(candidate)
    return None


def _extract_status_task_reference(text: str, member_name: str | None = None, client_name: str | None = None) -> str | None:
    cleaned = text
    for marker in [
        "qual o status de",
        "qual status de",
        "status de",
        "status do",
        "status da",
        "como esta a tarefa",
        "como ta a tarefa",
        "como esta",
        "como ta",
        "ja fez",
        "ja terminou",
        "ja concluiu",
        "foi feito",
        "foi concluido",
        "o que",
        "tem pendente",
        "pendente",
        "pendentes",
    ]:
        cleaned = cleaned.replace(marker, " ")

    for value in [member_name, client_name]:
        if value:
            cleaned = re.sub(rf"\b{re.escape(_normalize_text(value))}\b", " ", cleaned)

    cleaned = re.sub(r"\b(?:o|a|os|as|do|da|de|dos|das|no|na|nos|nas|para|pra|pro|cliente|tarefa|tarefas)\b", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ?.!,")
    if len(cleaned) < 3:
        return None
    return _normalize_task_title(cleaned)


def _clean_status_entity(value: str) -> str:
    cleaned = re.sub(r"\b(?:o|a|os|as|do|da|de|dos|das|no|na|nos|nas)\b", " ", value)
    cleaned = re.split(r"\b(?:tem|esta|ta|ficou|pendente|pendentes|tarefa|tarefas|cliente)\b", cleaned, maxsplit=1)[0]
    return re.sub(r"\s+", " ", cleaned).strip()


def _looks_like_task_query(text: str) -> bool:
    query_words = ["quais", "mostrar", "mostra", "listar", "ver", "consultar", "tenho", "minhas"]
    task_words = ["tarefa", "tarefas", "pendencia", "pendencias", "pendente", "pendentes"]
    return _contains_any(text, query_words) and _contains_any(text, task_words)


def _parse_invite_params(text: str) -> dict:
    body = _after_any(text, ["convidar", "convide"]).strip()
    job_title = None
    if " como " in body:
        body, job_title = body.split(" como ", 1)
        job_title = _title_ascii_name(job_title.strip())

    phone_match = re.search(r"\+?\d[\d\s().-]{8,}\d", body)
    phone = _normalize_phone(phone_match.group(0)) if phone_match else None
    name = body
    if phone_match:
        name = (body[: phone_match.start()] + body[phone_match.end() :]).strip()

    return {
        "name": _title_ascii_name(name.strip()) if name.strip() else None,
        "phone": phone,
        "job_title": job_title,
        "role": "member",
    }


def _after_any(text: str, markers: list[str]) -> str:
    for marker in markers:
        if marker in text:
            return text.split(marker, 1)[1].strip()
    return text


def _contains_any(text: str, terms: list[str]) -> bool:
    return any(term in text for term in terms)


def _contains_command_word(text: str, terms: list[str]) -> bool:
    return any(re.search(rf"\b{re.escape(term)}\b", text) for term in terms)


def _clean_task_title(text: str, client_name: str | None = None) -> str:
    title = text
    if "," in title:
        title = title.split(",", 1)[1]
    else:
        leading_assignee = _leading_assignee_candidate(title)
        if leading_assignee:
            title = title[len(leading_assignee) :].strip()
    title = _strip_leading_schedule_clause(title)
    title = _strip_leading_request_prefix(title)
    title = _after_any(
        title,
        [
            "criar tarefa",
            "nova tarefa",
            "delegar",
            "quero que voce",
            "queria que voce",
            "gostaria que voce",
            "preciso que voce",
            *PERSONAL_TASK_MARKERS,
        ],
    )
    if client_name:
        title = _remove_client_reference(title, client_name)
    title = _split_before_first_marker(title, [
        " amanha",
        " amanh",
        " hoje",
        " ate ",
        " segunda",
        " terca",
        " quarta",
        " quinta",
        " sexta",
        " sabado",
        " domingo",
        " prioridade",
        " as ",
        " fim do dia",
        " final do dia",
        " de manha",
        " pela manha",
        " de tarde",
        " de noite",
        " depois do almoco",
        " depois do almoço",
        " meio dia",
        " semana que vem",
        " proxima semana",
        " fim de semana",
        " daqui ",
    ])
    title = title.strip(" ,.")
    title = _strip_task_pronoun(title)
    title = _strip_trailing_filler(title)
    return _normalize_task_title(title) or text


def _guess_client_name(text: str) -> str | None:
    body = text.split(",", 1)[1] if "," in text else text
    body = _strip_leading_schedule_clause(body)
    body = _strip_leading_request_prefix(body)
    body = _strip_trailing_filler(body)
    body = _strip_due_and_priority(body)

    patterns = [
        r"\b(?:site|app|sistema|contrato|proposta|fluxos|prazos|tarefas|deploy)\s+(?:da|do|de|no|na)\s+([a-z0-9][a-z0-9 -]{1,40})$",
        r"\b(?:tasks|task)\s+(?:da|do|de|no|na)\s+([a-z0-9][a-z0-9-]{1,40})(?:\s+para\b.*)?$",
        r"\b(?:cliente|client)\s+([a-z0-9][a-z0-9 -]{1,40})$",
        r"\b(?:da|do|de|no|na)\s+([a-z0-9][a-z0-9-]{1,40})(?:\s+no\s+n8n)?$",
    ]
    for pattern in patterns:
        match = re.search(pattern, body.strip())
        if not match:
            continue
        candidate = _clean_client_candidate(match.group(1))
        if candidate:
            return _title_ascii_name(candidate)
    return None


def _strip_due_and_priority(value: str) -> str:
    text = _split_before_first_marker(value, [
        " amanha",
        " amanh",
        " hoje",
        " ate ",
        " segunda",
        " terca",
        " quarta",
        " quinta",
        " sexta",
        " sabado",
        " domingo",
        " prioridade",
        " fim do dia",
        " final do dia",
        " de manha",
        " pela manha",
        " de tarde",
        " de noite",
        " depois do almoco",
        " depois do almoço",
        " meio dia",
        " semana que vem",
        " proxima semana",
        " fim de semana",
        " daqui ",
    ])
    return text.strip(" ,.")


def _split_before_first_marker(value: str, markers: list[str]) -> str:
    positions = [value.find(marker) for marker in markers if marker in value]
    if not positions:
        return value
    return value[: min(positions)]


def _strip_leading_schedule_clause(value: str) -> str:
    weekdays = "segunda(?:-feira)?|terca(?:-feira)?|quarta(?:-feira)?|quinta(?:-feira)?|sexta(?:-feira)?|sabado|domingo"
    patterns = [
        rf"^\s*(?:na|no|nesta|nesse|em)?\s*(?:{weekdays})(?:\s+(?:de\s+manha|pela\s+manha|de\s+tarde|a\s+tarde|de\s+noite|as\s+\d{{1,2}}(?::\d{{2}})?))?\s*,?\s*",
        r"^\s*(?:amanha|hoje)(?:\s+(?:de\s+manha|pela\s+manha|de\s+tarde|a\s+tarde|de\s+noite|as\s+\d{1,2}(?::\d{2})?))?\s*,?\s*",
    ]
    cleaned = value
    for pattern in patterns:
        cleaned = re.sub(pattern, "", cleaned, count=1, flags=re.IGNORECASE).strip(" ,.")
    return cleaned


def _strip_leading_request_prefix(value: str) -> str:
    patterns = [
        r"^\s*por\s+favor\s*,?\s*",
        r"^\s*favor\s*,?\s*",
        r"^\s*(?:pode|consegue|conseguiria|poderia)\s+",
    ]
    cleaned = value
    for pattern in patterns:
        cleaned = re.sub(pattern, "", cleaned, count=1, flags=re.IGNORECASE).strip(" ,.")
    return cleaned


def _clean_client_candidate(value: str) -> str:
    candidate = value.strip(" .,;:")
    candidate = re.split(r"\s+(?:para|pra)\s+", candidate, maxsplit=1)[0].strip(" .,;:")
    stop_suffixes = [" no n8n", " na plataforma", " no site", " no app"]
    for suffix in stop_suffixes:
        if candidate.endswith(suffix):
            candidate = candidate[: -len(suffix)]
    generic = {"site", "app", "sistema", "contrato", "proposta", "fluxos", "prazos", "tarefas", "time", "equipe"}
    if not candidate or candidate in generic or candidate[0].isdigit():
        return ""
    return candidate


def _remove_client_reference(title: str, client_name: str) -> str:
    normalized_client = re.escape(client_name.lower())
    patterns = [
        rf"\s+(?:para|pra)\s+(?:a|o)?\s*{normalized_client}\b",
        rf"\s+(?:cliente|client)\s+{normalized_client}\b",
        rf"\s+(?:da|do|de|no|na)\s+{normalized_client}\b",
    ]
    cleaned = title
    for pattern in patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    return cleaned


def _normalize_task_title(title: str) -> str:
    words = title.split()
    if not words:
        return ""

    first_word = VERB_MAP.get(words[0], words[0])
    normalized = " ".join([first_word, *words[1:]])
    return normalized[:1].upper() + normalized[1:]


def _guess_assignee(text: str) -> str | None:
    if any(text.startswith(marker) for marker in PERSONAL_TASK_MARKERS):
        return None
    if "," in text:
        possible_name = text.split(",", 1)[0].strip()
        if possible_name and len(possible_name.split()) <= 2:
            return _title_ascii_name(possible_name)
    leading_assignee = _leading_assignee_candidate(text)
    if leading_assignee:
        return _title_ascii_name(leading_assignee)
    if " para " not in text:
        return None
    after_para = text.rsplit(" para ", 1)[1]
    if after_para.startswith(("cliente ", "client ", "a cliente ", "o cliente ")):
        return None
    return _title_ascii_name(after_para.split(" ", 1)[0].strip(" ,."))


def _guess_priority(text: str) -> str | None:
    if "alta" in text or "high" in text:
        return "high"
    if "baixa" in text or "low" in text:
        return "low"
    if "media" in text or "medi" in text:
        return "medium"
    return None


def _guess_due_date(text: str) -> str | None:
    now = datetime.now(APP_TIMEZONE)

    relative_datetime = _guess_relative_datetime(text, now)
    if relative_datetime is not None:
        return relative_datetime.replace(second=0, microsecond=0).isoformat()

    base_date = None
    if "amanha" in text or "amanh" in text:
        base_date = now + timedelta(days=1)
    elif "hoje" in text:
        base_date = now
    else:
        base_date = _guess_specific_date(text, now) or _guess_weekday_date(text, now) or _guess_relative_date(text, now)

    if base_date is None:
        return None

    hour, minute = _guess_time(text)
    return base_date.replace(hour=hour, minute=minute, second=0, microsecond=0).isoformat()


def _guess_specific_date(text: str, now: datetime | None = None) -> datetime | None:
    match = re.search(r"\b(?:dia\s*)?(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", text)
    if not match:
        return None

    if now is None:
        now = datetime.now(APP_TIMEZONE)

    day = int(match.group(1))
    month = int(match.group(2))
    year_text = match.group(3)
    if year_text:
        year = int(year_text)
        if year < 100:
            year += 2000
    else:
        year = now.year

    try:
        candidate = datetime(year, month, day, tzinfo=APP_TIMEZONE)
    except ValueError:
        return None

    if not year_text and candidate.date() < now.date():
        try:
            candidate = datetime(year + 1, month, day, tzinfo=APP_TIMEZONE)
        except ValueError:
            return None
    return candidate


def _guess_weekday_date(text: str, now: datetime | None = None) -> datetime | None:
    weekdays = {
        "segunda": 0,
        "segunda-feira": 0,
        "terca": 1,
        "terca-feira": 1,
        "quarta": 2,
        "quarta-feira": 2,
        "quinta": 3,
        "quinta-feira": 3,
        "sexta": 4,
        "sexta-feira": 4,
        "sabado": 5,
        "domingo": 6,
    }
    target = next((value for name, value in weekdays.items() if name in text), None)
    if target is None:
        return None

    if now is None:
        now = datetime.now(APP_TIMEZONE)
    days_ahead = (target - now.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return now + timedelta(days=days_ahead)


def _guess_relative_date(text: str, now: datetime | None = None) -> datetime | None:
    if now is None:
        now = datetime.now(APP_TIMEZONE)

    if "semana que vem" in text or "proxima semana" in text:
        days_to_monday = (7 - now.weekday()) % 7 or 7
        return now + timedelta(days=days_to_monday)

    if "fim de semana" in text or "fds" in text:
        days_to_saturday = (5 - now.weekday()) % 7 or 7
        return now + timedelta(days=days_to_saturday)

    match = re.search(r"daqui\s+(\d+|dois|duas|tres|quatro|cinco|seis|sete)\s+dias?", text)
    if match:
        raw = match.group(1)
        num_map = {"dois": 2, "duas": 2, "tres": 3, "quatro": 4, "cinco": 5, "seis": 6, "sete": 7}
        days = int(raw) if raw.isdigit() else num_map.get(raw, 0)
        if days > 0:
            return now + timedelta(days=days)
    return None


def _guess_relative_datetime(text: str, now: datetime | None = None) -> datetime | None:
    if now is None:
        now = datetime.now(APP_TIMEZONE)

    patterns = [
        (r"daqui\s+(\d+|uma|um|dois|duas|tres|quatro|cinco|seis)\s+horas?", "hours"),
        (r"em\s+(\d+|uma|um|dois|duas|tres)\s+horas?", "hours"),
        (r"daqui\s+(\d+|quinze|vinte|trinta|quarenta)\s+minutos?", "minutes"),
        (r"em\s+(\d+|quinze|vinte|trinta|quarenta)\s+minutos?", "minutes"),
    ]
    num_map = {
        "uma": 1,
        "um": 1,
        "dois": 2,
        "duas": 2,
        "tres": 3,
        "quatro": 4,
        "cinco": 5,
        "seis": 6,
        "quinze": 15,
        "vinte": 20,
        "trinta": 30,
        "quarenta": 40,
    }
    for pattern, unit in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        raw = match.group(1)
        amount = int(raw) if raw.isdigit() else num_map.get(raw, 0)
        if amount <= 0:
            continue
        return now + (timedelta(hours=amount) if unit == "hours" else timedelta(minutes=amount))
    return None

def _guess_time(text: str) -> tuple[int, int]:
    numeric_time = _extract_numeric_time(text)
    if numeric_time is not None:
        return numeric_time

    voice_phrases: list[tuple[str, int, int]] = [
        (r"fim\s+do\s+dia|final\s+do\s+dia|fim\s+d[ae]\s+tard[ae]|final\s+da\s+tard[ae]", 18, 0),
        (r"inicio\s+da\s+manh[aã]|come[cç]o\s+da\s+manh[aã]|\bcedo\b", 8, 0),
        (r"\bde\s+manh[aã]\b|\bpela\s+manh[aã]\b", 9, 0),
        (r"depois\s+do\s+almo[cç]o|p[oó]s[\s-]almo[cç]o", 14, 0),
        (r"\bmeio[\s-]?dia\b", 12, 0),
        (r"come[cç]o\s+da\s+tard[ae]|in[ií]cio\s+da\s+tard[ae]", 13, 0),
        (r"\bde\s+tard[ae]\b|\b[aà]\s+tard[ae]\b|\bpela\s+tard[ae]\b", 15, 0),
        (r"\bde\s+noite\b|\b[aà]\s+noite\b|\bpela\s+noite\b", 20, 0),
        (r"fim\s+da\s+noite|final\s+da\s+noite|\bbem\s+tarde\b", 22, 0),
    ]
    for pattern, hour, minute in voice_phrases:
        if re.search(pattern, text):
            return hour, minute

    words_to_hours = {
        "vinte e tres": 23,
        "vinte e duas": 22,
        "vinte e uma": 21,
        "dezessete": 17,
        "dezesseis": 16,
        "dezenove": 19,
        "quatorze": 14,
        "catorze": 14,
        "dezoito": 18,
        "quinze": 15,
        "treze": 13,
        "vinte": 20,
        "doze": 12,
        "onze": 11,
        "dez": 10,
        "nove": 9,
        "oito": 8,
        "sete": 7,
        "seis": 6,
        "cinco": 5,
        "quatro": 4,
        "tres": 3,
        "duas": 2,
        "dois": 2,
        "uma": 1,
    }

    for phrase, hour in words_to_hours.items():
        if re.search(rf"\b{re.escape(phrase)}\b", text):
            if hour < 12 and any(marker in text for marker in ["tarde", "noite"]):
                return hour + 12, 0
            return hour, 0

    return 18, 0

def _extract_numeric_time(text: str) -> tuple[int, int] | None:
    patterns = [
        r"\b(?:as|ate|a)\s+(\d{1,2}):(\d{2})\b",
        r"\b(\d{1,2}):(\d{2})\b",
        r"\b(?:as|ate|a)\s+(\d{1,2})h(?:(\d{2}))?\b",
        r"\b(\d{1,2})h(?:(\d{2}))?\b",
        r"\b(?:as|ate|a)\s+(\d{1,2})(?:\s*horas?)?\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        hour = int(match.group(1))
        minute = int(match.group(2) or 0) if len(match.groups()) > 1 else 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    return None


def _normalize_text(value: str) -> str:
    text = normalize("NFKD", value.lower().strip())
    return "".join(char for char in text if char.isascii())


def _normalize_audio_artifacts(text: str) -> str:
    # Whisper sometimes drops the first syllable of "lembrar" and transcribes "r o" at start
    text = re.sub(r"^r\s+o\s+", "lembrar o ", text)
    text = re.sub(r"^r\s+a\s+", "lembrar a ", text)
    text = re.sub(r"\besta rapida\s+a\s+", "testar api da ", text)
    text = re.sub(r"\bta rapida\s+a\s+", "testar api da ", text)
    text = re.sub(r"\bconclu[ií]\b", "concluir", text)
    text = re.sub(r"\bconduir\b", "concluir", text)
    text = re.sub(r"\bregendar\b", "reagendar", text)
    text = re.sub(r"\bre-agendar\b", "reagendar", text)
    text = re.sub(r"\bati\b(?=\s+\d)", "ate", text)
    text = re.sub(r"\b(?:pras|pra as|para as)\s+(\d)", r"as \1", text)
    text = re.sub(r"\bsem pre[çcz]o\b", "sem prazo", text)
    text = re.sub(r"\bsem pr[ae]zo\b", "sem prazo", text)
    text = re.sub(r"\bdeletar para\b", "delegar para", text)
    text = re.sub(r"\bdeletar pra\b", "delegar pra", text)
    text = re.sub(r"\bprosposta\b", "proposta", text)
    text = re.sub(r"\bpr[aá]\s*amanh[aã]\b", "pra amanha", text)
    return text

def _leading_assignee_candidate(text: str) -> str | None:
    words = text.split()
    if len(words) < 2:
        return None
    first = words[0].strip(" ,.")
    if not first or first in TASK_VERBS or first in {"eu", "me", "tenho", "preciso", "nao", "criar", "nova", "salva", "salvar", "anota", "anotar", "lembra", "lembre", "vamos", "vou"}:
        return None
    rest = " ".join(words[1:])
    if not _has_task_verb(rest):
        return None
    if len(first) < 3 or first.isdigit():
        return None
    return first


def _strip_task_pronoun(value: str) -> str:
    return re.sub(r"^(?:de|me|pra mim|para mim|eu preciso|preciso)\s+", "", value).strip()


_DEMONSTRATIVE_REFS = {
    "essa tarefa", "essa", "isso", "ela", "este", "esta", "aqui",
    "a tarefa", "desta tarefa", "aquela tarefa", "aquela",
    "nessa tarefa", "nesta tarefa", "disso",
}


def _strip_demonstrative_ref(value: str | None) -> str | None:
    """Return None when the task reference is a demonstrative pronoun, not a real title."""
    if not value:
        return None
    cleaned = value.strip()
    if cleaned.lower() in _DEMONSTRATIVE_REFS:
        return None
    return cleaned


def _strip_trailing_filler(value: str) -> str:
    """Remove conversational confirmation fillers from the end of a task title."""
    fillers = [
        r",?\s*por\s+favor\.?$",
        r",?\s*favor\.?$",
        r",?\s*por\s+gentileza\.?$",
        r",?\s*ta\s+bom\??$",
        r",?\s*tudo\s+bem\??$",
        r",?\s*tudo\s+certo\??$",
        r",?\s*pode\s+ser\??$",
        r",?\s*ne\s+nao\??$",
        r",?\s*ne\??$",
        r",?\s*nao\s+e\??$",
        r",?\s*certo\??$",
        r",?\s*ok\??$",
        r",?\s*beleza\??$",
        r",?\s*combinado\??$",
        r",?\s*pode\??$",
        r",?\s*sim\??$",
        r",?\s*correto\??$",
    ]
    cleaned = value
    for pattern in fillers:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE).strip(" ,.")
    return cleaned


def _is_bad_task_title(value: str | None) -> bool:
    if not value:
        return True
    normalized = _normalize_text(value).strip(" .,;:!?")
    filler_titles = {
        "por favor",
        "favor",
        "por gentileza",
        "ok",
        "certo",
        "beleza",
        "combinado",
        "ta bom",
        "tudo bem",
        "pode ser",
        "pode",
        "sim",
        "correto",
    }
    return normalized in filler_titles


def _title_ascii_name(value: str) -> str:
    return " ".join(part[:1].upper() + part[1:] for part in value.split())


def _normalize_phone(value: str) -> str:
    return normalize_phone(value)


def _is_task_acknowledgement_local(text: str) -> bool:
    """Lightweight version of graph._is_task_acknowledgement for use in local parser."""
    if not text:
        return False
    if _contains_any(text, ["conclui", "concluido", "concluida", "feito", "terminei", "finalizei"]):
        return False
    if _contains_any(text, ["prazo", "reagendar", "remarcar", "adiar", "mudar"]):
        return False

    short_acks = {"ok", "okay", "sim", "beleza", "fechado", "combinado", "ta bom", "esta bom", "blz", "show", "perfeito", "certo", "tranquilo", "aceito", "aceitar"}
    if text in short_acks:
        return True

    patterns = [
        r"\b(eu\s+)?aceit\w*\b",
        r"\bpode\s+deixar\b",
        r"\bvou\s+(fazer|ver|ajustar|resolver|cuidar|mexer|olhar|trabalhar|pedir|cobrar|ligar|mandar|enviar|iniciar|comecar|solicitar|verificar|checar|falar|confirmar)\b",
        r"\bdeixa\s+comigo\b",
        r"\bfico\s+com\s+essa\b",
        r"\bta\s+comigo\b",
        r"\besta\s+comigo\b",
        r"\bcomecei\b",
        r"\biniciei\b",
        r"\bja\s+(comecei|iniciei|estou|to)\b",
        r"\bpode\s+contar\s+comigo\b",
        r"\bvou\s+\w+\s+agora\b",
    ]
    return any(re.search(p, text) for p in patterns)


def _extract_start_task_reference(text: str) -> str | None:
    """Extract a meaningful task reference from acknowledgement phrases like 'comecei o deploy da Nanocare'."""
    start_markers = ["iniciei", "comecei", "estou trabalhando em", "ja comecei", "ja iniciei", "iniciando"]
    for marker in start_markers:
        if marker in text:
            after = text.split(marker, 1)[1].strip()
            after = re.sub(r"^(?:o|a|os|as|um|uma)\s+", "", after).strip()
            if len(after) >= 3:
                return _normalize_task_title(after)
    return None

