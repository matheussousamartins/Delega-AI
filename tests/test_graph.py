from datetime import datetime, timedelta, timezone
from uuid import uuid4

from whatsapp_task_agent import tools
from whatsapp_task_agent.graph import _task_matches, app_graph
from whatsapp_task_agent.parser import parse_message, _fill_addressed_assignee_from_team, _sanitize_llm_parsed
from whatsapp_task_agent.schemas import Action, Intent, ParsedCommand, Task
from whatsapp_task_agent.store import InMemoryTaskStore


def test_create_and_list_task() -> None:
    created = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "criar tarefa enviar proposta para cliente X amanha as 18 prioridade alta",
            "provider_message_id": "test-1",
        }
    )

    assert "Tarefa criada para você" in created["reply"]

    listed = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "minhas tarefas",
            "provider_message_id": "test-2",
        }
    )

    assert "Enviar proposta" in listed["reply"]


def test_list_tasks_and_delegated_tasks_uses_separate_sections() -> None:
    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "criar tarefa revisar contrato da listagem amanha as 18",
            "provider_message_id": "test-list-sections-1",
        }
    )
    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Joao, enviar proposta da listagem amanha as 10",
            "provider_message_id": "test-list-sections-2",
        }
    )

    listed = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "minhas tarefas e as que deleguei",
            "provider_message_id": "test-list-sections-3",
        }
    )

    assert "📋 Suas tarefas abertas" in listed["reply"]
    assert "📤 Tarefas que você delegou" in listed["reply"]
    assert "Revisar contrato" in listed["reply"]
    assert "Enviar proposta" in listed["reply"]
    assert "Cliente: Listagem" in listed["reply"]
    assert "Responsável: Joao" in listed["reply"]
    assert "Prazo:" in listed["reply"]
    assert "Status: pendente" in listed["reply"]


def test_list_delegated_tasks_uses_delegated_section_only() -> None:
    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Joao, validar relatorio delegado unico amanha as 11",
            "provider_message_id": "test-list-delegated-only-setup",
        }
    )

    listed = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "tarefas que eu deleguei",
            "provider_message_id": "test-list-delegated-only-1",
        }
    )

    assert "📤 Tarefas que você delegou" in listed["reply"]
    assert "Validar relatorio delegado unico" in listed["reply"]
    assert "Responsável: Joao" in listed["reply"]


def test_create_reply_includes_operational_details() -> None:
    created = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Joao, envia o contrato amanha ate as dezoito",
            "provider_message_id": "test-copy-1",
        }
    )

    assert "Tarefa criada" in created["reply"]
    assert "Enviar o contrato" in created["reply"]
    assert "Responsável: Joao" in created["reply"]
    assert "Prazo:" in created["reply"]
    assert "Codigo:" not in created["reply"]


def test_parser_normalizes_delegated_task_title() -> None:
    parsed = parse_message("Joao, envia a proposta hoje ate 18h")

    assert parsed.params["title"] == "Enviar a proposta"
    assert parsed.params["assignee_name"] == "Joao"
    assert "T18:00:00" in parsed.params["due_date"]


def test_parser_understands_written_portuguese_hour() -> None:
    parsed = parse_message("Joao, envia a proposta amanha ate as dezoito")

    assert parsed.params["title"] == "Enviar a proposta"
    assert parsed.params["assignee_name"] == "Joao"
    assert "T18:00:00" in parsed.params["due_date"]


def test_parser_understands_afternoon_hour() -> None:
    parsed = parse_message("Maria, revisa o contrato hoje as seis da tarde")

    assert parsed.params["title"] == "Revisar o contrato"
    assert parsed.params["assignee_name"] == "Maria"
    assert "T18:00:00" in parsed.params["due_date"]


def test_parser_preserves_numeric_minutes() -> None:
    parsed = parse_message("criar tarefa testar lembrete hoje as 18:43")

    assert parsed.params["title"] == "Testar lembrete"
    assert "T18:43:00" in parsed.params["due_date"]


def test_parser_understands_personal_task_phrasing() -> None:
    examples = {
        "tenho que revisar contrato amanha as 18": "Revisar contrato",
        "nao posso esquecer de pagar boleto hoje as 17": "Pagar boleto",
        "me lembra de enviar proposta sexta as 10": "Enviar proposta",
        "salva pra mim validar dashboard amanha": "Validar dashboard",
    }

    for message, expected_title in examples.items():
        parsed = parse_message(message)
        assert parsed.action == "create_task"
        assert parsed.params["title"] == expected_title
        assert parsed.params["assignee_name"] is None
        assert parsed.params["due_date"] is not None


def test_self_task_reply_feels_personal() -> None:
    created = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "tenho que revisar contrato amanha as 18",
            "provider_message_id": "test-self-task-1",
        }
    )

    assert "Tarefa criada para você" in created["reply"]
    assert "Revisar contrato" in created["reply"]
    assert "Fica na sua lista" in created["reply"]
    assert "Para delegar" not in created["reply"]


def test_missing_due_date_for_self_task_asks_naturally() -> None:
    result = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "tenho que revisar contrato",
            "provider_message_id": "test-self-task-missing-date-1",
        }
    )

    assert "Tarefa criada para você" in result["reply"]
    assert "Revisar contrato" in result["reply"]
    assert "sem prazo" in result["reply"]
    assert "colocar prazo" in result["reply"].lower()


def test_pending_task_can_be_created_without_due_date() -> None:
    created = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "tenho que revisar contrato",
            "provider_message_id": "test-self-task-no-date-1",
        }
    )

    assert "Tarefa criada para você" in created["reply"]
    assert "Revisar contrato" in created["reply"]
    assert "sem prazo" in created["reply"]

    updated = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "coloca prazo amanhã às 10",
            "provider_message_id": "test-self-task-no-date-2",
        }
    )

    assert "Prazo atualizado" in updated["reply"]
    assert "Revisar contrato" in updated["reply"]


def test_task_matches_sorts_mixed_timezone_due_dates() -> None:
    company_id = uuid4()
    user_id = uuid4()
    tasks = [
        Task(
            id=uuid4(),
            company_id=company_id,
            title="Tarefa com timezone",
            assigned_to=user_id,
            created_by=user_id,
            due_at=datetime(2026, 5, 8, 15, 0, tzinfo=timezone.utc),
        ),
        Task(
            id=uuid4(),
            company_id=company_id,
            title="Tarefa sem timezone",
            assigned_to=user_id,
            created_by=user_id,
            due_at=datetime(2026, 5, 8, 13, 0),
        ),
    ]

    matches = _task_matches(tasks)

    assert [match["title"] for match in matches] == [
        "Tarefa com timezone",
        "Tarefa sem timezone",
    ]


