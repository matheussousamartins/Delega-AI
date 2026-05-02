from whatsapp_task_agent.store import InMemoryTaskStore


def test_memory_store_completes_onboarding() -> None:
    store = InMemoryTaskStore()
    phone = "+554188880000"

    session = store.start_onboarding_session(phone)
    assert session["step"] == "company_name"

    store.update_onboarding_session(phone, company_name="Commandix", step="user_name")
    store.update_onboarding_session(phone, user_name="Matheus", step="job_title")
    store.update_onboarding_session(phone, job_title="Desenvolvedor", step="completed")
    context = store.complete_onboarding_session(phone)

    assert context["company_name"] == "Commandix"
    assert context["user_name"] == "Matheus"
    assert context["job_title"] == "Desenvolvedor"
    assert context["role"] == "owner"
    assert store.identify_user(phone)["user_name"] == "Matheus"
