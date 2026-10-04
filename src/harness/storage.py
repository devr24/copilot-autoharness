import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .errors import HarnessError


@dataclass(frozen=True)
class Event:
    session_id: str
    event_name: str
    occurred_at: str
    cwd: str
    tool_name: str | None
    outcome: str | None
    details: dict[str, Any]


class Storage:
    def __init__(self, database: Path):
        self.database = database
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        if os.name != "nt":
            try:
                self.database.parent.chmod(0o700)
                self.database.chmod(0o600)
            except OSError:
                pass

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    event_name TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    tool_name TEXT,
                    outcome TEXT,
                    details_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_session_idx
                    ON events(session_id, id);
                CREATE INDEX IF NOT EXISTS events_cwd_idx
                    ON events(cwd, id);
                CREATE TABLE IF NOT EXISTS proposals (
                    id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    risk_level TEXT NOT NULL DEFAULT 'low',
                    source_sessions_json TEXT NOT NULL DEFAULT '[]',
                    skill_md TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    cwd TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reflected_messages (
                    cwd TEXT NOT NULL,
                    hash TEXT NOT NULL,
                    PRIMARY KEY (cwd, hash)
                );
                CREATE TABLE IF NOT EXISTS skill_usage (
                    session_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    invoked_at TEXT NOT NULL,
                    plugin TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (session_id, name, invoked_at)
                );
                CREATE TABLE IF NOT EXISTS skill_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cwd TEXT NOT NULL,
                    name TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    rating TEXT NOT NULL CHECK (rating IN ('helpful', 'not-helpful')),
                    recorded_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS skill_feedback_lookup_idx
                    ON skill_feedback(scope, name, recorded_at);
                CREATE TABLE IF NOT EXISTS reflection_marks (
                    session_id TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    last_event_id INTEGER NOT NULL,
                    last_event_at TEXT NOT NULL,
                    reflected_at TEXT NOT NULL,
                    PRIMARY KEY (session_id, cwd)
                );
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(proposals)").fetchall()
            }
            if "risk_level" not in columns:
                connection.execute(
                    "ALTER TABLE proposals ADD COLUMN risk_level TEXT NOT NULL DEFAULT 'low'"
                )
            if "absorbs_json" not in columns:
                connection.execute(
                    "ALTER TABLE proposals ADD COLUMN absorbs_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "source_sessions_json" not in columns:
                connection.execute(
                    "ALTER TABLE proposals ADD COLUMN source_sessions_json TEXT NOT NULL DEFAULT '[]'"
                )

    def add_event(self, event: Event) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """INSERT INTO events
                       (session_id, event_name, occurred_at, cwd, tool_name, outcome, details_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event.session_id,
                        event.event_name,
                        event.occurred_at,
                        event.cwd,
                        event.tool_name,
                        event.outcome,
                        json.dumps(event.details, ensure_ascii=False),
                    ),
                )
        except sqlite3.Error as exc:
            raise HarnessError(f"Could not record Copilot event: {exc}") from exc

    def seen_message_hashes(self, cwd: str) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute("SELECT hash FROM reflected_messages WHERE cwd = ?", (cwd,)).fetchall()
        return {row["hash"] for row in rows}

    def add_message_hashes(self, cwd: str, hashes: set[str]) -> None:
        with self._connect() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO reflected_messages (cwd, hash) VALUES (?, ?)",
                [(cwd, value) for value in hashes],
            )

    def record_skill_uses(self, session_id: str, uses: list[dict[str, str]]) -> int:
        added = 0
        with self._connect() as connection:
            for use in uses:
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO skill_usage (session_id, name, scope, invoked_at, plugin)
                       VALUES (?, ?, ?, ?, ?)""",
                    (session_id, use["name"], use["scope"], use["at"], use.get("plugin", "")),
                )
                added += cursor.rowcount
        return added

    def skill_usage(self) -> dict[tuple[str, str], tuple[int, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT name, scope, COUNT(*) AS uses, MAX(invoked_at) AS last "
                "FROM skill_usage GROUP BY name, scope"
            ).fetchall()
        return {(row["scope"], row["name"]): (row["uses"], row["last"]) for row in rows}

    def record_skill_feedback(
        self, cwd: str, name: str, scope: str, version: int, rating: str, recorded_at: str
    ) -> None:
        if rating not in {"helpful", "not-helpful"}:
            raise HarnessError(f"Unsupported skill feedback rating: {rating}")
        try:
            with self._connect() as connection:
                connection.execute(
                    """INSERT INTO skill_feedback
                       (cwd, name, scope, version, rating, recorded_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (cwd, name, scope, version, rating, recorded_at),
                )
        except sqlite3.Error as exc:
            raise HarnessError(f"Could not record skill feedback: {exc}") from exc

    def skill_feedback(self, cwd: str) -> dict[tuple[str, str, str], tuple[int, int]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT cwd, name, scope,
                          SUM(CASE WHEN rating = 'helpful' THEN 1 ELSE 0 END) AS helpful,
                          SUM(CASE WHEN rating = 'not-helpful' THEN 1 ELSE 0 END) AS not_helpful
                   FROM skill_feedback WHERE cwd = ? GROUP BY cwd, name, scope""",
                (cwd,),
            ).fetchall()
        return {
            (row["cwd"], row["scope"], row["name"]): (row["helpful"], row["not_helpful"])
            for row in rows
        }

    def latest_session(self, cwd: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT session_id FROM events WHERE cwd = ? ORDER BY id DESC LIMIT 1",
                (cwd,),
            ).fetchone()
        return row["session_id"] if row else None

    def latest_transcript_path(self, session_id: str, cwd: str) -> str | None:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT details_json FROM events
                   WHERE session_id = ? AND cwd = ? AND details_json LIKE '%ranscript%'
                   ORDER BY id DESC LIMIT 20""",
                (session_id, cwd),
            ).fetchall()
        for row in rows:
            details = json.loads(row["details_json"])
            value = details.get("transcriptPath") or details.get("transcript_path")
            if isinstance(value, str) and value.strip():
                return value
        return None

    def reflection_mark(self, session_id: str, cwd: str) -> tuple[int, str | None]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT last_event_id, last_event_at FROM reflection_marks "
                "WHERE session_id = ? AND cwd = ?",
                (session_id, cwd),
            ).fetchone()
        return (row["last_event_id"], row["last_event_at"]) if row else (0, None)

    def set_reflection_mark(
        self, session_id: str, cwd: str, last_event_id: int, last_event_at: str, reflected_at: str
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO reflection_marks
                   (session_id, cwd, last_event_id, last_event_at, reflected_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(session_id, cwd) DO UPDATE SET
                     last_event_id = excluded.last_event_id,
                     last_event_at = excluded.last_event_at,
                     reflected_at = excluded.reflected_at""",
                (session_id, cwd, last_event_id, last_event_at, reflected_at),
            )

    def session_events(
        self, session_id: str, cwd: str, after_id: int = 0
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id, event_name, occurred_at, tool_name, outcome, details_json
                   FROM events WHERE session_id = ? AND cwd = ? AND id > ? ORDER BY id""",
                (session_id, cwd, after_id),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "event": row["event_name"],
                "timestamp": row["occurred_at"],
                "tool": row["tool_name"],
                "outcome": row["outcome"],
                "details": json.loads(row["details_json"]),
            }
            for row in rows
        ]

    def add_proposal(self, proposal: dict[str, Any], cwd: str, created_at: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO proposals
                   (id, action, scope, name, description, reason, confidence, risk_level,
                    source_sessions_json, absorbs_json, skill_md,
                    evidence_json, status, created_at, cwd)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    proposal["id"],
                    proposal["action"],
                    proposal["scope"],
                    proposal["name"],
                    proposal["description"],
                    proposal["reason"],
                    proposal["confidence"],
                    proposal["risk_level"],
                    json.dumps(proposal.get("source_session_ids", [])),
                    json.dumps(proposal.get("absorbs", [])),
                    proposal["skill_md"],
                    json.dumps(proposal["evidence"]),
                    created_at,
                    cwd,
                ),
            )

    def proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
        return self._proposal_dict(row) if row else None

    def proposals(self, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM proposals"
        parameters: tuple[Any, ...] = ()
        if status:
            query += " WHERE status = ?"
            parameters = (status,)
        query += " ORDER BY created_at DESC"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._proposal_dict(row) for row in rows]

    def set_proposal_status(self, proposal_id: str, status: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE proposals SET status = ? WHERE id = ? AND status = 'pending'",
                (status, proposal_id),
            )
        if cursor.rowcount != 1:
            raise HarnessError(f"Pending proposal not found: {proposal_id}")

    @staticmethod
    def _proposal_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["evidence"] = json.loads(data.pop("evidence_json"))
        data["source_session_ids"] = json.loads(data.pop("source_sessions_json"))
        data["absorbs"] = json.loads(data.pop("absorbs_json", "[]"))
        return data

    def get_meta(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def tool_events_after(self, cwd: str, after_id: int) -> tuple[int, int]:
        """Return (tool event count, newest event id) for a directory after an event id."""
        with self._connect() as connection:
            row = connection.execute(
                """SELECT COUNT(CASE WHEN event_name IN ('postToolUse', 'postToolUseFailure')
                                     THEN 1 END) AS tools, COALESCE(MAX(id), ?) AS newest
                   FROM events WHERE cwd = ? AND id > ?""",
                (after_id, cwd, after_id),
            ).fetchone()
        return row["tools"], row["newest"]
