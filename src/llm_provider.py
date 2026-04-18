from __future__ import annotations

from abc import ABC, abstractmethod


SUPPORTED_PROVIDERS = ("anthropic", "openai", "gemini")

# Maps provider name -> pip package so the interactive installer knows what to fetch.
PROVIDER_PIP_PACKAGES = {
    "anthropic": "anthropic",
    "openai":    "openai",
    "gemini":    "google-genai",
}


class MissingSDKError(RuntimeError):
    """The SDK for the selected provider isn't installed. Carries the pip package name."""

    def __init__(self, provider: str, pip_pkg: str):
        self.provider = provider
        self.pip_pkg = pip_pkg
        super().__init__(
            f"The '{provider}' provider was selected but its SDK is not installed.\n"
            f"Run:  pip install {pip_pkg}\n"
            f"…or set LLM_PROVIDER / a different *_API_KEY to use another provider."
        )


class LLMProviderError(Exception):
    """User-actionable failure from the LLM provider (billing, bad key, quota, permissions).

    These are *global* conditions, not per-project flakes — raising one should stop the
    whole run so the user can either top up, fix the key, or switch LLM_PROVIDER.
    """

    def __init__(self, provider: str, reason: str, detail: str = "", suggestion: str = ""):
        self.provider = provider
        self.reason = reason
        self.detail = detail
        self.suggestion = suggestion
        super().__init__(self._format())

    def _format(self) -> str:
        lines = [
            "",
            "───────────────────────────────────────────────────────────────",
            f"  The {self.provider.upper()} API rejected the request.",
            "───────────────────────────────────────────────────────────────",
            f"  Problem: {self.reason}",
        ]
        if self.detail:
            lines.append(f"  Detail:  {self.detail}")
        lines.append("")
        lines.append("  What you can do:")
        if self.suggestion:
            lines.append(f"    • {self.suggestion}")
        lines.extend([
            "    • Switch providers: edit .env and set LLM_PROVIDER to",
            "      'anthropic', 'openai', or 'gemini' with the matching LLM_API_KEY.",
            "    • Run with DRY_RUN=1 to generate locally without committing while",
            "      you sort out billing/access.",
            "───────────────────────────────────────────────────────────────",
        ])
        return "\n".join(lines)


_BILLING_MARKERS = (
    "credit balance",
    "insufficient_quota",
    "insufficient credit",
    "exceeded your current quota",
    "out of credits",
    "quota exceeded",
    "billing",
    "payment required",
)
_AUTH_MARKERS = (
    "invalid api key",
    "invalid_api_key",
    "incorrect api key",
    "no auth credentials",
    "api key not valid",
    "unauthorized",
)
_MODEL_NOT_FOUND_MARKERS = (
    "model not found",
    "model_not_found",
    "model: ",                 # anthropic error prefix
    "is not a valid model",
    "does not exist",
    "the model",               # openai "The model `foo` does not exist"
)


def _classify(exc: BaseException) -> tuple[str, str] | None:
    """If this exception is a known user-actionable condition, return (reason, suggestion)."""
    msg = str(exc).lower()
    tname = type(exc).__name__.lower()

    if any(m in msg for m in _BILLING_MARKERS):
        return (
            "Billing/quota exhausted on this API key.",
            "Top up credits in your provider dashboard, or switch LLM_PROVIDER to one you have credits for.",
        )
    if "authenticationerror" in tname or any(m in msg for m in _AUTH_MARKERS):
        return (
            "The API key was rejected as invalid or revoked.",
            "Verify LLM_API_KEY in your .env matches the selected LLM_PROVIDER.",
        )
    if "permissiondenied" in tname or "forbidden" in msg:
        return (
            "The API key doesn't have permission for this model/operation.",
            "Check your key's access to the chosen model, or switch LLM_PROVIDER.",
        )
    # Model-name typos (e.g. ANTHROPIC_MODEL='Claude Haiku 4.5' instead of 'claude-haiku-4-5')
    if any(m in msg for m in _MODEL_NOT_FOUND_MARKERS) and (
        "badrequest" in tname or "notfound" in tname or "not_found_error" in msg or "invalid" in msg
    ):
        return (
            "The model name in your .env is not a valid model ID for this provider.",
            "Check LLM_MODEL / *_MODEL in your .env. Valid examples: "
            "claude-opus-4-7, claude-haiku-4-5, gpt-4o-mini, gemini-2.5-flash.",
        )
    return None


class LLMProvider(ABC):
    """Thin adapter around one of {Anthropic, OpenAI, Gemini}.

    A provider turns (system, user, max_tokens) into plain text. Retries and
    empty-response handling live one layer up in ReadmeGenerator so every
    provider behaves the same way from the caller's perspective.
    """

    name: str

    @abstractmethod
    def generate(self, *, system: str, user: str, max_tokens: int) -> str: ...

    def _call(self, fn):
        """Invoke `fn()` and convert user-actionable failures into LLMProviderError."""
        try:
            return fn()
        except Exception as e:
            classified = _classify(e)
            if classified:
                reason, suggestion = classified
                raise LLMProviderError(self.name, reason, str(e), suggestion) from e
            raise


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str):
        try:
            from anthropic import Anthropic
        except ImportError as e:
            raise _missing_sdk_error("anthropic", "anthropic") from e
        self.client = Anthropic(api_key=api_key)
        self.model = model

    def generate(self, *, system: str, user: str, max_tokens: int) -> str:
        def _do():
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
            return "\n".join(parts).strip()
        return self._call(_do)


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, api_key: str, model: str):
        try:
            from openai import OpenAI
        except ImportError as e:
            raise _missing_sdk_error("openai", "openai") from e
        self.client = OpenAI(api_key=api_key)
        self.model = model

    def generate(self, *, system: str, user: str, max_tokens: int) -> str:
        def _do():
            resp = self.client.chat.completions.create(
                model=self.model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            return (resp.choices[0].message.content or "").strip()
        return self._call(_do)


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str):
        try:
            from google import genai
            from google.genai import types
        except ImportError as e:
            raise _missing_sdk_error("gemini", "google-genai") from e
        self._types = types
        self.client = genai.Client(api_key=api_key)
        self.model = model

    def generate(self, *, system: str, user: str, max_tokens: int) -> str:
        def _do():
            resp = self.client.models.generate_content(
                model=self.model,
                contents=user,
                config=self._types.GenerateContentConfig(
                    system_instruction=system,
                    max_output_tokens=max_tokens,
                ),
            )
            return (resp.text or "").strip()
        return self._call(_do)


def build_llm_provider(provider: str, api_key: str, model: str) -> LLMProvider:
    provider = provider.lower()
    if provider == "anthropic":
        return AnthropicProvider(api_key, model)
    if provider == "openai":
        return OpenAIProvider(api_key, model)
    if provider == "gemini":
        return GeminiProvider(api_key, model)
    raise ValueError(
        f"Unknown LLM provider '{provider}'. Supported: {', '.join(SUPPORTED_PROVIDERS)}."
    )


def _missing_sdk_error(provider: str, pip_pkg: str) -> MissingSDKError:
    return MissingSDKError(provider, pip_pkg)
