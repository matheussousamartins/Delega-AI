from whatsapp_task_agent.evolution_client import EvolutionClient


def test_evolution_client_sends_text_with_encoded_instance(monkeypatch) -> None:
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "PENDING"}

    class FakeClient:
        def __init__(self, timeout, trust_env):
            captured["timeout"] = timeout
            captured["trust_env"] = trust_env

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return None

        def post(self, url, headers, json):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return FakeResponse()

    def fake_client(timeout, trust_env):
        return FakeClient(timeout=timeout, trust_env=trust_env)

    monkeypatch.setattr("httpx.Client", fake_client)

    client = EvolutionClient(
        base_url="https://evoapi.example.com/",
        instance_name="Delega AI",
        api_key="secret",
    )
    response = client.send_text("+55 (41) 98786-0818", "Teste")

    assert response["status"] == "PENDING"
    assert captured["trust_env"] is False
    assert captured["url"] == "https://evoapi.example.com/message/sendText/Delega%20AI"
    assert captured["headers"]["apikey"] == "secret"
    assert captured["json"] == {"number": "5541987860818", "text": "Teste"}


def test_evolution_client_sends_typing_presence(monkeypatch) -> None:
    """send_presence uses the flat payload format (Evolution API v2+)."""
    calls: list[dict] = []

    class FakeResponse:
        status_code = 200
        content = b""

        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "ok"}

    class FakeHttpClient:
        def __init__(self, timeout, trust_env):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def post(self, url, headers, json):
            calls.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    monkeypatch.setattr("httpx.Client", lambda **kw: FakeHttpClient(**kw))

    client = EvolutionClient(
        base_url="https://evoapi.example.com/",
        instance_name="Delega AI",
        api_key="secret",
    )
    response = client.send_presence("+55 (41) 98786-0818", delay_ms=1800)

    assert response["status"] == "ok"
    assert len(calls) == 1, "flat format succeeded — no fallback expected"
    assert calls[0]["url"] == "https://evoapi.example.com/chat/sendPresence/Delega%20AI"
    assert calls[0]["headers"]["apikey"] == "secret"
    assert calls[0]["json"] == {
        "number": "5541987860818",
        "delay": 1800,
        "presence": "composing",
    }


def test_evolution_client_sends_typing_presence_fallback_options_format(monkeypatch) -> None:
    """Falls back to options-wrapped payload when Evolution returns 400."""
    calls: list[dict] = []

    class FakeResponse400:
        status_code = 400
        content = b'{"error": "bad request"}'

        def raise_for_status(self):
            return None

    class FakeResponse200:
        status_code = 200
        content = b""

        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "ok"}

    class FakeHttpClient:
        def __init__(self, timeout, trust_env):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def post(self, url, headers, json):
            calls.append(json)
            return FakeResponse400() if len(calls) == 1 else FakeResponse200()

    monkeypatch.setattr("httpx.Client", lambda **kw: FakeHttpClient(**kw))

    client = EvolutionClient(
        base_url="https://evoapi.example.com/",
        instance_name="Delega AI",
        api_key="secret",
    )
    response = client.send_presence("+55 (41) 98786-0818", delay_ms=1800)

    assert response["status"] == "ok"
    assert len(calls) == 2
    assert calls[0] == {"number": "5541987860818", "delay": 1800, "presence": "composing"}
    assert calls[1] == {"number": "5541987860818", "options": {"delay": 1800, "presence": "composing"}}
