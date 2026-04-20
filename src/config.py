from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .llm_provider import SUPPORTED_PROVIDERS

ROOT = Path(__file__).resolve().parent.parent
_log = logging.getLogger("readme_agent")


class LLMNotConfigured(RuntimeError):
    """Raised when .env has no usable LLM provider/key pair.

    Distinguished from generic RuntimeError so main() can offer an interactive
    first-run setup instead of just printing and exiting.
    """


# Default model per provider. User can override via <PROVIDER>_MODEL env var.
DEFAULT_MODELS = {
    "anthropic": "claude-opus-4-7",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.5-flash",
}

# Tolerated misspellings — users trip on these constantly.
_ENV_ALIASES: dict[str, tuple[str, ...]] = {
    "ANTHROPIC_API_KEY": ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"),
    "ANTHROPIC_MODEL":   ("ANTHROPIC_MODEL", "CLAUDE_MODEL"),
    "OPENAI_API_KEY":    ("OPENAI_API_KEY", "OPEN_AI_API_KEY", "OPEN_AI_KEY", "OPENAI_KEY"),
    "OPENAI_MODEL":      ("OPENAI_MODEL", "OPEN_AI_MODEL", "OPEN_AI_API_MODEL"),
    "GEMINI_API_KEY":    ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_KEY"),
    "GEMINI_MODEL":      ("GEMINI_MODEL", "GOOGLE_MODEL"),
    "LLM_PROVIDER":      ("LLM_PROVIDER",),
    "LLM_API_KEY":       ("LLM_API_KEY",),
    "LLM_MODEL":         ("LLM_MODEL",),
}


def _env(canonical: str) -> str:
    """Case-insensitive env lookup across known aliases. Returns "" if unset."""
    aliases = _ENV_ALIASES.get(canonical, (canonical,))
    upper_env = {k.upper(): (k, v) for k, v in os.environ.items()}
    for alias in aliases:
        hit = upper_env.get(alias.upper())
        if hit and hit[1].strip():
            actual_name, value = hit
            if actual_name not in aliases:  # case-only mismatch; fine but log at debug
                _log.debug("Resolved %s via env var '%s' (case-insensitive).", canonical, actual_name)
            elif actual_name != canonical:
                _log.warning(
                    "Found env var '%s' — using it as %s. Consider renaming to '%s' for clarity.",
                    actual_name, canonical, canonical,
                )
            return value.strip()
    return ""


# Common display-name → API-ID normalization. Only Anthropic because GPT/Gemini
# IDs don't collide with human-friendly names as often.
_ANTHROPIC_DISPLAY_MAP = {
    "claude haiku 4.5":   "claude-haiku-4-5",
    "claude sonnet 4.6":  "claude-sonnet-4-6",
    "claude opus 4.7":    "claude-opus-4-7",
}


def _normalize_model(provider: str, model: str) -> str:
    """Gently correct obvious display-name → API-ID mistakes. Pass through otherwise."""
    m = model.strip()
    if not m:
        return m
    if provider == "anthropic":
        key = m.lower()
        if key in _ANTHROPIC_DISPLAY_MAP:
            fixed = _ANTHROPIC_DISPLAY_MAP[key]
            _log.warning(
                "ANTHROPIC_MODEL was '%s' (display name). Using '%s' (API ID). "
                "Update your .env to silence this warning.",
                m, fixed,
            )
            return fixed
        if " " in m:
            _log.warning(
                "ANTHROPIC_MODEL='%s' contains spaces, which is not a valid model ID. "
                "The API will likely reject this. Examples of valid IDs: "
                "claude-opus-4-7, claude-haiku-4-5, claude-sonnet-4-6.",
                m,
            )
    return m


@dataclass
class Config:
    gitlab_url: str
    gitlab_token: str
    gitlab_group: str | None
    llm_provider: str           # "anthropic" | "openai" | "gemini"
    llm_api_key: str
    llm_model: str
    dry_run: bool
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def readme_filenames(self) -> list[str]:
        return list(self.raw.get("readme_filenames", []))

    @property
    def analysis(self) -> dict[str, Any]:
        return self.raw.get("analysis", {})

    @property
    def skip(self) -> dict[str, Any]:
        return self.raw.get("skip", {})

    @property
    def commit(self) -> dict[str, Any]:
        return self.raw.get("commit", {})

    @property
    def runtime(self) -> dict[str, Any]:
        return self.raw.get("runtime", {})

    @property
    def logging_cfg(self) -> dict[str, Any]:
        return self.raw.get("logging", {})