def test_pending_task_accepts_audio_transcription_without_due_date_punctuation() -> None:
    created = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Joao, ajustar o Delega AI para receber contato e salvar no time",
            "provider_message_id": "test-self-task-no-date-audio-1",
        }
    )

    assert "Tarefa criada" in created["reply"]
    assert "ajustar o delega ai para receber contato e salvar no time" in created["reply"].lower()
    assert "sem prazo" in created["reply"]
    assert "colocar prazo" in created["reply"].lower()


def test_parser_understands_natural_delegation_with_weekday() -> None:
    parsed = parse_message("Luiz, me fazer um Pix de 2k ate segunda-feira")

    assert parsed.action == "create_task"
    assert parsed.params["title"] == "Fazer um pix de 2k"
    assert parsed.params["assignee_name"] == "Luiz"
    assert "T18:00:00" in parsed.params["due_date"]


def test_parser_understands_adjust_task_with_weekday() -> None:
    parsed = parse_message("Luiz, ajustar os prazos no DixCore ate terca-feira")

    assert parsed.action == "create_task"
    assert parsed.params["title"] == "Ajustar os prazos"
    assert parsed.params["assignee_name"] == "Luiz"
    assert parsed.params["client_name"] == "Dixcore"
    assert "T18:00:00" in parsed.params["due_date"]


def test_parser_understands_test_task_with_polite_suffix() -> None:
    parsed = parse_message("Leozin, testar a API da Derry ate amanha, por favor.")

    assert parsed.action == "create_task"
    assert parsed.params["title"] == "Testar a api"
    assert parsed.params["assignee_name"] == "Leozin"
    assert parsed.params["client_name"] == "Derry"
    assert "T18:00:00" in parsed.params["due_date"]


def test_parser_handles_leading_weekday_and_polite_suffix() -> None:
    parsed = parse_message("Leo, na segunda, pode fazer o deploy da nova tech, por favor.")

    assert parsed.action == "create_task"
    assert parsed.params["title"] == "Fazer o deploy"
    assert parsed.params["assignee_name"] == "Leo"
    assert parsed.params["client_name"] == "Nova Tech"
    assert parsed.params["due_date"] is not None


def test_llm_sanitizer_repairs_filler_only_task_title() -> None:
    parsed = _sanitize_llm_parsed(
        ParsedCommand(
            intent=Intent.task,
            action=Action.create_task,
            params={
                "title": "Por favor",
                "assignee_name": "Leo",
                "due_date": "2026-05-11T18:00:00",
            },
            confidence=95,
        ),
        original_message="Leo, na segunda, pode fazer o deploy da nova tech, por favor.",
    )

    assert parsed.action == Action.create_task
    assert parsed.params["title"] == "Fazer o deploy"
    assert parsed.params["client_name"] == "Nova Tech"
    assert parsed.params["due_date"] == "2026-05-11T18:00:00-03:00"


def test_parser_handles_audio_without_comma_and_transcription_artifact() -> None:
    parsed = parse_message("Jonas esta rapida a Dairy ate amanha as dezoito")

    assert parsed.action == "create_task"
    assert parsed.params["title"] == "Testar api"
    assert parsed.params["assignee_name"] == "Jonas"
    assert parsed.params["client_name"] == "Dairy"
    assert "T18:00:00" in parsed.params["due_date"]


def test_parser_does_not_treat_vamos_as_assignee() -> None:
    parsed = parse_message("Vamos testar a API da Deri ate amanha as dezoito")

    assert parsed.action == "create_task"
    assert parsed.params["title"] == "Vamos testar a api"
    assert parsed.params["assignee_name"] is None
    assert parsed.params["client_name"] == "Deri"


def test_llm_parser_output_is_sanitized_before_execution() -> None:
    parsed = _sanitize_llm_parsed(
        ParsedCommand(
            intent=Intent.task,
            action=Action.create_task,
            params={
                "title": " testar api ",
                "assignee_name": " Leozinho ",
                "client_name": " Derry ",
                "due_date": "amanha",
                "priority": "urgente",
                "company_id": "forbidden",
            },
            confidence=82,
        )
    )

    assert parsed.action == Action.create_task
    assert parsed.params["title"] == "Testar api"
    assert parsed.params["assignee_name"] == "Leozinho"
    assert parsed.params["client_name"] == "Derry"
    assert parsed.params["priority"] is None
    assert "company_id" not in parsed.params
    assert parsed.params["due_date"] is not None


def test_parser_understands_create_as_natural_delegation() -> None:
    parsed = parse_message("Martins, criar os fluxos da Dairy no n8n")

    assert parsed.action == "create_task"
    assert parsed.params["title"] == "Criar os fluxos no n8n"
    assert parsed.params["assignee_name"] == "Martins"
    assert parsed.params["client_name"] == "Dairy"
    assert parsed.params["due_date"] is None


def test_parser_treats_contact_and_time_inside_sentence_as_task_content() -> None:
    parsed = parse_message("Mateus, ajustar o Delegate AI para receber um contato e salvar no time.")

    assert parsed.action == "create_task"
    assert parsed.params["title"] == "Ajustar o delegate ai para receber um contato e salvar no time"
    assert parsed.params["assignee_name"] == "Mateus"
    assert parsed.params["due_date"] is None


def test_parser_understands_task_status_questions() -> None:
    examples = [
        ("Joao ja fez o deploy?", {"member_name": "Joao", "task_reference": "deploy"}),
        ("como esta a tarefa da Nanocare?", {"client_name": "Nanocare"}),
        ("o que o Joao tem pendente?", {"member_name": "Joao"}),
        ("status do deploy da Nanocare", {"task_reference": "deploy", "client_name": "Nanocare"}),
    ]

    for message, expected in examples:
        parsed = parse_message(message)
        assert parsed.action == Action.task_status
        for key, value in expected.items():
            assert str(parsed.params.get(key)).lower() == value.lower()


def test_task_status_by_member_reference_and_client() -> None:
    created = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Joao, fazer o deploy da Nanocare amanha as 18",
            "provider_message_id": "test-status-create-1",
        }
    )
    assert "Tarefa criada" in created["reply"]

    started = app_graph.invoke(
        {
            "from_phone": "+5511988888888",
            "message": "comecei o deploy da Nanocare",
            "provider_message_id": "test-status-start-1",
        }
    )
    assert "em andamento" in started["reply"]

    by_member = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Joao ja fez o deploy?",
            "provider_message_id": "test-status-query-1",
        }
    )
    assert "Status da tarefa" in by_member["reply"]
    assert "Fazer o deploy" in by_member["reply"]
    assert "Responsável: Joao" in by_member["reply"]
    assert "Status: em andamento" in by_member["reply"]

    by_client = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "como esta a tarefa da Nanocare?",
            "provider_message_id": "test-status-query-2",
        }
    )
    assert "Nanocare" in by_client["reply"]
    assert "Status: em andamento" in by_client["reply"]


