"""Application settings (LLD 0.3). Keys are added phase by phase as they are needed."""

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="AGENT_", extra="ignore", populate_by_name=True
    )

    env: str = "dev"  # dev | test | prod
    timezone: str = "Asia/Kolkata"
    log_level: str = "INFO"

    # NLU (Phase 3): stub | rules | hybrid (rules first, Gemini for what the rules miss)
    nlu_engine: str = "hybrid"
    nlu_confidence_threshold: float = 0.7
    nlu_timeout_s: float = 4.0
    gemini_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "gemini_api_key", "AGENT_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"
        ),
    )
    # Alias of the newest Gemini Flash model; pin a specific version in prod
    # (e.g. AGENT_GEMINI_MODEL=gemini-3.8-flash) for reproducible behaviour.
    gemini_model: str = "gemini-flash-latest"
    # Gemini 3 "thinking_level"; "minimal" keeps latency low. Empty/None disables the setting
    # (older models that do not support it are retried without it automatically).
    gemini_thinking_level: str | None = "minimal"

    # Domain
    slot_duration_min: int = 30
    business_hours: tuple[int, int] = (9, 18)  # IST, [start, end)
    booking_horizon_days: int = 14
    lead_time_min: int = 120
    mock_calendar_path: str = "data/mock_calendar.json"

    # Sessions
    session_ttl_min: int = 30
    record_golden: bool = False
    golden_dir: str = "tests/golden"

    # Chat HTTP API limits (LLD 2.12)
    sessions_per_min_per_ip: int = 10
    messages_per_min_per_session: int = 30

    # MCP tools (Phase 3B, LLD 3.9-3.13). Off by default so the chat runs without Google
    # credentials; set AGENT_MCP_TOOLS_ENABLED=true once the Google setup below is done.
    mcp_tools_enabled: bool = False
    # "inprocess" | path to the server script (stdio) | "http://host:8001/mcp"
    mcp_server_target: str = "inprocess"
    mcp_llm_tool_calling: bool = True  # Gemini proposes the calls (gated); else plan only
    mcp_max_tool_calls_per_turn: int = 4
    mcp_call_timeout_s: float = 10.0
    mcp_agent_timeout_s: float = 6.0

    # Google (used only by the FastMCP server process)
    google_calendar_id: str = "primary"
    google_prebooking_doc_id: str | None = None  # the "Advisor Pre-Bookings" Google Doc id
    google_advisor_email: str | None = None  # recipient of the (never sent) drafts
    google_credentials_file: str = "secrets/google_oauth_client.json"
    google_token_file: str = "secrets/google_token.json"
    google_service_account_file: str | None = None
    google_delegated_user: str | None = None

    # Phase 4: storage, outbox, secure link, PII vault (LLD 4.1-4.6)
    database_url: str = "sqlite:///./data/agent.db"
    outbox_poll_s: float = 1.0
    public_base_url: str = "http://localhost:8000"
    link_hmac_secret: SecretStr | None = None
    link_ttl_hours: int = 48
    vault_fernet_key: SecretStr | None = None

    # Phase 5/6 (LLD 5.1, 6.2)
    waitlist_mode: Literal["hold", "notes_only"] = "hold"  # transparent waitlist hold or notes
    cancel_draft_enabled: bool = True  # prepare the advisor 'cancel' Gmail draft on cancel

    # Phase 7 voice (LLD 7). "google" = Cloud Speech-to-Text + Text-to-Speech using
    # AGENT_GOOGLE_SERVICE_ACCOUNT_FILE (both APIs must be enabled on its project);
    # "browser" = the Web Speech preview only. Google falls back to browser when unavailable.
    voice_provider: Literal["google", "browser"] = "google"
    voice_language: str = "en-IN"
    voice_tts_voice: str = "en-IN-Neural2-A"
    voice_speaking_rate: float = 1.0
    voice_stt_model: str = "latest_short"
    voice_max_utterance_s: int = 20
    voice_requests_per_min_per_ip: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
