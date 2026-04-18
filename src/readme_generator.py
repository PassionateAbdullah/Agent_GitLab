from __future__ import annotations

from pathlib import Path

from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential

from .llm_provider import LLMProvider, LLMProviderError
from .project_analyzer import ProjectContext

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "readme_system.md"


class ReadmeGenerator:
    def __init__(self, provider: LLMProvider, max_tokens: int, logger):
        self.provider = provider
        self.max_tokens = max_tokens
        self.log = logger
        self.system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    @retry(
        reraise=True,
        retry=retry_if_not_exception_type(LLMProviderError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
    )
    def generate(self, ctx: ProjectContext) -> str:
        user_message = self._render_user_message(ctx)
        readme = self.provider.generate(
            system=self.system_prompt,
            user=user_message,
            max_tokens=self.max_tokens,
        )
        if not readme:
            raise RuntimeError(f"{self.provider.name} returned an empty README.")
        if not readme.endswith("\n"):
            readme += "\n"
        return readme

    # ---------- prompt construction ----------

    def _render_user_message(self, ctx: ProjectContext) -> str:
        lines: list[str] = []
        lines.append("## Project metadata")
        lines.append(f"- Name: {ctx.name}")
        lines.append(f"- Path: {ctx.full_path}")
        lines.append(f"- Web URL: {ctx.web_url}")
        lines.append(f"- Default branch: {ctx.default_branch}")
        if ctx.description:
            lines.append(f"- Description: {ctx.description}")
        if ctx.topics:
            lines.append(f"- Topics: {', '.join(ctx.topics)}")
        if ctx.languages:
            langs = ", ".join(f"{k} ({v:.1f}%)" for k, v in ctx.languages.items())
            lines.append(f"- Languages: {langs}")

        lines.append("")
        lines.append("## Top-level files & folders")
        if ctx.top_level_tree:
            for name in ctx.top_level_tree:
                lines.append(f"- {name}")
        else:
            lines.append("(none)")

        lines.append("")
        lines.append("## File samples")
        if not ctx.files:
            lines.append("(no text files could be read)")
        for rf in ctx.files:
            lines.append(f"\n### `{rf.path}` ({rf.size} bytes)")
            fence = self._fence_for(rf.path)
            lines.append(f"```{fence}")
            lines.append(rf.content.rstrip())
            lines.append("```")

        lines.append("")
        lines.append("Now write the README.md.")
        return "\n".join(lines)

    @staticmethod
    def _fence_for(path: str) -> str:
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        return {
            "py": "python", "js": "javascript", "ts": "typescript",
            "go": "go", "rs": "rust", "java": "java", "rb": "ruby",
            "php": "php", "cs": "csharp", "c": "c", "cpp": "cpp",
            "sh": "bash", "yml": "yaml", "yaml": "yaml",
            "json": "json", "toml": "toml", "xml": "xml",
            "html": "html", "md": "markdown",
        }.get(ext, "")