def test_task_status_lists_member_pending_tasks() -> None:
    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Joao, revisar contrato da Alpha amanha as 10",
            "provider_message_id": "test-status-list-1",
        }
    )
    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Joao, fazer deploy da Nanocare amanha as 18",
            "provider_message_id": "test-status-list-2",
        }
    )

    result = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "o que o Joao tem pendente?",
            "provider_message_id": "test-status-list-3",
        }
    )

    assert "Tarefas de Joao" in result["reply"]
    assert "Revisar contrato" in result["reply"]
    assert "Fazer deploy" in result["reply"]


def test_member_cannot_query_unrelated_member_tasks() -> None:
    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Maria, revisar contrato da Alpha amanha as 10",
            "provider_message_id": "test-status-private-1",
        }
    )

    result = app_graph.invoke(
        {
            "from_phone": "+5511988888888",
            "message": "o que a Maria tem pendente?",
            "provider_message_id": "test-status-private-2",
        }
    )

    assert "Não encontrei tarefas abertas" in result["reply"]
    assert "Revisar contrato" not in result["reply"]


def test_parser_understands_natural_look_request_as_task() -> None:
    parsed = parse_message("Matheus, quero que você dê uma olhadinha nas tasks da Derry para entender o que está pendente.")

    assert parsed.action == "create_task"
    assert parsed.params["assignee_name"] == "Matheus"
    assert parsed.params["client_name"] == "Derry"
    assert parsed.params["due_date"] is None
    assert "olhadinha nas tasks" in parsed.params["title"].lower()


def test_llm_task_assignee_is_normalized_from_team_members() -> None:
    parsed = _fill_addressed_assignee_from_team(
        "Mateus, quero que voce de uma olhadinha nas tasks da Derry",
        ParsedCommand(
            intent=Intent.task,
            action=Action.create_task,
            params={"title": "Dar uma olhadinha nas tasks da Derry", "assignee_name": "Mateus"},
            confidence=95,
        ),
        [{"name": "Matheus Martins", "role": "owner"}],
    )

    assert parsed.params["assignee_name"] == "Matheus Martins"


def test_llm_task_assignee_is_resolved_from_middle_of_message() -> None:
    parsed = _fill_addressed_assignee_from_team(
        "Quero que o Matheus revise as tasks da Derry",
        ParsedCommand(
            intent=Intent.task,
            action=Action.create_task,
            params={"title": "Revisar as tasks da Derry"},
            confidence=95,
        ),
        [{"name": "Matheus Martins", "role": "owner"}, {"name": "Leo", "role": "member"}],
    )

    assert parsed.params["assignee_name"] == "Matheus Martins"


def test_llm_task_assignee_is_resolved_from_end_of_message() -> None:
    parsed = _fill_addressed_assignee_from_team(
        "Essa tarefa e para o Leo",
        ParsedCommand(
            intent=Intent.task,
            action=Action.create_task,
            params={"title": "Revisar as tasks da Derry"},
            confidence=95,
        ),
        [{"name": "Matheus Martins", "role": "owner"}, {"name": "Leo", "role": "member"}],
    )

    assert parsed.params["assignee_name"] == "Leo"


def test_llm_task_assignee_is_not_filled_when_multiple_members_are_mentioned() -> None:
    parsed = _fill_addressed_assignee_from_team(
        "Matheus e Leo precisam alinhar as tasks da Derry",
        ParsedCommand(
            intent=Intent.task,
            action=Action.create_task,
            params={"title": "Alinhar as tasks da Derry"},
            confidence=95,
        ),
        [{"name": "Matheus Martins", "role": "owner"}, {"name": "Leo", "role": "member"}],
    )

    assert parsed.params.get("assignee_name") is None

def test_llm_first_result_is_not_overridden_by_local_parser(monkeypatch) -> None:
    monkeypatch.setattr("whatsapp_task_agent.parser.settings.openai_api_key", "test-key")
    monkeypatch.setattr(
        "whatsapp_task_agent.parser._parse_with_llm_safely",
        lambda *args, **kwargs: ParsedCommand(intent=Intent.other, action=None, params={}, confidence=40),
    )

    parsed = parse_message("Matheus, quero que voce de uma olhadinha nas tasks da Derry")

    assert parsed.action is None
    assert parsed.confidence == 40


def test_local_parser_is_only_used_when_llm_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr("whatsapp_task_agent.parser.settings.openai_api_key", "test-key")
    monkeypatch.setattr("whatsapp_task_agent.parser._parse_with_llm_safely", lambda *args, **kwargs: None)

    parsed = parse_message("Matheus, quero que voce de uma olhadinha nas tasks da Derry")

    assert parsed.action == "create_task"
    assert parsed.params["assignee_name"] == "Matheus"


def test_low_confidence_llm_invite_is_blocked_before_execution() -> None:
    parsed = _sanitize_llm_parsed(
        ParsedCommand(
            intent=Intent.command,
            action=Action.invite_user,
            params={},
            confidence=50,
        )
    )

    assert parsed.action is None
    assert parsed.confidence <= 50


def test_missing_due_date_creates_immediately_and_accepts_later_update() -> None:
    created = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Joao, criar os fluxos da Dairy no n8n",
            "provider_message_id": "test-draft-1",
        }
    )

    assert "Tarefa criada" in created["reply"]
    assert "Criar os fluxos" in created["reply"]
    assert "sem prazo" in created["reply"]
    assert "colocar prazo" in created["reply"].lower()

    updated = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "coloca prazo amanhã às 18",
            "provider_message_id": "test-draft-2",
        }
    )

    assert "Prazo atualizado" in updated["reply"]
    assert "Criar os fluxos" in updated["reply"]


def test_parser_understands_natural_pending_task_queries() -> None:
    examples = [
        "o que tenho pra fazer?",
        "quais tarefas tenho?",
        "me mostra minhas pendencias",
        "tenho algo pendente?",
        "minhas pendencias",
    ]

    for message in examples:
        parsed = parse_message(message)
        assert parsed.action == "list_my_tasks"
        assert parsed.params["filter"] == "pending"


