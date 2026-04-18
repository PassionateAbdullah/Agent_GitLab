from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Iterator

import gitlab
from gitlab.exceptions import (
    GitlabAuthenticationError,
    GitlabError,
    GitlabGetError,
    GitlabHttpError,
)
from gitlab.v4.objects import Project
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


@dataclass
class RepoFile:
    path: str
    size: int
    content: str  # decoded UTF-8 (may be truncated); empty for binary/too-large


REQUIRED_SCOPES = [
    ("api", "full read/write API access — needed to create READMEs, branches, and merge requests"),
    ("read_repository", "needed to list files and read repository contents"),
    ("write_repository", "needed to commit the generated README back to the repo"),
]


class MissingScopeError(Exception):
    """Raised when the GitLab token is missing a required permission scope."""

    def __init__(self, action: str, detail: str = ""):
        self.action = action
        self.detail = detail
        super().__init__(self._format())

    def _format(self) -> str:
        scopes_block = "\n".join(
            f"    • {name:<17} — {desc}" for name, desc in REQUIRED_SCOPES
        )
        detail_line = f"\n  GitLab said: {self.detail}\n" if self.detail else "\n"
        return (
            "\n"
            "───────────────────────────────────────────────────────────────\n"
            "  Your GitLab access token is missing a required permission.\n"
            "───────────────────────────────────────────────────────────────\n"
            f"  The agent tried to {self.action}, but GitLab blocked it because\n"
            f"  your personal access token does not have the right scopes.\n"
            f"{detail_line}"
            "  How to fix it:\n"
            "    1. Go to GitLab → User Settings → Access Tokens.\n"
            "    2. Create a new token (or edit the existing one) and tick\n"
            "       the Scopes checkboxes listed below.\n"
            "    3. Copy the new token, update GITLAB_TOKEN in your env /\n"
            "       config.yaml, and rerun the agent.\n"
            "\n"
            "  Required scopes for this agent:\n"
            f"{scopes_block}\n"
            "───────────────────────────────────────────────────────────────"
        )


def _looks_like_scope_error(exc: BaseException) -> bool:
    """Heuristic: does this GitLab error look like a missing-scope/permission problem?"""
    if isinstance(exc, GitlabAuthenticationError):
        return True
    code = getattr(exc, "response_code", None)
    if code in (401, 403):
        return True
    msg = str(exc).lower()
    return "insufficient_scope" in msg or "forbidden" in msg or "unauthorized" in msg


class GitLabClient:
    """Thin wrapper around python-gitlab for the operations this agent needs."""

    def __init__(self, url: str, token: str, logger):
        self.url = url
        self.token = token
        self.log = logger
        self.gl = gitlab.Gitlab(url=url, private_token=token, per_page=100)
        try:
            self.gl.auth()
        except GitlabError as e:
            if _looks_like_scope_error(e):
                raise MissingScopeError(
                    action="authenticate and read your user profile",
                    detail=str(e),
                ) from e
            raise
        self.log.info("Authenticated to GitLab as %s (id=%s)", self.gl.user.username, self.gl.user.id)

    # ---------- project iteration ----------

    def iter_projects(self, group: str | None = None) -> Iterator[Project]:
        """Yield every project accessible to the token, or within a specific group (recursively)."""
        try:
            if group:
                grp = self.gl.groups.get(group)
                self.log.info("Scanning group '%s' (id=%s) recursively", grp.full_path, grp.id)
                yield from grp.projects.list(
                    include_subgroups=True,
                    iterator=True,
                    archived=False,
                    all_available=True,
                )
            else:
                self.log.info("Scanning every project the token can see (membership=true)")
                yield from self.gl.projects.list(
                    iterator=True,
                    membership=True,
                    archived=False,
                )
        except GitlabError as e:
            if _looks_like_scope_error(e):
                raise MissingScopeError(
                    action="list the projects your token can see",
                    detail=str(e),
                ) from e
            raise

    def get_project(self, project_id: int) -> Project:
        """Group listings return lightweight `GroupProject` objects — hydrate to a full Project."""
        return self.gl.projects.get(project_id)

    # ---------- repo inspection ----------

    @retry(
        reraise=True,
        retry=retry_if_exception_type((GitlabHttpError,)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
    )
    def list_tree(self, project: Project, ref: str, path: str = "", recursive: bool = False) -> list[dict]:
        try:
            return project.repository_tree(path=path, ref=ref, recursive=recursive, all=True)
        except GitlabGetError as e:
            if e.response_code == 404:
                return []
            raise

    def has_readme(self, project: Project, ref: str, candidate_names: list[str]) -> str | None:
        """Return the existing README's path if one is present in the repo root, else None."""
        tree = self.list_tree(project, ref, path="", recursive=False)
        lowered = {c.lower() for c in candidate_names}
        for entry in tree:
            if entry.get("type") == "blob" and entry.get("name", "").lower() in lowered:
                return entry["path"]
        return None

    @retry(
        reraise=True,
        retry=retry_if_exception_type((GitlabHttpError,)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
    )
    def get_file(self, project: Project, path: str, ref: str, max_bytes: int) -> RepoFile | None:
        try:
            f = project.files.get(file_path=path, ref=ref)
        except GitlabGetError:
            return None
        try:
            raw = base64.b64decode(f.content)
        except Exception:
            return None
        size = len(raw)
        truncated = raw[:max_bytes]
        try:
            text = truncated.decode("utf-8")
        except UnicodeDecodeError:
            return RepoFile(path=path, size=size, content="")  # binary — skip content
        return RepoFile(path=path, size=size, content=text)

    # ---------- committing ----------

    def create_readme(
        self,
        project: Project,
        branch: str,
        content: str,
        commit_message: str,
        author_name: str,
        author_email: str,
        filename: str = "README.md",
    ) -> None:
        project.files.create(
            {
                "file_path": filename,
                "branch": branch,
                "content": content,
                "commit_message": commit_message,
                "author_name": author_name,
                "author_email": author_email,
            }
        )

    def create_readme_via_mr(
        self,
        project: Project,
        default_branch: str,
        content: str,
        commit_message: str,
        author_name: str,
        author_email: str,
        filename: str = "README.md",
        feature_branch: str = "readme-agent/add-readme",
    ) -> str:
        """Create a feature branch, add the README, and open an MR. Returns the MR web URL."""
        try:
            project.branches.create({"branch": feature_branch, "ref": default_branch})
        except GitlabHttpError as e:
            if e.response_code != 400:  # 400 = already exists
                raise
        project.files.create(
            {
                "file_path": filename,
                "branch": feature_branch,
                "content": content,
                "commit_message": commit_message,
                "author_name": author_name,
                "author_email": author_email,
            }
        )
        mr = project.mergerequests.create(
            {
                "source_branch": feature_branch,
                "target_branch": default_branch,
                "title": commit_message,
                "description": "Auto-generated README by readme-agent.",
                "remove_source_branch": True,
            }
        )
        return mr.web_url
