"""Local configuration; credentials are never included in repr output."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"


@dataclass(frozen=True)
class Settings:
    database_path: Path
    stripe_secret_key: str = field(default="", repr=False)
    gemini_api_key: str = field(default="", repr=False)
    gemini_model: str = DEFAULT_GEMINI_MODEL
    gemini_fallback_model: str = ""
    agent_tool_budget: int = 5
    agent_context_mode: str = "structured"
    agent_fallback_model: str = ""
    gemini_requests_per_minute: int = 15
    model_pacing_path: Path = PROJECT_ROOT / 'instance' / 'model-pacing.db'

    @classmethod
    def from_environment(cls):
        values = {**dotenv_values(PROJECT_ROOT / ".env"), **os.environ}
        database_path = Path(values.get("DATABASE_PATH") or "instance/payresolve.db")
        if not database_path.is_absolute():
            database_path = PROJECT_ROOT / database_path
        try:
            budget = int(values.get('AGENT_TOOL_BUDGET') or '5')
        except ValueError:
            budget = 0
        mode = values.get('AGENT_CONTEXT_MODE') or 'structured'
        try:
            rpm = int(values.get('GEMINI_REQUESTS_PER_MINUTE') or '15')
        except ValueError:
            rpm = 0
        if not 1 <= rpm <= 60:
            from .stripe_gateway import SandboxError
            raise SandboxError('invalid_agent_config', 'Use GEMINI_REQUESTS_PER_MINUTE=1..60.', 400)
        if not 1 <= budget <= 10 or mode not in ('structured', 'history'):
            from .stripe_gateway import SandboxError
            raise SandboxError('invalid_agent_config', 'Use AGENT_TOOL_BUDGET=1..10 and AGENT_CONTEXT_MODE=structured or history.', 400)
        return cls(
            database_path,
            (values.get("STRIPE_SECRET_KEY") or "").strip(),
            (values.get("GEMINI_API_KEY") or "").strip(),
            (values.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL).strip(),
            (values.get("GEMINI_FALLBACK_MODEL") or "").strip(),
            budget, mode, (values.get('AGENT_FALLBACK_MODEL') or '').strip(), rpm,
        )
