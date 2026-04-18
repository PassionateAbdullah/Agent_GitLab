# GitLab README Agent

A Python agent that scans every GitLab project your token can see, detects the ones missing a README, generates one with Claude, and commits it back to the default branch. Runs twice a day so new projects are picked up automatically.

## How it works

1. Authenticates to GitLab with a Personal Access Token.
2. Iterates every accessible project (or a single group + subgroups, if you scope it).
3. For each project, checks the root of the default branch for a README (`.md`, `.rst`, `.txt`, …).
4. If one exists, skips the project and records that in a local SQLite memory.
5. If not, samples up to ~12 important files (manifests, entry points, Dockerfile, CI, etc.), sends a compact context to Claude, and gets back a grounded README.md.
6. Commits the generated `README.md` to the default branch — or opens a Merge Request, depending on config.

The SQLite memory at `state/processed.db` means each run focuses on genuinely-new work — every project's last status, last commit checked, and every run's tally is kept there.

## Setup

```bash
cd gitlab-readme-agent
bash scripts/install.sh
cp .env.example .env   # already done by the installer on first run
$EDITOR .env            # fill in GITLAB_TOKEN and ANTHROPIC_API_KEY
```

The GitLab token needs these scopes: `api`, `read_repository`, `write_repository`.

## Test it (dry run)

A dry run analyzes and generates but does NOT push anything:

```bash
DRY_RUN=1 .venv/bin/python run_agent.py
```

Check `logs/agent.log` for per-project decisions.

## Schedule it

Installs cron jobs at 07:00 and 19:00:

```bash
bash scripts/cron_setup.sh
```

## Configuration

- **`.env`** — secrets and per-run flags (`DRY_RUN`, optional `GITLAB_GROUP` to scope a single group).
- **`config.yaml`** — behavior: which filenames count as a README, which files to sample, skip rules (archived / forks / empty repos), commit strategy (`default` branch vs `mr`), retries, logging.

### Merge-request mode

Set `commit.branch_strategy: mr` in `config.yaml` and the agent will push to a feature branch (`readme-agent/add-readme`) and open an MR instead of committing straight to the default branch. Useful for protected branches.

## Files

- [src/agent.py](src/agent.py) — orchestrator, per-project pipeline
- [src/gitlab_client.py](src/gitlab_client.py) — GitLab API operations
- [src/project_analyzer.py](src/project_analyzer.py) — file sampling + metadata
- [src/readme_generator.py](src/readme_generator.py) — Claude prompt + call
- [src/state_manager.py](src/state_manager.py) — SQLite memory
- [prompts/readme_system.md](prompts/readme_system.md) — system prompt for the generator

## Memory / observability

Inspect what the agent has done:

```bash
sqlite3 state/processed.db 'SELECT status, COUNT(*) FROM projects GROUP BY status;'
sqlite3 state/processed.db 'SELECT * FROM runs ORDER BY id DESC LIMIT 5;'
tail -f logs/agent.log
```

## Safety notes

- Dry-run first on a test group before pointing it at your whole GitLab.
- The commit strategy defaults to pushing straight to the default branch. Switch to `mr` if that's too aggressive for your team.
- The agent never overwrites an existing README — it only creates one when none is present.
# Agent_GitLab
