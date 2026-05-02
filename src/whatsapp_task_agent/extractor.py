import json

from openai import OpenAI, OpenAIError

from whatsapp_task_agent.logger import get_logger
from whatsapp_task_agent.settings import settings

logger = get_logger(__name__)

_STEP_CONFIGS: dict[str, dict] = {
    "company_name": {
        "instruction": (
            "Extraia o nome da empresa ou negócio da mensagem abaixo. "
            "Retorne SOMENTE o JSON: {\"value\": \"Nome da Empresa\"} ou {\"value\": null} se não encontrado. "
            "Exemplos: 'minha empresa é Commandix' → Commandix | 'Delega AI' → Delega AI | "
            "'trabalho na TechCorp' → TechCorp | 'oi' → null"
        ),
        "max_len": 80,
    },
    "user_name": {
        "instruction": (
            "Extraia o nome completo da pessoa da mensagem abaixo. "
            "Retorne SOMENTE o JSON: {\"value\": \"Nome Completo\"} ou {\"value\": null} se não encontrado. "
            "Exemplos: 'pode me chamar de Matheus' → Matheus | 'sou o João Silva' → João Silva | "
            "'meu nome é Ana Costa' → Ana Costa | 'oi tudo bem' → null"
        ),
        "max_len": 80,
    },
    "job_title": {
        "instruction": (
            "Extraia o cargo ou função profissional da mensagem abaixo. "
            "Retorne SOMENTE o JSON: {\"value\": \"Cargo\"} ou {\"value\": null} se não encontrado. "
            "Capitalize corretamente. "
            "Exemplos: 'trabalho como CEO' → CEO | 'sou desenvolvedor back-end' → Desenvolvedor Back-end | "
            "'gerente comercial' → Gerente Comercial | 'não sei' → null"
        ),
        "max_len": 60,
    },
}


def extract_onboarding_value(step: str, raw_message: str) -> str | None:
    config = _STEP_CONFIGS.get(step)
    if not config or not settings.openai_api_key:
        return None

    client = OpenAI(api_key=settings.openai_api_key)
    try:
        response = client.chat.completions.create(
            model=settings.openai_model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": config["instruction"]},
                {"role": "user", "content": raw_message},
            ],
        )
        data = json.loads(response.choices[0].message.content or "{}")
        value = data.get("value")
        if not value or not isinstance(value, str):
            return None
        value = value.strip()
        if len(value) < 2 or len(value) > config["max_len"]:
            return None
        logger.info("onboarding_extraction_ok", extra={"step": step, "chars": len(value)})
        return value
    except OpenAIError as exc:
        logger.error(
            "onboarding_extraction_openai_error",
            extra={"step": step, "exc_type": type(exc).__name__},
            exc_info=True,
        )
        return None
    except Exception as exc:
        logger.error(
            "onboarding_extraction_failed",
            extra={"step": step, "exc_type": type(exc).__name__},
            exc_info=True,
        )
        return None