def test_parser_understands_personal_and_delegated_task_views() -> None:
    delegated = parse_message("tarefas que eu deleguei")
    combined = parse_message("minhas tarefas e as que deleguei")
    delegated_today = parse_message("tarefas que deleguei hoje")
    delegated_client = parse_message("tarefas que eu deleguei da Nanocare")

    assert delegated.action == "list_my_tasks"
    assert delegated.params["filter"] == "pending"
    assert delegated.params["view"] == "delegated"
    assert combined.action == "list_my_tasks"
    assert combined.params["filter"] == "pending"
    assert combined.params["view"] == "all"
    assert delegated_today.action == "list_my_tasks"
    assert delegated_today.params["filter"] == "today"
    assert delegated_today.params["view"] == "delegated"
    assert delegated_client.action == "list_my_tasks"
    assert delegated_client.params["filter"] == "pending"
    assert delegated_client.params["view"] == "delegated"
    assert delegated_client.params["client_name"] == "Nanocare"


def test_contextual_fallback_asks_for_missing_delegation_details() -> None:
    result = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Martins, ve isso pra mim",
            "provider_message_id": "test-contextual-fallback-1",
        }
    )

    assert "Martins" in result["reply"]
    assert "faltou clareza" in result["reply"]
    assert "revisar contrato da Dairy" in result["reply"]


def test_contextual_fallback_asks_task_query_scope() -> None:
    result = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "tarefas",
            "provider_message_id": "test-contextual-fallback-2",
        }
    )

    assert "Quais tarefas voce quer ver" in result["reply"]
    assert "tarefas de hoje" in result["reply"]
    assert "tarefas atrasadas" in result["reply"]


def test_incomplete_complete_command_no_open_tasks() -> None:
    # When user has no open tasks assigned to them, completing without reference
    # should say "no open tasks" rather than asking "which task?"
    result = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "concluir",
            "provider_message_id": "test-contextual-fallback-3",
        }
    )

    reply = result["reply"].lower()
    assert "tarefas abertas" in reply or "qual tarefa" in reply


def test_parser_understands_today_and_overdue_task_queries() -> None:
    today = parse_message("o que vence hoje?")
    overdue = parse_message("o que esta atrasado?")

    assert today.action == "list_my_tasks"
    assert today.params["filter"] == "today"
    assert overdue.action == "list_my_tasks"
    assert overdue.params["filter"] == "overdue"


def test_parser_understands_specific_date_task_query() -> None:
    parsed = parse_message("Minhas tarefas para dia 04/05.")

    assert parsed.action == "list_my_tasks"
    assert parsed.params["filter"] == "date"
    assert parsed.params["date"].endswith("-05-04")


def test_specific_date_query_filters_tasks() -> None:
    store = InMemoryTaskStore()
    context = store.identify_user("+5511999999999")
    store.create_task(
        context["company_id"],
        context["user_id"],
        "Tarefa do dia quatro",
        due_at=datetime(2026, 5, 4, 18, 0),
    )
    store.create_task(
        context["company_id"],
        context["user_id"],
        "Tarefa do dia cinco",
        due_at=datetime(2026, 5, 5, 18, 0),
    )

    tasks = store.list_tasks(
        context["company_id"],
        context["user_id"],
        status_filter="date",
        target_date=datetime(2026, 5, 4).date(),
    )

    assert [task.title for task in tasks] == ["Tarefa do dia quatro"]


def test_specific_date_query_filters_delegated_tasks() -> None:
    store = InMemoryTaskStore()
    context = store.identify_user("+5511999999999")
    store.create_task(
        context["company_id"],
        context["user_id"],
        "Delegada do dia quatro",
        assignee_name="Joao",
        due_at=datetime(2026, 5, 4, 10, 0),
    )
    store.create_task(
        context["company_id"],
        context["user_id"],
        "Delegada do dia cinco",
        assignee_name="Joao",
        due_at=datetime(2026, 5, 5, 10, 0),
    )

    tasks = store.list_delegated_tasks(
        context["company_id"],
        context["user_id"],
        status_filter="date",
        target_date=datetime(2026, 5, 4).date(),
    )

    assert [task.title for task in tasks] == ["Delegada do dia quatro"]


def test_list_my_tasks_filters_delegated_tasks_by_client(monkeypatch) -> None:
    memory_store = InMemoryTaskStore()
    monkeypatch.setattr(tools, "store", memory_store)
    context = memory_store.identify_user("+5511999999999")
    memory_store.create_task(
        context["company_id"],
        context["user_id"],
        "Delegada Nanocare",
        assignee_name="Joao",
        client_name="Nanocare",
        due_at=datetime(2026, 5, 4, 10, 0),
    )
    memory_store.create_task(
        context["company_id"],
        context["user_id"],
        "Delegada Alpha",
        assignee_name="Joao",
        client_name="Alpha",
        due_at=datetime(2026, 5, 4, 11, 0),
    )

    result = tools.list_my_tasks(context, {"view": "delegated", "client_name": "Nanocare"})

    assert [task["title"] for task in result["delegated_tasks"]] == ["Delegada Nanocare"]
    assert result["total_delegated"] == 1


def test_list_my_tasks_paginates_delegated_tasks(monkeypatch) -> None:
    memory_store = InMemoryTaskStore()
    monkeypatch.setattr(tools, "store", memory_store)
    context = memory_store.identify_user("+5511999999999")
    for index in range(tools.PAGE_SIZE + 1):
        memory_store.create_task(
            context["company_id"],
            context["user_id"],
            f"Delegada pagina {index + 1:02d}",
            assignee_name="Joao",
            due_at=datetime(2026, 5, 4, 10, index),
        )

    first_page = tools.list_my_tasks(context, {"view": "delegated"})
    second_page = tools.list_my_tasks(context, {"view": "delegated", "page": 2})

    assert len(first_page["delegated_tasks"]) == tools.PAGE_SIZE
    assert first_page["has_more"] is True
    assert [task["title"] for task in second_page["delegated_tasks"]] == ["Delegada pagina 11"]
    assert second_page["has_more"] is False


def test_empty_overdue_query_uses_overdue_copy() -> None:
    result = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "quais minhas tarefas atrasadas?",
            "provider_message_id": "test-empty-overdue-copy",
        }
    )

    assert "tarefas atrasadas" in result["reply"]
    assert "pendentes" not in result["reply"]


def test_team_summary_includes_actionable_pending_details() -> None:
    store = InMemoryTaskStore()
    context = store.identify_user("+5511999999999")
    store.create_task(
        context["company_id"],
        context["user_id"],
        "Ajustar site",
        assignee_name="Joao",
        due_at=datetime.now() + timedelta(days=1),
    )
    summary = store.team_summary(context["company_id"])

    assert summary["pending"] == 1
    assert summary["pending_tasks"][0]["assignee_name"] == "Joao"
    assert summary["pending_tasks"][0]["title"] == "Ajustar site"


