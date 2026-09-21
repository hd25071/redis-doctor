"""SQLite persistence for diagnoses, traces, approvals and alert fingerprints.

Schema changes go through the numbered migrations below, applied on startup.
The plan called for Alembic; a 40-line migration runner was chosen instead
because this schema has no ORM and no autogenerate story — the trade-off is
recorded in ``docs/design-decisions.md`` (decision 6).

SQLite is a single-writer database: the agent runs as exactly one replica with
the ``Recreate`` strategy, which is also why the trajectory store can be SQLite
at all.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS diagnoses (
            id TEXT PRIMARY KEY,
            created_at REAL NOT NULL,
            finished_at REAL,
            status TEXT NOT NULL,
            variant TEXT NOT NULL,
            alert TEXT NOT NULL,
            alertname TEXT,
            fingerprint TEXT,
            root_cause TEXT,
            confidence REAL,
            report_json TEXT,
            state_json TEXT,
            audit_json TEXT,
            meta_json TEXT
        );
        CREATE TABLE IF NOT EXISTS tool_calls (
            call_id TEXT PRIMARY KEY,
            diagnosis_id TEXT NOT NULL,
            at REAL NOT NULL,
            tool TEXT NOT NULL,
            args_json TEXT,
            signals_json TEXT,
            blocked INTEGER DEFAULT 0,
            blocked_reason TEXT,
            truncated INTEGER DEFAULT 0,
            duration_ms INTEGER,
            output TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_tool_calls_diagnosis ON tool_calls(diagnosis_id);
        CREATE TABLE IF NOT EXISTS approvals (
            id TEXT PRIMARY KEY,
            diagnosis_id TEXT NOT NULL,
            requested_at REAL NOT NULL,
            decided_at REAL,
            status TEXT NOT NULL,
            action_json TEXT NOT NULL,
            decided_by TEXT,
            note TEXT
        );
        CREATE TABLE IF NOT EXISTS alert_fingerprints (
            fingerprint TEXT PRIMARY KEY,
            alertname TEXT,
            first_seen REAL NOT NULL,
            last_seen REAL NOT NULL,
            seen_count INTEGER NOT NULL DEFAULT 1,
            last_diagnosis_id TEXT
        );
        """,
    ),
]


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        with self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
            )
            applied = {
                row["version"]
                for row in self._conn.execute("SELECT version FROM schema_migrations")
            }
            for version, script in MIGRATIONS:
                if version in applied:
                    continue
                self._conn.executescript(script)
                self._conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, time.time()),
                )

    # -- diagnoses -------------------------------------------------------
    def save_diagnosis(self, record: dict[str, Any], trace: list[dict[str, Any]]) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO diagnoses
                (id, created_at, finished_at, status, variant, alert, alertname, fingerprint,
                 root_cause, confidence, report_json, state_json, audit_json, meta_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record["id"],
                    record["created_at"],
                    record.get("finished_at"),
                    record.get("status", "ok"),
                    record.get("variant", "D"),
                    record.get("alert", ""),
                    record.get("alertname"),
                    record.get("fingerprint"),
                    record.get("root_cause"),
                    record.get("confidence"),
                    json.dumps(record.get("report"), ensure_ascii=False),
                    json.dumps(record.get("state"), ensure_ascii=False),
                    json.dumps(record.get("audit"), ensure_ascii=False),
                    json.dumps(record.get("meta"), ensure_ascii=False),
                ),
            )
            for entry in trace:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO tool_calls
                    (call_id, diagnosis_id, at, tool, args_json, signals_json, blocked,
                     blocked_reason, truncated, duration_ms, output)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        entry["call_id"],
                        record["id"],
                        entry.get("at", time.time()),
                        entry.get("tool", ""),
                        json.dumps(entry.get("args"), ensure_ascii=False),
                        json.dumps(entry.get("signals"), ensure_ascii=False),
                        1 if entry.get("blocked") else 0,
                        entry.get("block_reason"),
                        1 if entry.get("truncated") else 0,
                        entry.get("duration_ms"),
                        entry.get("output", ""),
                    ),
                )

    def get_diagnosis(self, diagnosis_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM diagnoses WHERE id = ?", (diagnosis_id,)).fetchone()
        if row is None:
            return None
        record = dict(row)
        for key in ("report_json", "state_json", "audit_json", "meta_json"):
            value = record.pop(key, None)
            record[key.replace("_json", "")] = json.loads(value) if value else None
        record["trace"] = self.get_trace(diagnosis_id)
        record["approvals"] = self.list_approvals(diagnosis_id)
        return record

    def list_diagnoses(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT id, created_at, status, variant, alertname, root_cause, confidence, alert
            FROM diagnoses ORDER BY created_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_trace(self, diagnosis_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM tool_calls WHERE diagnosis_id = ? ORDER BY at", (diagnosis_id,)
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["args"] = json.loads(item.pop("args_json") or "null")
            item["signals"] = json.loads(item.pop("signals_json") or "[]")
            item["blocked"] = bool(item["blocked"])
            item["truncated"] = bool(item["truncated"])
            out.append(item)
        return out

    def count_diagnoses(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) AS c FROM diagnoses").fetchone()["c"])

    # -- approvals -------------------------------------------------------
    def save_approval(
        self,
        approval_id: str,
        diagnosis_id: str,
        action: dict[str, Any],
        status: str = "pending",
        decided_by: str | None = None,
        note: str | None = None,
    ) -> None:
        existing = self._conn.execute(
            "SELECT requested_at FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        requested_at = float(existing["requested_at"]) if existing else time.time()
        with self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO approvals
                (id, diagnosis_id, requested_at, decided_at, status, action_json, decided_by, note)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    approval_id,
                    diagnosis_id,
                    requested_at,
                    time.time() if status != "pending" else None,
                    status,
                    json.dumps(action, ensure_ascii=False),
                    decided_by,
                    note,
                ),
            )

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["action"] = json.loads(item.pop("action_json"))
        return item

    def claim_approval(self, approval_id: str) -> bool:
        """Atomically move pending -> executing.

        Returns False when the row was not pending, which is what stops two
        concurrent approvals from executing the same action twice.
        """
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE approvals SET status = 'executing' WHERE id = ? AND status = 'pending'",
                (approval_id,),
            )
        return cursor.rowcount == 1

    def finish_approval(
        self,
        approval_id: str,
        status: str,
        decided_by: str,
        note: str = "",
        result: dict[str, Any] | None = None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE approvals SET status = ?, decided_at = ?, decided_by = ?, note = ? "
                "WHERE id = ?",
                (
                    status,
                    time.time(),
                    decided_by,
                    json.dumps(result or {}, ensure_ascii=False) if result else note,
                    approval_id,
                ),
            )

    def list_approvals(self, diagnosis_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM approvals WHERE diagnosis_id = ? ORDER BY requested_at",
            (diagnosis_id,),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["action"] = json.loads(item.pop("action_json"))
            out.append(item)
        return out

    def list_pending_approvals(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM approvals WHERE status = 'pending' ORDER BY requested_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["action"] = json.loads(item.pop("action_json"))
            out.append(item)
        return out

    # -- alert dedup -----------------------------------------------------
    def note_fingerprint(
        self, fingerprint: str, alertname: str, cooldown_seconds: int
    ) -> tuple[bool, int]:
        """Record an alert and decide whether it should be diagnosed.

        Returns ``(should_diagnose, seen_count)``. Within the cooldown window a
        repeat of the same fingerprint is counted but not re-diagnosed: an
        alert storm must not turn into an LLM spend storm (or an approval
        storm for the on-call engineer).
        """
        now = time.time()
        row = self._conn.execute(
            "SELECT * FROM alert_fingerprints WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        if row is None:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO alert_fingerprints
                    (fingerprint, alertname, first_seen, last_seen, seen_count)
                    VALUES (?,?,?,?,1)
                    """,
                    (fingerprint, alertname, now, now),
                )
            return True, 1
        counted = int(row["seen_count"]) + 1
        cooled = (now - float(row["last_seen"])) >= cooldown_seconds
        with self._conn:
            self._conn.execute(
                """
                UPDATE alert_fingerprints
                SET last_seen = ?, seen_count = ?, last_diagnosis_id = ?
                WHERE fingerprint = ?
                """,
                (now, counted, row["last_diagnosis_id"], fingerprint),
            )
        return cooled, counted

    def link_fingerprint(self, fingerprint: str, diagnosis_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE alert_fingerprints SET last_diagnosis_id = ? WHERE fingerprint = ?",
                (diagnosis_id, fingerprint),
            )

    def check_fingerprint(self, fingerprint: str, cooldown_seconds: int) -> tuple[bool, int]:
        """Read-only cooldown check (recording happens after a successful run)."""
        now = time.time()
        row = self._conn.execute(
            "SELECT last_seen, seen_count FROM alert_fingerprints WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        if row is None:
            return True, 0
        seen = int(row["seen_count"])
        return (now - float(row["last_seen"])) >= cooldown_seconds, seen

    def record_fingerprint(
        self, fingerprint: str, alertname: str, diagnosis_id: str | None = None
    ) -> None:
        """Record an alert as handled. Called only after the diagnosis succeeded,
        so a failure does not start the cooldown window."""
        now = time.time()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO alert_fingerprints
                (fingerprint, alertname, first_seen, last_seen, seen_count, last_diagnosis_id)
                VALUES (?,?,?,?,1,?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    seen_count = alert_fingerprints.seen_count + 1,
                    last_diagnosis_id = COALESCE(excluded.last_diagnosis_id,
                                                 alert_fingerprints.last_diagnosis_id)
                """,
                (fingerprint, alertname, now, now, diagnosis_id),
            )

    def stats(self) -> dict[str, Any]:
        diagnoses = self.count_diagnoses()
        approvals = self._conn.execute(
            "SELECT status, COUNT(*) AS c FROM approvals GROUP BY status"
        ).fetchall()
        alerts = self._conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(SUM(seen_count), 0) AS s FROM alert_fingerprints"
        ).fetchone()
        return {
            "diagnoses": diagnoses,
            "approvals": {row["status"]: row["c"] for row in approvals},
            "alert_fingerprints": alerts["c"],
            "alert_events": alerts["s"],
        }

    def close(self) -> None:
        self._conn.close()
