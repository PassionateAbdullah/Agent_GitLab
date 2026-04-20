from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from gitlab.exceptions import GitlabCreateError, GitlabHttpError
from gitlab.v4.objects import Project

from .config import DEFAULT_MODELS, Config, LLMNotConfigured, load_config
from .gitlab_client import GitLabClient, MissingScopeError
from .llm_provider import (
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

ROOT = Path(__file__).resolve().parent.parent


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


# Substrings that signal "you can't push directly to this branch" in the GitLab
# error body. GitLab CE/EE sometimes returns 500 (Gitaly hook error) instead of
# 403 when the default branch is protected — both should trigger MR fallback.
_BRANCH_BLOCKED_MARKERS = (
    "protected branch",
    "you are not allowed to push",
    "commit_message is missing",       # rare — malformed commit
    "pre-receive hook",
    "cannot be modified",
    "is protected",
)


def _is_branch_write_blocked(exc: BaseException) -> bool:
    code = getattr(exc, "response_code", None)
    if code in (403, 500):
        return True
    msg = str(exc).lower()
    return any(m in msg for m in _BRANCH_BLOCKED_MARKERS)


def _explain_write_failure(project: Project, default_branch: str | None, exc: BaseException) -> str:
    """Build a human-readable explanation of a per-project write failure.

    Replaces a noisy stack trace in the log with something the user can act on:
    what GitLab said, the most likely cause, and the concrete next step.
    """
    code = getattr(exc, "response_code", None)
    full_path = project.path_with_namespace
    web_url = getattr(project, "web_url", "")
    lines = [
        f"Could not write README to {full_path}",
        f"  Project: {web_url}" if web_url else "",
        f"  GitLab said: HTTP {code} — {exc}".rstrip(),
        "",
    ]
    if code == 404:
        lines += [
            "  Most likely cause:",
            f"    • The default branch '{default_branch}' doesn't actually exist in this",
            "      repo (the project is empty, or the branch was deleted/renamed).",
            "",
            "  What to do:",
            "    • If the repo really is empty, push at least one commit so a branch exists.",
            "    • If the branch was renamed, fix the project's default branch under",
            "      Settings → Repository → Default branch.",
            "    • If you only want to skip empty repos, set `skip.empty_repo: true` in",
            "      config.yaml — but it's already true by default, so this means GitLab",
            "      reports a default_branch that doesn't really exist.",
        ]
    elif code in (403, 500):
        lines += [
            "  Most likely cause:",
            "    • Your token doesn't have write permission on this repo, OR",
            "    • The default branch is protected and only Maintainers can push to it.",
            "      (GitLab CE returns 500 instead of 403 when a Gitaly hook rejects this.)",
            "",
            "  What to do:",
            "    • Ask the project owner to grant your account Maintainer (or Developer",
            "      with 'allowed to push') on the default branch.",
            "    • Or lower the protection at Settings → Repository → Protected branches.",
            "    • If the project owner doesn't want this repo touched, add it to a skip",
            "      list — easiest is to archive it (the agent already skips archived projects).",
        ]
    else:
        lines += [
            "  Most likely cause:",
            f"    • Unexpected GitLab response (HTTP {code}). The repo may be misconfigured.",
            "",
            "  What to do:",
            "    • Open the project in GitLab and confirm the default branch exists and",
            "      that your token has write access.",
        ]
    return "\n".join(line for line in lines if line is not None)


def _persist_llm_to_env(provider: str, api_key: str, model: str) -> Path:
    """Write LLM_PROVIDER / LLM_API_KEY / LLM_MODEL to .env (preserving other lines).

    - Updates in place if the keys already exist.
    - Appends them under a clear header if not.
    - Creates .env if it doesn't exist yet.
    - Also updates os.environ so a subsequent load_config() in the same process
      picks up the new values.
    """
    env_path = ROOT / ".env"
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    updates = {
        "LLM_PROVIDER": provider,
        "LLM_API_KEY": api_key,
        "LLM_MODEL": model,
    }

    seen: set[str] = set()
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            lines[i] = f"{key}={updates[key]}"
            seen.add(key)

    missing = [k for k in updates if k not in seen]
    if missing:
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.append("# ─── LLM (persisted by the agent) ───")
        for k in missing:
            lines.append(f"{k}={updates[k]}")

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    os.environ["LLM_PROVIDER"] = provider
    os.environ["LLM_API_KEY"] = api_key
    os.environ["LLM_MODEL"] = model

    return env_path


def _interactive_setup_provider(excluded: str | None = None) -> LLMProvider | None:
    """Interactive provider selection → key prompt → build → persist to .env.

    Returns the built LLMProvider, or None if the user cancelled.
    `excluded` omits a provider from the menu (e.g. the one that just failed).
    Used by both first-run setup and switch-on-failure recovery.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None

    alternatives = [p for p in SUPPORTED_PROVIDERS if p != excluded]

    try:
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
                        print(f"\n{e2}\n")
                        continue
                else:
                    continue
            except RuntimeError as e:
                print(f"\n{e}\n")
                continue

            env_path = _persist_llm_to_env(picked, api_key, model)
            try:
                rel = env_path.relative_to(ROOT)
            except ValueError:
                rel = env_path
            print(
                f"\n✓ Saved to {rel} — future runs will use this key automatically."
                f"\n✓ Using {picked.title()} ({model}).\n"
            )
            return provider

    except (KeyboardInterrupt, EOFError):
        print()
        return None


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
        """Show the friendly error, ask y/N, then delegate to the shared setup flow.

        Returns the new LLMProvider if the user picked one, or None if they
        declined / we're non-interactive.
        """
        print(str(error))

        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return None

        try:
            answer = input("\nSwitch to a different LLM provider and continue? [y/N]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            return None
        if answer not in ("y", "yes"):
            return None

        return _interactive_setup_provider(excluded=error.provider)

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

        commit_kwargs = dict(
            content=readme,
            commit_message=self.cfg.commit.get("message", "docs: add auto-generated README"),
            author_name=self.cfg.commit.get("author_name", "README Agent"),
            author_email=self.cfg.commit.get("author_email", "readme-agent@localhost"),
        )
        strategy = self.cfg.commit.get("branch_strategy", "default")

        try:
            if strategy == "mr":
                url = self.gl.create_readme_via_mr(
                    project=project, default_branch=default_branch, **commit_kwargs,
                )
                note = f"MR: {url}"
            else:
                note = self._commit_with_fallbacks(project, default_branch, commit_kwargs)
            self.state.upsert(pid, full_path, "readme_created", note=note)
            self.log.info("[%s] README committed (%s)", pid, note)
            return "created"
        except (GitlabCreateError, GitlabHttpError) as e:
            explanation = _explain_write_failure(project, default_branch, e)
            self.state.upsert(pid, full_path, "error", note=f"commit: HTTP {getattr(e, 'response_code', '?')}")
            self.log.warning("[%s] %s", pid, explanation)
            return "error"
        except Exception as e:
            self.state.upsert(pid, full_path, "error", note=f"commit: {e}")
            self.log.exception("[%s] commit failed: %s", pid, e)
            return "error"

    def _commit_with_fallbacks(
        self, project: Project, default_branch: str, commit_kwargs: dict,
    ) -> str:
        """Try direct → MR → empty-repo initial commit, in that order. Returns the state note."""
        pid = project.id
        try:
            self.gl.create_readme(project=project, branch=default_branch, **commit_kwargs)
            return f"committed to {default_branch}"
        except (GitlabCreateError, GitlabHttpError) as e:
            if not _is_branch_write_blocked(e):
                raise
            self.log.warning(
                "[%s] direct commit to '%s' blocked (%s) — trying merge request.",
                pid, default_branch, e,
            )

        # MR fallback. If this 404s, the source branch likely doesn't exist:
        # check whether the repo is actually empty and self-fix if so.
        try:
            url = self.gl.create_readme_via_mr(
                project=project, default_branch=default_branch, **commit_kwargs,
            )
            return f"MR (fallback, branch protected): {url}"
        except (GitlabCreateError, GitlabHttpError) as e:
            code = getattr(e, "response_code", None)
            if code != 404 or not self.gl.is_empty_repo(project):
                raise
            self.log.warning(
                "[%s] '%s' doesn't exist as a real branch — repo is empty. "
                "Creating the very first commit with the README.",
                pid, default_branch,
            )

        # Empty-repo path: create the initial commit (which also creates the branch).
        self.gl.create_initial_commit_with_readme(
            project=project, branch=default_branch, **commit_kwargs,
        )
        return f"initial commit on empty repo → {default_branch}"


def main() -> int:
    try:
        cfg = load_config()
    except LLMNotConfigured as e:
        # First run (or .env missing LLM creds) — offer interactive setup.
        if sys.stdin.isatty() and sys.stdout.isatty():
            print("\n─── First-time setup ───────────────────────────────────────")
            print("No LLM provider configured. Pick one and paste your API key;")
            print("the agent will save it to .env so it's used automatically from")
            print("now on. You'll only be prompted again if the key stops working.\n")
            if _interactive_setup_provider() is None:
                print("\nSetup cancelled — nothing saved.")
                return 4
            cfg = load_config()  # reload with the freshly-persisted values
        else:
            print(str(e))
            return 4

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