def test_store_creates_task_with_client_context() -> None:
    store = InMemoryTaskStore()
    context = store.identify_user("+5511999999999")

    task = store.create_task(
        context["company_id"],
        context["user_id"],
        "Criar fluxos no n8n",
        assignee_name="Joao",
        client_name="Dairy",
        due_at=datetime(2026, 5, 4, 18, 0),
    )

    assert task.client_name == "Dairy"
    assert task.client_id is not None


def test_team_summary_reply_is_actionable() -> None:
    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "criar tarefa validar painel premium amanha as 18",
            "provider_message_id": "test-team-summary-1",
        }
    )
    result = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "resumo do time",
            "provider_message_id": "test-team-summary-2",
        }
    )

    assert "Resumo do time" in result["reply"]
    assert "Próximas pendentes" in result["reply"]
    assert "tarefas do time" in result["reply"]


def test_parser_understands_team_views() -> None:
    assert parse_message("tarefas do time").params["view"] == "team_tasks"
    assert parse_message("atrasadas do time").params["view"] == "team_overdue"
    assert parse_message("concluidas hoje").params["view"] == "team_done_today"
    assert parse_message("concluidas do time").params["view"] == "team_done_today"

    # "tarefas do [nome]" now routes to task_status for fuzzy member matching
    member = parse_message("tarefas do Luiz")
    assert member.action is not None
    assert member.params.get("member_name") == "Luiz"


def test_invite_user_reply_confirms_invite() -> None:
    invited = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "convidar Joao 554188880000 como Desenvolvedor",
            "provider_message_id": "test-invite-1",
        }
    )

    assert "Convite enviado" in invited["reply"]
    assert "Joao" in invited["reply"]
    assert "+554188880000" in invited["reply"]
    assert "Desenvolvedor" in invited["reply"]


def test_member_can_invite_user() -> None:
    invited = app_graph.invoke(
        {
            "from_phone": "+5511988888888",
            "message": "convidar Pedro 554188881111 como Analista",
            "provider_message_id": "test-member-invite-1",
        }
    )

    assert "Convite enviado" in invited["reply"]
    assert "Pedro" in invited["reply"]
    assert invited["result"]["invited_by_name"] == "Joao"
    assert invited["result"]["role"] == "member"


def test_incomplete_invite_suggests_sharing_contact() -> None:
    result = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Quero convidar um colaborador",
            "provider_message_id": "test-invite-copy-1",
        }
    )

    assert "compartilhar o contato" in result["reply"]
    assert "convidar Luiz" in result["reply"]


def test_invited_phone_can_accept_invite_before_onboarding() -> None:
    invited_phone = "+554188880777"
    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "convidar Pedro 554188880777 como Designer",
            "provider_message_id": "test-invite-accept-1",
        }
    )

    prompt = app_graph.invoke(
        {
            "from_phone": invited_phone,
            "message": "oi",
            "provider_message_id": "test-invite-accept-2",
        }
    )

    assert "foi convidado" in prompt["reply"]
    assert "Commandix" in prompt["reply"]
    assert "aceitar convite" in prompt["reply"]

    accepted = app_graph.invoke(
        {
            "from_phone": invited_phone,
            "message": "1",
            "provider_message_id": "test-invite-accept-3",
        }
    )

    assert "Convite aceito" in accepted["reply"]
    assert "Pedro" in accepted["reply"]
    assert "Designer" in accepted["reply"]
    assert accepted["result"]["should_notify_inviter"] is True
    assert accepted["result"]["invited_by_phone"] == "+5511999999999"


def test_invited_phone_accepts_invite_with_natural_language() -> None:
    invited_phone = "+554188880779"
    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "convidar Leo 554188880779 como Engenheiro Fullstack",
            "provider_message_id": "test-invite-natural-accept-1",
        }
    )

    accepted = app_graph.invoke(
        {
            "from_phone": invited_phone,
            "message": "eu aceito o convite, pode confirmar",
            "provider_message_id": "test-invite-natural-accept-2",
        }
    )

    assert "Convite aceito" in accepted["reply"]
    assert "Leo" in accepted["reply"]
    assert "Engenheiro Fullstack" in accepted["reply"]
    assert accepted["result"]["should_notify_inviter"] is True


def test_invited_phone_accepts_invite_with_audio_style_transcription() -> None:
    invited_phone = "+554188880780"
    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "convidar Bia 554188880780 como Designer",
            "provider_message_id": "test-invite-audio-accept-1",
        }
    )

    accepted = app_graph.invoke(
        {
            "from_phone": invited_phone,
            "message": "sim pode me colocar quero participar",
            "provider_message_id": "test-invite-audio-accept-2",
        }
    )

    assert "Convite aceito" in accepted["reply"]
    assert "Bia" in accepted["reply"]
    assert accepted["result"]["invite_accepted"] is True


def test_invited_phone_declines_invite_with_natural_language() -> None:
    invited_phone = "+554188880781"
    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "convidar Rafa 554188880781 como Analista",
            "provider_message_id": "test-invite-natural-decline-1",
        }
    )

    declined = app_graph.invoke(
        {
            "from_phone": invited_phone,
            "message": "agora não quero participar",
            "provider_message_id": "test-invite-natural-decline-2",
        }
    )

    assert "Convite recusado" in declined["reply"]


def test_unknown_invite_response_guides_without_repeating_full_invite() -> None:
    invited_phone = "+554188880782"
    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "convidar Dani 554188880782 como Comercial",
            "provider_message_id": "test-invite-unknown-response-1",
        }
    )

    result = app_graph.invoke(
        {
            "from_phone": invited_phone,
            "message": "quero saber os detalhes",
            "provider_message_id": "test-invite-unknown-response-2",
        }
    )

    assert "Ainda não consegui confirmar" in result["reply"]
    assert "aceito" in result["reply"]
    assert "recusar" in result["reply"]
    assert "Convidado por" not in result["reply"]

def test_pending_invite_takes_priority_over_existing_onboarding() -> None:
    invited_phone = "+554188880778"
    onboarding = app_graph.invoke(
        {
            "from_phone": invited_phone,
            "message": "oi",
            "provider_message_id": "test-invite-priority-1",
        }
    )
    assert "nome da sua empresa" in onboarding["reply"]

    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "convidar Clara 554188880778 como Operacoes",
            "provider_message_id": "test-invite-priority-2",
        }
    )
    accepted = app_graph.invoke(
        {
            "from_phone": invited_phone,
            "message": "1",
            "provider_message_id": "test-invite-priority-3",
        }
    )

    assert "Convite aceito" in accepted["reply"]
    assert "Clara" in accepted["reply"]
    assert "Operacoes" in accepted["reply"]
    assert "nome da sua empresa" not in accepted["reply"]


