from __future__ import annotations

from dataclasses import dataclass, field

from gitlab.v4.objects import Project

from .gitlab_client import GitLabClient, RepoFile


@dataclass
class ProjectContext:
    """Everything the README generator needs to know about a project."""
    name: str
    full_path: str
    description: str | None
    default_branch: str
    topics: list[str]
    languages: dict[str, float]          # { "Python": 87.4, "HTML": 12.6 }
    web_url: str
    top_level_tree: list[str] = field(default_factory=list)
    files: list[RepoFile] = field(default_factory=list)


class ProjectAnalyzer:
    """Pulls a compact, informative snapshot of a project for LLM analysis."""

    def __init__(self, client: GitLabClient, analysis_cfg: dict, logger):
        self.client = client
        self.cfg = analysis_cfg
        self.log = logger
        self.priority_files: list[str] = list(self.cfg.get("priority_files", []))
        self.max_files: int = int(self.cfg.get("max_files", 12))
        self.max_bytes: int = int(self.cfg.get("max_bytes_per_file", 8000))
        self.budget: int = int(self.cfg.get("total_context_budget", 60000))

    def analyze(self, project: Project) -> ProjectContext:
        default_branch = project.default_branch or "main"

        # Languages: GitLab returns {"Python": 87.43, ...}
        try:
            languages = project.languages() or {}
        except Exception:
            languages = {}

        # Top-level tree names (helps the LLM see project layout quickly).
        tree = self.client.list_tree(project, ref=default_branch, path="", recursive=False)
        top_level = [e["name"] for e in tree if e.get("type") in {"blob", "tree"}]

        # Pick the files to fetch: priority list first (if present in tree), then other useful files.
        chosen = self._select_files(tree)

        files: list[RepoFile] = []
        used_bytes = 0
        for path in chosen:
            if len(files) >= self.max_files or used_bytes >= self.budget:
                break
            rf = self.client.get_file(project, path, default_branch, self.max_bytes)
            if rf is None or not rf.content:
                continue
            remaining = self.budget - used_bytes
            if remaining <= 0:
                break
            if len(rf.content) > remaining:
                rf = RepoFile(path=rf.path, size=rf.size, content=rf.content[:remaining])
            files.append(rf)
            used_bytes += len(rf.content)

        return ProjectContext(
            name=project.name,
            full_path=project.path_with_namespace,
            description=project.description,
            default_branch=default_branch,
            topics=list(getattr(project, "topics", []) or []),
            languages=languages,
            web_url=project.web_url,
            top_level_tree=top_level,
            files=files,
        )

    # ---------- helpers ----------

    def _select_files(self, tree: list[dict]) -> list[str]:
        names_in_tree = {e["name"]: e["path"] for e in tree if e.get("type") == "blob"}
        picks: list[str] = []

        # 1. priority files that actually exist at root
        for candidate in self.priority_files:
            # candidate may be a nested path like "src/main.rs"; match by exact path via a later listing
            if candidate in names_in_tree.values() or candidate in names_in_tree:
                picks.append(names_in_tree.get(candidate, candidate))

        # 2. any source-ish file at root as a fallback (e.g., a single-file project)
        if not picks:
            interesting_ext = {".py", ".js", ".ts", ".go", ".rs", ".java", ".rb", ".php", ".cs", ".c", ".cpp", ".sh", ".html"}
            for name, path in names_in_tree.items():
                if any(name.endswith(ext) for ext in interesting_ext):
                    picks.append(path)
                if len(picks) >= self.max_files:
                    break

        # dedupe preserving order
        seen: set[str] = set()
        out: list[str] = []
        for p in picks:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return out[: self.max_files]