def _resolve_llm() -> tuple[str, str, str]:
    """Pick a provider + key + model from env.

    Preferred form (simple):
        LLM_PROVIDER=anthropic|openai|gemini
        LLM_API_KEY=...
        LLM_MODEL=...          # optional; falls back to provider default

    Legacy fallback (still supported for backwards compat):
        ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY (+ *_MODEL)
        First one found wins, unless LLM_PROVIDER forces a specific one.

    Env var lookups are case-insensitive and tolerate common aliases
    (e.g. OPEN_AI_API_KEY is accepted as OPENAI_API_KEY).
    """
    explicit = _env("LLM_PROVIDER").lower() or None
    if explicit and explicit not in SUPPORTED_PROVIDERS:
        raise RuntimeError(
            f"LLM_PROVIDER='{explicit}' is not supported. "
            f"Use one of: {', '.join(SUPPORTED_PROVIDERS)}."
        )

    # Preferred form: LLM_API_KEY + LLM_PROVIDER (+ optional LLM_MODEL)
    generic_key = _env("LLM_API_KEY")
    generic_model = _env("LLM_MODEL")
    if generic_key:
        if not explicit:
            raise RuntimeError(
                "LLM_API_KEY is set but LLM_PROVIDER is missing. "
                f"Add LLM_PROVIDER=<one of {', '.join(SUPPORTED_PROVIDERS)}> to your .env."
            )
        model = _normalize_model(explicit, generic_model) if generic_model else DEFAULT_MODELS[explicit]
        return explicit, generic_key, model

    # Legacy fallback: per-provider env vars.
    legacy_keys = {
        "anthropic": _env("ANTHROPIC_API_KEY"),
        "openai":    _env("OPENAI_API_KEY"),
        "gemini":    _env("GEMINI_API_KEY"),
    }

    def legacy_model(p: str) -> str:
        raw = _env(f"{p.upper()}_MODEL")
        return _normalize_model(p, raw) if raw else DEFAULT_MODELS[p]

    if explicit:
        key = legacy_keys[explicit]
        if not key:
            raise LLMNotConfigured(_no_key_message(forced=explicit))
        return explicit, key, legacy_model(explicit)

    for provider in SUPPORTED_PROVIDERS:
        if legacy_keys[provider]:
            return provider, legacy_keys[provider], legacy_model(provider)

    raise LLMNotConfigured(_no_key_message())


def _no_key_message(forced: str | None = None) -> str:
    header = (
        f"LLM_PROVIDER={forced} was requested, but no API key is set for it."
        if forced
        else "No LLM API key found in the environment."
    )
    return (
        f"{header}\n"
        "\n"
        "Pick ONE of the three providers and add these to your .env:\n"
        "    LLM_PROVIDER=anthropic     # or 'openai' or 'gemini'\n"
        "    LLM_API_KEY=<your key>\n"
        "    LLM_MODEL=                 # optional — leave blank for provider default\n"
        "\n"
        "Defaults if LLM_MODEL is blank:\n"
        f"    anthropic -> {DEFAULT_MODELS['anthropic']}\n"
        f"    openai    -> {DEFAULT_MODELS['openai']}\n"
        f"    gemini    -> {DEFAULT_MODELS['gemini']}"
    )


def load_config() -> Config:
    load_dotenv(ROOT / ".env")
    cfg_path = ROOT / "config.yaml"
    with cfg_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    token = os.environ.get("GITLAB_TOKEN", "").strip()
    if not token:
        raise RuntimeError("GITLAB_TOKEN is not set. Copy .env.example to .env and fill it in.")
    if token.startswith(("-", " ")) or " " in token:
        _log.warning("GITLAB_TOKEN has suspicious whitespace around it — stripped, but check your .env.")
        token = token.strip()

    provider, api_key, model = _resolve_llm()

    return Config(
        gitlab_url=os.environ.get("GITLAB_URL", "https://gitlab.com").strip().rstrip("/"),
        gitlab_token=token,
        gitlab_group=os.environ.get("GITLAB_GROUP", "").strip() or None,
        llm_provider=provider,
        llm_api_key=api_key,
        llm_model=model,
        dry_run=os.environ.get("DRY_RUN", "0").strip() in {"1", "true", "TRUE", "yes"},
        raw=raw,
    )