def test_uncertain_audio_assignee_asks_confirmation_then_creates_task() -> None:
    first = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Jonas esta rapida a Dairy ate amanha as dezoito",
            "provider_message_id": "test-audio-assignee-confirm-1",
        }
    )

    assert "transcricao parece ter confundido" in first["reply"]
    assert "Ouvi: Jonas" in first["reply"]
    assert "1. Joao" in first["reply"]

    confirmed = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "1",
            "provider_message_id": "test-audio-assignee-confirm-2",
        }
    )

    assert "Tarefa criada" in confirmed["reply"]
    assert "Responsável: Joao" in confirmed["reply"]
    assert confirmed["result"]["should_notify_assignee"] is True


def test_onboarding_does_not_accept_greetings_as_profile_data() -> None:
    phone = "+554188880779"

    welcome = app_graph.invoke(
        {
            "from_phone": phone,
            "message": "Ola",
            "provider_message_id": "test-onboarding-guard-1",
        }
    )
    assert "nome da sua empresa" in welcome["reply"]

    repeated_greeting = app_graph.invoke(
        {
            "from_phone": phone,
            "message": "Ola",
            "provider_message_id": "test-onboarding-guard-2",
        }
    )
    assert "nome da sua empresa" in repeated_greeting["reply"]

    company = app_graph.invoke(
        {
            "from_phone": phone,
            "message": "Commandix",
            "provider_message_id": "test-onboarding-guard-3",
        }
    )
    assert "Agora me diga seu nome" in company["reply"]

    invalid_name = app_graph.invoke(
        {
            "from_phone": phone,
            "message": "Oi",
            "provider_message_id": "test-onboarding-guard-4",
        }
    )
    assert "seu nome completo" in invalid_name["reply"]

    name = app_graph.invoke(
        {
            "from_phone": phone,
            "message": "Matheus Martins",
            "provider_message_id": "test-onboarding-guard-5",
        }
    )
    assert "Qual é o seu cargo" in name["reply"]

    invalid_job_title = app_graph.invoke(
        {
            "from_phone": phone,
            "message": "Boa tarde",
            "provider_message_id": "test-onboarding-guard-6",
        }
    )
    assert "seu cargo" in invalid_job_title["reply"]


def test_onboarding_allows_company_name_correction_before_user_name() -> None:
    phone = "+554188880787"

    app_graph.invoke(
        {
            "from_phone": phone,
            "message": "Ola",
            "provider_message_id": "test-onboarding-company-correction-1",
        }
    )
    app_graph.invoke(
        {
            "from_phone": phone,
            "message": "Commandex",
            "provider_message_id": "test-onboarding-company-correction-2",
        }
    )

    corrected = app_graph.invoke(
        {
            "from_phone": phone,
            "message": "Alterar nome da empresa para Commandix, ok?",
            "provider_message_id": "test-onboarding-company-correction-3",
        }
    )

    assert "Nome da empresa atualizado para Commandix" in corrected["reply"]
    assert "nome completo" in corrected["reply"]

    app_graph.invoke(
        {
            "from_phone": phone,
            "message": "Matheus Martins",
            "provider_message_id": "test-onboarding-company-correction-4",
        }
    )
    completed = app_graph.invoke(
        {
            "from_phone": phone,
            "message": "Desenvolvedor de IA",
            "provider_message_id": "test-onboarding-company-correction-5",
        }
    )

    assert completed["result"]["company_name"] == "Commandix"


def test_store_returns_ambiguous_matches_before_completing() -> None:
    store = InMemoryTaskStore()
    context = store.identify_user("+5511999999999")
    store.create_task(context["company_id"], context["user_id"], "Enviar proposta cliente A")
    store.create_task(context["company_id"], context["user_id"], "Enviar proposta cliente B")

    result = store.complete_task(context["company_id"], context["user_id"], "proposta")

    assert isinstance(result, list)
    assert len(result) == 2
    assert all(task.status == "pending" for task in result)


def test_ambiguous_completion_reply_uses_numbered_options() -> None:
    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "criar tarefa revisar zetaambig alfa hoje",
            "provider_message_id": "test-ambiguous-1",
        }
    )
    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "criar tarefa revisar zetaambig beta hoje",
            "provider_message_id": "test-ambiguous-2",
        }
    )

    result = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "concluir zetaambig",
            "provider_message_id": "test-ambiguous-3",
        }
    )

    assert "Encontrei mais de uma tarefa parecida" in result["reply"]
    assert "numero da tarefa" in result["reply"]
    assert "codigo da tarefa" not in result["reply"]
    assert "1." in result["reply"]
    assert "2." in result["reply"]
    assert "📌" in result["reply"]
    assert "zetaambig alfa" in result["reply"]
    assert "zetaambig beta" in result["reply"]
    assert result["reply"].find("zetaambig alfa") < result["reply"].find("zetaambig beta")


def test_numbered_reply_completes_pending_ambiguous_task() -> None:
    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "criar tarefa revisar lambdachoice alfa hoje",
            "provider_message_id": "test-choice-1",
        }
    )
    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "criar tarefa revisar lambdachoice beta hoje",
            "provider_message_id": "test-choice-2",
        }
    )
    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "concluir lambdachoice",
            "provider_message_id": "test-choice-3",
        }
    )

    completed = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "1",
            "provider_message_id": "test-choice-4",
        }
    )

    assert "✅ Tarefa concluida" in completed["reply"]
    assert "📌" in completed["reply"]
    assert "lambdachoice" in completed["reply"]


def test_extract_choice_number_accepts_voice_ordinals() -> None:
    from whatsapp_task_agent.graph import _extract_choice_number

    assert _extract_choice_number("o primeiro") == 1
    assert _extract_choice_number("a segunda") == 2
    assert _extract_choice_number("concluir o terceiro") == 3
    assert _extract_choice_number("opcao 4") == 4
    assert _extract_choice_number("a de cima") == 1


def test_parser_resolves_voice_natural_deadlines_with_timezone() -> None:
    parsed = parse_message("Joao, enviar proposta hoje fim do dia")
    due = parsed.params.get("due_date")

    assert parsed.action == "create_task"
    assert due is not None
    assert "T18:00:00" in due
    assert due.endswith("-03:00") or due.endswith("-02:00")
    assert "fim do dia" not in parsed.params.get("title", "").lower()


def test_parser_resolves_relative_hours_as_full_datetime() -> None:
    parsed = parse_message("criar tarefa revisar contrato daqui duas horas")
    due = parsed.params.get("due_date")

    assert parsed.action == "create_task"
    assert due is not None
    assert due.endswith("-03:00") or due.endswith("-02:00")
    assert "daqui" not in parsed.params.get("title", "").lower()


