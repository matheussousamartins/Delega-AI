import pytest
from unittest.mock import patch

from whatsapp_task_agent.store import InMemoryTaskStore


@pytest.fixture(autouse=True)
def isolated_store():
    """Fresh in-memory store per test — fast, isolated, no DB, idempotent.

    Also forces the local parser by patching away the LLM call so tests are
    deterministic and don't incur API costs or depend on model behaviour.
    Tests that explicitly want LLM behaviour can override this fixture.
    """
    mem = InMemoryTaskStore()
    with (
        patch("whatsapp_task_agent.store.store", mem),
        patch("whatsapp_task_agent.graph.store", mem),
        patch("whatsapp_task_agent.tools.store", mem),
        patch("whatsapp_task_agent.api.store", mem),
        # Force deterministic local parser — disable LLM for all tests by default
        patch("whatsapp_task_agent.parser._parse_with_llm_safely", return_value=None),
    ):
        yield mem
