"""Kingsley runtime configuration.

Everything is environment-driven so no secret ever lands in the repository.
Missing keys never crash the app: a provider without credentials is simply
skipped by the router and reported as ``not-configured`` in the UI.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("KINGSLEY_HOME", str(REPO_ROOT / ".kingsley"))).expanduser()
DB_PATH = HOME / "kingsley.db"

# Task -> provider preference order. This is the whole point of the router:
# Gemini owns Google-shaped work, Grok owns news, Claude owns code, ChatGPT owns
# ideation, FreeLLMAPI is the free safety net, Ollama is the offline floor.
TASK_ROUTES: dict[str, tuple[str, ...]] = {
    "code": ("claude", "freellmapi", "gpt", "gemini", "ollama"),
    "news": ("grok", "gemini", "freellmapi", "gpt", "ollama"),
    "google": ("gemini", "freellmapi", "gpt", "ollama"),
    "ideas": ("gpt", "claude", "gemini", "freellmapi", "ollama"),
    "homework": ("gpt", "claude", "gemini", "freellmapi", "ollama"),
    "vision": ("gemini", "gpt", "claude", "freellmapi", "ollama"),
    "fast": ("freellmapi", "gemini", "ollama", "gpt"),
    "default": ("freellmapi", "gemini", "gpt", "claude", "grok", "ollama"),
}

TASK_KINDS = tuple(k for k in TASK_ROUTES if k != "default")

GOOGLE_SCOPES = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
)


@dataclass(frozen=True)
class Provider:
    """One upstream model endpoint behind the router."""

    slug: str
    label: str
    kind: str  # "openai" | "anthropic" | "gemini"
    base_url: str
    api_key: str
    model: str
    vision: bool = False
    free: bool = False

    @property
    def configured(self) -> bool:
        if self.free:  # local routers: presence of a base URL is the config
            return bool(self.base_url)
        return bool(self.api_key)

    @property
    def is_local(self) -> bool:
        return self.slug in {"freellmapi", "ollama"}

    def public(self) -> dict[str, object]:
        return {
            "slug": self.slug,
            "label": self.label,
            "kind": self.kind,
            "model": self.model,
            "configured": self.configured,
            "vision": self.vision,
            "free": self.free,
            "local": self.is_local,
        }


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def load_providers() -> dict[str, Provider]:
    """Build the provider table from the current environment."""
    providers = [
        Provider(
            slug="freellmapi",
            label="FreeLLMAPI",
            kind="openai",
            base_url=_env("FREELLMAPI_BASE_URL", default="http://127.0.0.1:3001/v1").rstrip("/"),
            api_key=_env("FREELLMAPI_API_KEY", default="freellmapi-local"),
            model=_env("FREELLMAPI_MODEL", default="auto"),
            vision=True,
            free=True,
        ),
        Provider(
            slug="gemini",
            label="Gemini",
            kind="gemini",
            base_url=_env("GEMINI_BASE_URL", default="https://generativelanguage.googleapis.com").rstrip("/"),
            api_key=_env("GEMINI_API_KEY", "GOOGLE_API_KEY"),
            model=_env("KINGSLEY_GEMINI_MODEL", default="gemini-2.5-flash"),
            vision=True,
            free=True,
        ),
        Provider(
            slug="gpt",
            label="ChatGPT",
            kind="openai",
            base_url=_env("OPENAI_BASE_URL", default="https://api.openai.com/v1").rstrip("/"),
            api_key=_env("OPENAI_API_KEY"),
            model=_env("KINGSLEY_GPT_MODEL", default="gpt-5"),
            vision=True,
        ),
        Provider(
            slug="claude",
            label="Claude",
            kind="anthropic",
            base_url=_env("ANTHROPIC_BASE_URL", default="https://api.anthropic.com").rstrip("/"),
            api_key=_env("ANTHROPIC_API_KEY"),
            model=_env("KINGSLEY_CLAUDE_MODEL", default="claude-sonnet-4-5"),
            vision=True,
        ),
        Provider(
            slug="grok",
            label="Grok",
            kind="openai",
            base_url=_env("XAI_BASE_URL", default="https://api.x.ai/v1").rstrip("/"),
            api_key=_env("XAI_API_KEY"),
            model=_env("KINGSLEY_GROK_MODEL", default="grok-4"),
            vision=True,
        ),
        Provider(
            slug="ollama",
            label="Ollama (local)",
            kind="openai",
            base_url=_env("OLLAMA_HOST", default="http://127.0.0.1:11434").rstrip("/") + "/v1",
            api_key="ollama",
            model=_env("KINGSLEY_OLLAMA_MODEL", default="llama3.2"),
            vision=True,
            free=True,
        ),
    ]
    return {p.slug: p for p in providers}


def workspace_root() -> Path:
    """Filesystem root the repo agent is allowed to touch."""
    raw = _env("KINGSLEY_WORKSPACE", default=str(REPO_ROOT))
    return Path(raw).expanduser().resolve()


def interests() -> list[str]:
    raw = _env("KINGSLEY_INTERESTS")
    return [item.strip() for item in raw.split(",") if item.strip()]


def google_credentials() -> tuple[str, str]:
    return _env("GOOGLE_CLIENT_ID"), _env("GOOGLE_CLIENT_SECRET")


def public_url() -> str:
    """Origin the OAuth callback is registered against."""
    return _env("KINGSLEY_PUBLIC_URL", default="http://localhost:3000").rstrip("/")


def ideal_minutes_per_task() -> int:
    try:
        return max(15, int(_env("KINGSLEY_TASK_MINUTES", default="45")))
    except ValueError:
        return 45