def test_parser_resolves_next_week_voice_expression() -> None:
    parsed = parse_message("criar tarefa fechar contrato da Dairy semana que vem")

    assert parsed.action == "create_task"
    assert parsed.params.get("due_date") is not None
    assert parsed.params.get("client_name") == "Dairy"
    assert "semana que vem" not in parsed.params.get("title", "").lower()



def test_contact_share_invite_asks_role_before_sending() -> None:
    first = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Contato compartilhado: Leo",
            "provider_message_id": "test-contact-invite-role-1",
            "contact_share": {"name": "Leo", "phone": "554188881234"},
        }
    )

    assert "Qual será o cargo" in first["reply"]
    assert "Leo" in first["reply"]
    assert first.get("result", {}).get("invite_draft") is True

    second = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Desenvolvedor de IA",
            "provider_message_id": "test-contact-invite-role-2",
        }
    )

    assert "Convite enviado para Leo" in second["reply"]
    assert "Desenvolvedor de IA" in second["reply"]
    assert second["result"]["should_notify_invitee"] is True


def test_contact_share_invite_allows_name_correction_before_role() -> None:
    first = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Contato compartilhado: amorzao",
            "provider_message_id": "test-contact-invite-name-correction-1",
            "contact_share": {"name": "amorzão ❤️", "phone": "554196534097"},
        }
    )

    assert "Qual será o cargo" in first["reply"]
    assert "amorzão" in first["reply"]

    corrected = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Alterar o nome do contato para Maria",
            "provider_message_id": "test-contact-invite-name-correction-2",
        }
    )

    assert "Nome do contato atualizado para Maria" in corrected["reply"]
    assert "Qual será o cargo" in corrected["reply"]
    assert corrected["result"]["params"]["name"] == "Maria"

    sent = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Gestora Comercial",
            "provider_message_id": "test-contact-invite-name-correction-3",
        }
    )

    assert "Convite enviado para Maria" in sent["reply"]
    assert "Gestora Comercial" in sent["reply"]


def test_contact_share_invite_name_correction_handles_audio_transcript_style() -> None:
    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Contato compartilhado: apelido",
            "provider_message_id": "test-contact-invite-name-audio-correction-1",
            "contact_share": {"name": "apelido", "phone": "554196534098"},
        }
    )

    corrected = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "corrigir o nome do contato para maria por favor",
            "provider_message_id": "test-contact-invite-name-audio-correction-2",
        }
    )

    assert corrected["result"]["params"]["name"] == "Maria"
    assert "Nome do contato atualizado para Maria" in corrected["reply"]


def test_contact_share_invite_accepts_job_title_change_phrase() -> None:
    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Contato compartilhado: Clara",
            "provider_message_id": "test-contact-invite-job-title-phrase-1",
            "contact_share": {"name": "Clara", "phone": "554196534099"},
        }
    )

    sent = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "alterar cargo para gestora comercial",
            "provider_message_id": "test-contact-invite-job-title-phrase-2",
        }
    )

    assert "Convite enviado para Clara" in sent["reply"]
    assert "Gestora Comercial" in sent["reply"]


def test_typed_invite_without_role_asks_role_before_sending() -> None:
    first = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "convidar Ana 554188889999",
            "provider_message_id": "test-typed-invite-role-1",
        }
    )

    assert "Qual será o cargo" in first["reply"]
    assert "Ana" in first["reply"]
    assert first.get("result", {}).get("invite_draft") is True

    second = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Gestora Comercial",
            "provider_message_id": "test-typed-invite-role-2",
        }
    )

    assert "Convite enviado para Ana" in second["reply"]
    assert "Gestora Comercial" in second["reply"]
    assert second["result"]["should_notify_invitee"] is True




def test_assignee_acknowledges_delegated_task_naturally() -> None:
    created = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Joao, fazer o deploy da Nanocare ate amanha as 18",
            "provider_message_id": "test-task-ack-1",
        }
    )
    assert "Tarefa criada" in created["reply"]

    acknowledged = app_graph.invoke(
        {
            "from_phone": "+5511988888888",
            "message": "aceito a tarefa, pode deixar",
            "provider_message_id": "test-task-ack-2",
        }
    )

    assert "Marquei como em andamento" in acknowledged["reply"]
    assert "deploy" in acknowledged["reply"].lower()

    listed = app_graph.invoke(
        {
            "from_phone": "+5511988888888",
            "message": "minhas tarefas",
            "provider_message_id": "test-task-ack-3",
        }
    )
    assert "Status: em andamento" in listed["reply"]


def test_assignee_acknowledgement_with_multiple_tasks_asks_which_one() -> None:
    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Joao, revisar contrato da Dairy ate amanha as 10",
            "provider_message_id": "test-task-ack-multiple-1",
        }
    )
    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Joao, ajustar dashboard da Alpha ate amanha as 12",
            "provider_message_id": "test-task-ack-multiple-2",
        }
    )

    acknowledged = app_graph.invoke(
        {
            "from_phone": "+5511988888888",
            "message": "ok vou fazer",
            "provider_message_id": "test-task-ack-multiple-3",
        }
    )

    assert "Qual tarefa" in acknowledged["reply"]
    assert "1." in acknowledged["reply"]
    assert "2." in acknowledged["reply"]

    selected = app_graph.invoke(
        {
            "from_phone": "+5511988888888",
            "message": "a primeira",
            "provider_message_id": "test-task-ack-multiple-4",
        }
    )
    assert "Em andamento" in selected["reply"]


def test_assignee_can_request_more_time_with_new_due_date() -> None:
    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Joao, fazer o deploy da Nanocare ate amanha as 18",
            "provider_message_id": "test-more-time-date-1",
        }
    )

    result = app_graph.invoke(
        {
            "from_phone": "+5511988888888",
            "message": "preciso de mais prazo ate sexta as 10",
            "provider_message_id": "test-more-time-date-2",
        }
    )

    assert "Prazo atualizado" in result["reply"]
    assert "Tarefa:" in result["reply"]
    assert "Novo prazo:" in result["reply"]


def test_assignee_can_request_more_time_then_send_only_due_date() -> None:
    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Joao, revisar proposta da Alpha ate amanha as 18",
            "provider_message_id": "test-more-time-step-1",
        }
    )

    first = app_graph.invoke(
        {
            "from_phone": "+5511988888888",
            "message": "preciso de mais prazo",
            "provider_message_id": "test-more-time-step-2",
        }
    )

    assert "Qual novo prazo" in first["reply"]
    assert "Revisar proposta" in first["reply"]

    second = app_graph.invoke(
        {
            "from_phone": "+5511988888888",
            "message": "sexta as 10",
            "provider_message_id": "test-more-time-step-3",
        }
    )

    assert "Prazo atualizado" in second["reply"]
    assert "Novo prazo:" in second["reply"]


