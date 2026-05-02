from datetime import datetime

from whatsapp_task_agent.schemas import InviteStatus
from whatsapp_task_agent.store import InMemoryTaskStore
from whatsapp_task_agent.parser import parse_message


def test_memory_store_creates_pending_invite() -> None:
    store = InMemoryTaskStore()
    context = store.identify_user("+5511999999999")

    invite = store.create_invite(
        company_id=context["company_id"],
        invited_by=context["user_id"],
        phone="+554188880000",
        name="Joao",
        job_title="Desenvolvedor",
    )

    assert invite["status"] == InviteStatus.pending.value
    assert invite["name"] == "Joao"
    assert invite["job_title"] == "Desenvolvedor"
    assert store.get_pending_invite_by_phone("+554188880000")["id"] == invite["id"]


def test_memory_store_upserts_pending_invite_for_same_company_and_phone() -> None:
    store = InMemoryTaskStore()
    context = store.identify_user("+5511999999999")

    first = store.create_invite(
        company_id=context["company_id"],
        invited_by=context["user_id"],
        phone="+554188880000",
        name="Joao",
        job_title="Desenvolvedor",
    )
    second = store.create_invite(
        company_id=context["company_id"],
        invited_by=context["user_id"],
        phone="+554188880000",
        name="Joao Silva",
        job_title="Tech Lead",
    )

    assert second["id"] == first["id"]
    assert second["name"] == "Joao Silva"
    assert second["job_title"] == "Tech Lead"


def test_memory_store_marks_invite_as_accepted() -> None:
    store = InMemoryTaskStore()
    context = store.identify_user("+5511999999999")
    invite = store.create_invite(
        company_id=context["company_id"],
        invited_by=context["user_id"],
        phone="+554188880000",
    )

    updated = store.mark_invite_status(
        invite_id=invite["id"],
        status=InviteStatus.accepted.value,
        timestamp=datetime(2026, 4, 30, 12, 0),
    )

    assert updated["status"] == InviteStatus.accepted.value
    assert updated["accepted_at"] == datetime(2026, 4, 30, 12, 0)
    assert store.get_pending_invite_by_phone("+554188880000") is None


def test_memory_store_accepts_invite_and_registers_member() -> None:
    store = InMemoryTaskStore()
    context = store.identify_user("+5511999999999")
    invite = store.create_invite(
        company_id=context["company_id"],
        invited_by=context["user_id"],
        phone="+554188880001",
        name="Pedro",
        job_title="Designer",
    )

    accepted = store.accept_invite(invite["id"], "+554188880001")
    identified = store.identify_user("+554188880001")

    assert accepted["user_name"] == "Pedro"
    assert accepted["job_title"] == "Designer"
    assert accepted["role"] == "member"
    assert identified["company_id"] == context["company_id"]
    assert identified["user_name"] == "Pedro"
    assert identified["role"] == "member"
    assert store.get_pending_invite_by_phone("+554188880001") is None


def test_memory_store_accept_invite_clears_onboarding_session() -> None:
    store = InMemoryTaskStore()
    context = store.identify_user("+5511999999999")
    invited_phone = "+554188880002"
    store.start_onboarding_session(invited_phone)
    invite = store.create_invite(
        company_id=context["company_id"],
        invited_by=context["user_id"],
        phone=invited_phone,
        name="Marina",
        job_title="Gestora",
    )

    accepted = store.accept_invite(invite["id"], invited_phone)

    assert accepted["user_name"] == "Marina"
    assert store.get_onboarding_session(invited_phone) is None


def test_memory_store_finds_invite_with_brazilian_ninth_digit_variant() -> None:
    store = InMemoryTaskStore()
    context = store.identify_user("+5511999999999")
    invite = store.create_invite(
        company_id=context["company_id"],
        invited_by=context["user_id"],
        phone="+551198757341",
        name="Luiz",
        job_title="Desenvolvedor",
    )

    found = store.get_pending_invite_by_phone("+5511998757341")
    accepted = store.accept_invite(invite["id"], "+5511998757341")

    assert found["id"] == invite["id"]
    assert accepted["user_name"] == "Luiz"
    assert store.identify_user("+551198757341")["user_name"] == "Luiz"


def test_memory_store_matches_member_by_nickname_prefix() -> None:
    store = InMemoryTaskStore()
    context = store.identify_user("+5511999999999")
    invite = store.create_invite(
        company_id=context["company_id"],
        invited_by=context["user_id"],
        phone="+554188880003",
        name="Leozin",
        job_title="Desenvolvedor",
    )
    accepted = store.accept_invite(invite["id"], "+554188880003")

    task = store.create_task(
        company_id=context["company_id"],
        created_by=context["user_id"],
        title="Testar API",
        assignee_name="Leozinho",
    )

    assert str(task.assigned_to) == accepted["user_id"]


def test_parser_extracts_invite_command() -> None:
    parsed = parse_message("convidar Joao 554188880000 como Desenvolvedor")

    assert parsed.action == "invite_user"
    assert parsed.params["name"] == "Joao"
    assert parsed.params["phone"] == "+554188880000"
    assert parsed.params["job_title"] == "Desenvolvedor"
