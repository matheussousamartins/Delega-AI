from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "local"
    database_url: str | None = None
    evolution_api_base_url: str | None = None
    evolution_instance_name: str | None = None
    evolution_api_key: str | None = None
    evolution_send_enabled: bool = False
    evolution_typing_enabled: bool = True
    evolution_typing_delay_ms: int = 500
    evolution_typing_thinking_delay_ms: int = 1500
    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"
    openai_transcription_model: str = "gpt-4o-mini-transcribe"
    audio_max_bytes: int = 10 * 1024 * 1024
    min_execution_confidence: int = 65
    reminder_job_secret: str | None = None
    evolution_webhook_secret: str | None = None
    db_pool_min_size: int = 2
    db_pool_max_size: int = 10
    overdue_reminder_max_age_days: int = 7
    quiet_hours_enabled: bool = False
    quiet_hours_start: int = 22
    quiet_hours_end: int = 7
    quiet_hours_timezone: str = "America/Sao_Paulo"
    tts_enabled: bool = False
    tts_voice: str = "nova"
    conversation_history_limit: int = 10
    pending_choice_ttl_minutes: int = 30
    pending_draft_ttl_hours: int = 4
    pending_invite_ttl_hours: int = 24
    observe_api_key: str | None = None

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