def test_assignee_can_ask_task_details_naturally() -> None:
    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Joao, ajustar o dashboard da Alpha ate amanha as 12",
            "provider_message_id": "test-task-details-1",
        }
    )

    result = app_graph.invoke(
        {
            "from_phone": "+5511988888888",
            "message": "qual era mesmo a tarefa?",
            "provider_message_id": "test-task-details-2",
        }
    )

    assert "Esta é a tarefa" in result["reply"]
    assert "Ajustar o dashboard" in result["reply"]
    assert "Prazo:" in result["reply"]


def test_assignee_can_say_cannot_do_task_without_fallback() -> None:
    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Joao, validar integraçao da Dairy ate amanha as 15",
            "provider_message_id": "test-cannot-do-1",
        }
    )

    result = app_graph.invoke(
        {
            "from_phone": "+5511988888888",
            "message": "não vou conseguir fazer essa tarefa",
            "provider_message_id": "test-cannot-do-2",
        }
    )

    assert "Mantive a tarefa aberta" in result["reply"]
    assert "preciso de mais prazo" in result["reply"]
    assert "faltou clareza" not in result["reply"]


def test_assignee_can_ask_for_help_on_task() -> None:
    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Joao, validar integraçao da Dairy ate amanha as 15",
            "provider_message_id": "test-needs-help-1",
        }
    )

    result = app_graph.invoke(
        {
            "from_phone": "+5511988888888",
            "message": "preciso de ajuda com essa tarefa",
            "provider_message_id": "test-needs-help-2",
        }
    )

    assert "ajuda" in result["reply"].lower()
    assert "Mantive a tarefa aberta" in result["reply"]
    assert "faltou clareza" not in result["reply"]


def test_assignee_can_request_task_reassignment() -> None:
    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Joao, validar integraçao da Dairy ate amanha as 15",
            "provider_message_id": "test-reassign-1",
        }
    )

    result = app_graph.invoke(
        {
            "from_phone": "+5511988888888",
            "message": "isso não é comigo, manda para outra pessoa",
            "provider_message_id": "test-reassign-2",
        }
    )

    assert "Mantive a tarefa aberta" in result["reply"] or "tarefa" in result["reply"].lower()
    assert "avisei" in result["reply"].lower() or "repassar" in result["reply"].lower()
    assert "faltou clareza" not in result["reply"]


def test_parser_understands_global_client_rename() -> None:
    parsed = parse_message("renomear cliente Derry para Dairy")

    assert parsed.action == Action.edit_client
    assert parsed.params["current_name"] == "Derry"
    assert parsed.params["new_name"] == "Dairy"


def test_parser_understands_specific_task_client_change() -> None:
    parsed = parse_message("mudar o cliente da tarefa injetar mais documentos para Dairy")

    assert parsed.action == Action.edit_task
    assert parsed.params["task_reference"] == "Injetar mais documentos"
    assert parsed.params["new_client_name"] == "Dairy"


def test_parser_understands_member_correction() -> None:
    parsed = parse_message("corrigir colaborador Joao para Joao Silva")

    assert parsed.action == Action.edit_member
    assert parsed.params["current_name"] == "Joao"
    assert parsed.params["new_name"] == "Joao Silva"


def test_parser_understands_member_job_title_change() -> None:
    parsed = parse_message("alterar cargo do Joao para Desenvolvedor de IA")

    assert parsed.action == Action.edit_member
    assert parsed.params["current_name"] == "Joao"
    assert parsed.params["new_job_title"] == "Desenvolvedor de IA"


def test_parser_understands_self_job_title_change() -> None:
    parsed = parse_message("alterar meu cargo para CEO")

    assert parsed.action == Action.edit_member
    assert parsed.params["target_self"] is True
    assert parsed.params["new_job_title"] == "CEO"


def test_memory_store_renames_client_across_tasks() -> None:
    memory_store = InMemoryTaskStore()
    context = memory_store.identify_user("+5511999999999")
    task = memory_store.create_task(
        context["company_id"],
        context["user_id"],
        "Injetar documentos",
        client_name="Derry",
    )

    result = memory_store.update_client_name(context["company_id"], "Derry", "Dairy")

    assert result is not None
    assert result["renamed"] is True
    assert result["affected_tasks"] == 1
    assert task.client_name == "Dairy"
    assert memory_store.list_tasks(context["company_id"], context["user_id"], client_name="Dairy") == [task]


def test_memory_store_updates_member_job_title() -> None:
    memory_store = InMemoryTaskStore()
    context = memory_store.identify_user("+5511999999999")
    updated = memory_store.update_member_job_title(context["company_id"], str(memory_store.joao_id), "Desenvolvedor")

    assert updated is True
    members = memory_store.list_company_members(context["company_id"])
    joao = next(member for member in members if member["name"] == "Joao")
    assert joao["job_title"] == "Desenvolvedor"


def test_graph_renames_client_and_specific_task_edit_reply_shows_client() -> None:
    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Joao, revisar contrato da Zedry amanha as 18",
            "provider_message_id": "test-client-rename-create-1",
        }
    )

    renamed = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "renomear cliente Zedry para Zairy",
            "provider_message_id": "test-client-rename-1",
        }
    )

    assert "Cliente atualizado" in renamed["reply"]
    assert "Zedry → Zairy" in renamed["reply"]

    app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "Joao, ajustar proposta da Clidro amanha as 18",
            "provider_message_id": "test-task-client-edit-create-1",
        }
    )
    edited = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "mudar o cliente da tarefa ajustar proposta para Clidra",
            "provider_message_id": "test-task-client-edit-1",
        }
    )

    assert "Tarefa atualizada" in edited["reply"]
    assert "Cliente: Clidra" in edited["reply"]


def test_graph_updates_member_job_title() -> None:
    updated = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "alterar cargo do Joao para Desenvolvedor",
            "provider_message_id": "test-member-job-title-1",
        }
    )

    assert "Cargo atualizado" in updated["reply"]
    assert "Joao: Desenvolvedor" in updated["reply"]
    assert updated["result"]["new_job_title"] == "Desenvolvedor"


def test_graph_updates_self_job_title() -> None:
    updated = app_graph.invoke(
        {
            "from_phone": "+5511999999999",
            "message": "alterar meu cargo para CEO",
            "provider_message_id": "test-member-job-title-self-1",
        }
    )

    assert "Cargo atualizado" in updated["reply"]
    assert "Ana: CEO" in updated["reply"]
