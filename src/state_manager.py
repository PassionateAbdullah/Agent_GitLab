from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id    INTEGER PRIMARY KEY,
    full_path     TEXT NOT NULL,
    status        TEXT NOT NULL,        -- 'skipped_has_readme' | 'readme_created' | 'skipped_empty' | 'skipped_archived' | 'error'
    last_checked  TEXT NOT NULL,        -- ISO-8601 UTC
    last_commit   TEXT,                 -- default_branch HEAD sha at time of check
    note          TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    scanned       INTEGER DEFAULT 0,
    created       INTEGER DEFAULT 0,
    skipped       INTEGER DEFAULT 0,
    errors        INTEGER DEFAULT 0,
    summary       TEXT
);
"""


class StateManager:
    """SQLite-backed memory of everything the agent has seen and done."""

    def __init__(self, db_path: Path, logger):
        self.path = db_path
        self.log = logger
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------- project records ----------

    def get_record(self, project_id: int) -> dict | None:
        cur = self.conn.execute(
            "SELECT project_id, full_path, status, last_checked, last_commit, note FROM projects WHERE project_id = ?",
            (project_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        keys = ("project_id", "full_path", "status", "last_checked", "last_commit", "note")
        return dict(zip(keys, row))

    def upsert(
        self,
        project_id: int,
        full_path: str,
        status: str,
        last_commit: str | None = None,
        note: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.conn.execute(
            """
            INSERT INTO projects(project_id, full_path, status, last_checked, last_commit, note)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(project_id) DO UPDATE SET
                full_path=excluded.full_path,
                status=excluded.status,
                last_checked=excluded.last_checked,
                last_commit=excluded.last_commit,
                note=excluded.note
            """,
            (project_id, full_path, status, now, last_commit, note),
        )
        self.conn.commit()

    # ---------- run records ----------

    def start_run(self) -> int:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cur = self.conn.execute("INSERT INTO runs(started_at) VALUES(?)", (now,))
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, scanned: int, created: int, skipped: int, errors: int, summary: str) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.conn.execute(
            "UPDATE runs SET finished_at=?, scanned=?, created=?, skipped=?, errors=?, summary=? WHERE id=?",
            (now, scanned, created, skipped, errors, summary, run_id),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
