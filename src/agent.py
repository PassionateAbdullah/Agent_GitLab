from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from gitlab.v4.objects import Project

from .config import DEFAULT_MODELS, Config, load_config
from .gitlab_client import GitLabClient, MissingScopeError
from .llm_provider import (
    PROVIDER_PIP_PACKAGES,
    SUPPORTED_PROVIDERS,
    LLMProvider,
    LLMProviderError,
    MissingSDKError,
    build_llm_provider,
)
from .logger import setup_logging
from .project_analyzer import ProjectAnalyzer
from .readme_generator import ReadmeGenerator
from .state_manager import StateManager


def _masked_input(prompt: str) -> str:
    """Read a line with '*' echo per character. Paste works. Falls back to plain
    input() if we're not on a TTY or termios isn't available (e.g. Windows).

    The key never lands in stdout scrollback, but the user still gets visual
    feedback (asterisks accumulate as characters/paste arrive).
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return input(prompt)
    try:
        import termios
        import tty
    except ImportError:
        return input(prompt)

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    chars: list[str] = []
    try:
        tty.setcbreak(fd)  # disables canonical + echo, leaves Ctrl+C working
        # Drop anything buffered *before* the prompt so stale keystrokes /
        # earlier paste attempts / focus-tracking escapes don't echo as '*'.
        termios.tcflush(fd, termios.TCIFLUSH)
        # Disable bracketed-paste mode while we read: otherwise pasted text
        # arrives wrapped in ESC[200~…ESC[201~ markers that echo as '*'s.
        sys.stdout.write("\x1b[?2004l")
        sys.stdout.write(prompt)
        sys.stdout.flush()
        while True:
            ch = sys.stdin.read(1)
            if ch in ("\r", "\n"):
                sys.stdout.write("\n")
                sys.stdout.flush()
                break
            if ch in ("\x7f", "\b"):  # backspace / delete
                if chars:
                    chars.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            if ch == "\x03":  # Ctrl+C
                raise KeyboardInterrupt
            if ch == "\x04" and not chars:  # Ctrl+D on empty line
                raise EOFError
            if not ch or ord(ch) < 0x20:
                # Silently drop every control byte (ESC, arrow keys, mouse
                # tracking, bracketed-paste stragglers) — do NOT echo them.
                continue
            chars.append(ch)
            sys.stdout.write("*")
            sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return "".join(chars)


def _try_pip_install(pkg: str) -> bool:
    """Run `pip install <pkg>` in the current interpreter. Streams output live."""
    print(f"\nInstalling {pkg} into {sys.executable} …\n")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])
        print(f"\n✓ {pkg} installed.\n")
        return True
    except subprocess.CalledProcessError as e:
        print(f"\n✗ pip install failed (exit {e.returncode}). You can install it manually and rerun.\n")
        return False
    except FileNotFoundError:
        print("\n✗ Could not find pip for this interpreter.\n")
        return False

ROOT = Path(__file__).resolve().parent.parent


class ReadmeAgent:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log = setup_logging(
            cfg.logging_cfg.get("level", "INFO"),
            cfg.logging_cfg.get("file", "logs/agent.log"),
        )
        self.gl = GitLabClient(cfg.gitlab_url, cfg.gitlab_token, self.log)
        self.analyzer = ProjectAnalyzer(self.gl, cfg.analysis, self.log)
        provider = build_llm_provider(cfg.llm_provider, cfg.llm_api_key, cfg.llm_model)
        self.log.info("Using LLM provider '%s' (model=%s)", provider.name, cfg.llm_model)
        self.generator = ReadmeGenerator(
            provider,
            int(cfg.runtime.get("model_max_tokens", 2000)),
            self.log,
        )
        self.state = StateManager(ROOT / "state" / "processed.db", self.log)

    # ---------- main loop ----------

    def run(self) -> None:
        run_id = self.state.start_run()
        scanned = created = skipped = errors = 0
        self.log.info("=== README agent run started (dry_run=%s) ===", self.cfg.dry_run)
        try:
            for light_project in self.gl.iter_projects(self.cfg.gitlab_group):
                scanned += 1
                try:
                    project = self.gl.get_project(light_project.id)
                except Exception as e:
                    errors += 1
                    self.log.warning("Could not hydrate project id=%s: %s", light_project.id, e)
                    continue

                while True:
                    try:
                        outcome = self._process_project(project)
                        break
                    except LLMProviderError as e:
                        new_provider = self._prompt_switch_provider(e)
                        if new_provider is None:
                            raise
                        self.generator.provider = new_provider
                        self.log.info(
                            "Switched LLM provider to '%s' (model=%s) — retrying project %s",
                            new_provider.name, new_provider.model, project.path_with_namespace,
                        )

                if outcome == "created":
                    created += 1
                elif outcome == "error":
                    errors += 1
                else:
                    skipped += 1

                delay = float(self.cfg.runtime.get("per_project_delay", 1.0))
                if delay > 0:
                    time.sleep(delay)
        finally:
            summary = f"scanned={scanned} created={created} skipped={skipped} errors={errors}"
            self.state.finish_run(run_id, scanned, created, skipped, errors, summary)
            self.state.close()
            self.log.info("=== README agent run finished: %s ===", summary)

    # ---------- interactive provider switch ----------

    def _prompt_switch_provider(self, error: LLMProviderError) -> LLMProvider | None:
        """Show the friendly error and, if running interactively, offer to switch providers.

        Returns the new LLMProvider if the user picked one, or None to bail out
        (non-interactive shell, user declined, or cancelled with Ctrl+C / EOF).
        """
        print(str(error))

        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            # Non-interactive (cron, CI) — don't hang waiting for input.
            return None

        alternatives = [p for p in SUPPORTED_PROVIDERS if p != error.provider]

        try:
            answer = input("\nSwitch to a different LLM provider and continue? [y/N]: ").strip().lower()
            if answer not in ("y", "yes"):
                return None

            while True:
                print("\nWhich provider?")
                for i, name in enumerate(alternatives, start=1):
                    print(f"  [{i}] {name.title():<9} (default model: {DEFAULT_MODELS[name]})")

                picked: str | None = None
                while picked is None:
                    choice = input(f"Pick [1-{len(alternatives)}] (or 'q' to cancel): ").strip().lower()
                    if choice in ("q", "quit", ""):
                        return None
                    if choice.isdigit() and 1 <= int(choice) <= len(alternatives):
                        picked = alternatives[int(choice) - 1]
                    else:
                        print("  Not a valid choice — try again.")

                api_key = _masked_input(
                    f"Paste your {picked.title()} API key, then press Enter: "
                ).strip()
                if not api_key:
                    print("  No key entered — cancelling.")
                    return None

                default_model = DEFAULT_MODELS[picked]
                model = input(f"Model (blank for '{default_model}'): ").strip() or default_model

                try:
                    provider = build_llm_provider(picked, api_key, model)
                except MissingSDKError as e:
                    print(f"\n{e.pip_pkg} is not installed.")
                    answer = input("Install it now via pip? [y/N]: ").strip().lower()
                    if answer in ("y", "yes") and _try_pip_install(e.pip_pkg):
                        try:
                            provider = build_llm_provider(picked, api_key, model)
                        except MissingSDKError as e2:
                            # Install reported success but import still fails — weird;
                            # treat as a hard miss and let the user pick differently.
                            print(f"\n{e2}\n")
                            continue
                    else:
                        # User declined or install failed — offer to pick another.
                        continue
                except RuntimeError as e:
                    print(f"\n{e}\n")
                    continue

                print(f"\n✓ Switched to {picked.title()} ({model}). Resuming…\n")
                return provider

        except (KeyboardInterrupt, EOFError):
            print()  # clean line break after ^C / ^D
            return None

    # ---------- per-project ----------

    def _process_project(self, project: Project) -> str:
        pid = project.id
        full_path = project.path_with_namespace
        self.log.info("[%s] Checking %s", pid, full_path)

        if self.cfg.skip.get("archived", True) and getattr(project, "archived", False):
            self.state.upsert(pid, full_path, "skipped_archived")
            self.log.info("[%s] archived — skipped", pid)
            return "skipped"

        if self.cfg.skip.get("forks", False) and getattr(project, "forked_from_project", None):
            self.state.upsert(pid, full_path, "skipped_fork")
            self.log.info("[%s] fork — skipped", pid)
            return "skipped"

        default_branch = project.default_branch
        if not default_branch:
            if self.cfg.skip.get("empty_repo", True):
                self.state.upsert(pid, full_path, "skipped_empty")
                self.log.info("[%s] empty repo — skipped", pid)
                return "skipped"

        try:
            existing = self.gl.has_readme(project, default_branch, self.cfg.readme_filenames)
        except Exception as e:
            self.state.upsert(pid, full_path, "error", note=f"tree-read: {e}")
            self.log.exception("[%s] failed to list tree: %s", pid, e)
            return "error"

        if existing:
            self.state.upsert(pid, full_path, "skipped_has_readme", note=existing)
            self.log.info("[%s] already has README (%s) — skipped", pid, existing)
            return "skipped"

        self.log.info("[%s] no README found — generating", pid)
        try:
            ctx = self.analyzer.analyze(project)
        except Exception as e:
            self.state.upsert(pid, full_path, "error", note=f"analyze: {e}")
            self.log.exception("[%s] analyze failed: %s", pid, e)
            return "error"

        try:
            readme = self.generator.generate(ctx)
        except LLMProviderError:
            # Global condition (billing/quota/bad key) — stop the whole run.
            raise
        except Exception as e:
            self.state.upsert(pid, full_path, "error", note=f"generate: {e}")
            self.log.exception("[%s] generate failed: %s", pid, e)
            return "error"

        if self.cfg.dry_run:
            self.log.info("[%s] DRY_RUN — would commit %d chars of README", pid, len(readme))
            self.state.upsert(pid, full_path, "dry_run", note=f"{len(readme)} chars")
            return "skipped"

        try:
            strategy = self.cfg.commit.get("branch_strategy", "default")
            if strategy == "mr":
                url = self.gl.create_readme_via_mr(
                    project=project,
                    default_branch=default_branch,
                    content=readme,
                    commit_message=self.cfg.commit.get("message", "docs: add auto-generated README"),
                    author_name=self.cfg.commit.get("author_name", "README Agent"),
                    author_email=self.cfg.commit.get("author_email", "readme-agent@localhost"),
                )
                note = f"MR: {url}"
            else:
                self.gl.create_readme(
                    project=project,
                    branch=default_branch,
                    content=readme,
                    commit_message=self.cfg.commit.get("message", "docs: add auto-generated README"),
                    author_name=self.cfg.commit.get("author_name", "README Agent"),
                    author_email=self.cfg.commit.get("author_email", "readme-agent@localhost"),
                )
                note = f"committed to {default_branch}"
            self.state.upsert(pid, full_path, "readme_created", note=note)
            self.log.info("[%s] README committed (%s)", pid, note)
            return "created"
        except Exception as e:
            self.state.upsert(pid, full_path, "error", note=f"commit: {e}")
            self.log.exception("[%s] commit failed: %s", pid, e)
            return "error"


def main() -> int:
    cfg = load_config()
    try:
        agent = ReadmeAgent(cfg)
        agent.run()
    except MissingScopeError as e:
        print(str(e))
        return 2
    except LLMProviderError as e:
        # Interactive flow already printed the block when it prompted; only
        # print here if we skipped the prompt (non-interactive shell).
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print(str(e))
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
