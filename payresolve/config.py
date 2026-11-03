"""Local configuration; credentials are never included in repr output."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    database_path: Path
    stripe_secret_key: str = field(default="", repr=False)
    gemini_api_key: str = field(default="", repr=False)
    gemini_model: str = "gemini-3.8-flash"
    gemini_fallback_model: str = ""

    @classmethod
    def from_environment(cls):
        values = {**dotenv_values(PROJECT_ROOT / ".env"), **os.environ}
        database_path = Path(values.get("DATABASE_PATH") or "instance/payresolve.db")
        if not database_path.is_absolute():
            database_path = PROJECT_ROOT / database_path
        return cls(
            database_path,
            (values.get("STRIPE_SECRET_KEY") or "").strip(),
            (values.get("GEMINI_API_KEY") or "").strip(),
            (values.get("GEMINI_MODEL") or "gemini-3.8-flash").strip(),
            (values.get("GEMINI_FALLBACK_MODEL") or "").strip(),
        )
