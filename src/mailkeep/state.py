from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any, Iterator

from .models import EnrichmentResult, ParsedMessage, RemoteMessage, StoredObject


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


class State:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            package = resources.files("mailkeep.migrations")
            migrations = sorted(
                item for item in package.iterdir() if item.name.endswith(".sql")
            )
            applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
            for item in migrations:
                version = int(item.name.split("_", 1)[0])
                if version in applied:
                    continue
                conn.executescript(item.read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, utcnow()),
                )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()

    @contextmanager
    def attempt(self, stage: str, config_hash: str, *, archive_id: str | None = None,
                remote_id: int | None = None) -> Iterator[None]:
        started = utcnow()
        try:
            yield
        except Exception as exc:
            self._record_attempt(archive_id, remote_id, stage, started, "failed",
                                 f"{type(exc).__name__}: {exc}", config_hash)
            raise
        else:
            self._record_attempt(archive_id, remote_id, stage, started, "complete", None,
                                 config_hash)

    def _record_attempt(self, archive_id: str | None, remote_id: int | None,
                        stage: str, started: str, outcome: str,
                        error: str | None, config_hash: str) -> None:
        with self.connect() as conn:
            number = conn.execute(
                "SELECT COALESCE(MAX(attempt_number),0)+1 FROM attempts "
                "WHERE stage=? AND archive_id IS ? AND remote_id IS ?",
                (stage, archive_id, remote_id),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO attempts(archive_id,remote_id,stage,attempt_number,started_at,finished_at,outcome,error,config_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (archive_id, remote_id, stage, number, started, utcnow(), outcome, error,
                 config_hash),
            )

    def ensure_account(self, account_id: str, provider: str, username: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO accounts VALUES (?, ?, ?, ?) "
                "ON CONFLICT(account_id) DO UPDATE SET provider=excluded.provider, username=excluded.username",
                (account_id, provider, username, utcnow()),
            )

    def discover(self, remote: RemoteMessage) -> int:
        now = utcnow()
        with self.connect() as conn:
            if remote.gmail_msgid:
                stable = conn.execute(
                    "SELECT id FROM remote_messages WHERE account_id=? AND gmail_msgid=?",
                    (remote.account_id, remote.gmail_msgid),
                ).fetchone()
                if stable:
                    conn.execute(
                        "UPDATE remote_messages SET mailbox=?,uidvalidity=?,uid=?,last_seen_at=? WHERE id=?",
                        (remote.mailbox, remote.uidvalidity, remote.uid, now, stable[0]),
                    )
                    return int(stable[0])
            conn.execute(
                "INSERT INTO remote_messages(account_id,mailbox,uidvalidity,uid,gmail_msgid,last_seen_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(account_id,mailbox,uidvalidity,uid) "
                "DO UPDATE SET gmail_msgid=COALESCE(excluded.gmail_msgid,gmail_msgid),last_seen_at=excluded.last_seen_at",
                (remote.account_id, remote.mailbox, remote.uidvalidity, remote.uid,
                 remote.gmail_msgid, now),
            )
            row = conn.execute(
                "SELECT id FROM remote_messages WHERE account_id=? AND mailbox=? AND uidvalidity=? AND uid=?",
                (remote.account_id, remote.mailbox, remote.uidvalidity, remote.uid),
            ).fetchone()
            return int(row[0])

    def record_preserved(
        self, remote_id: int, stored: StoredObject, parsed: ParsedMessage,
        object_relpath: str, config_hash: str,
    ) -> None:
        now = utcnow()
        with self.transaction() as conn:
            collision = conn.execute(
                "SELECT archive_id FROM archives WHERE message_id=? AND raw_sha256<>?",
                (parsed.message_id, stored.raw_sha256),
            ).fetchone() if parsed.message_id else None
            conn.execute(
                "INSERT INTO archives(archive_id,raw_sha256,object_relpath,message_id,subject,message_date,date_source,"
                "preservation_state,created_at,updated_at,config_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(archive_id) DO UPDATE SET updated_at=excluded.updated_at",
                (stored.archive_id, stored.raw_sha256, object_relpath, parsed.message_id,
                 parsed.subject, parsed.message_date.isoformat() if parsed.message_date else None,
                 parsed.date_source, "preserved", now, now, config_hash),
            )
            conn.execute(
                "UPDATE remote_messages SET archive_id=? WHERE id=?",
                (stored.archive_id, remote_id),
            )
            conn.execute("DELETE FROM attachments WHERE archive_id=?", (stored.archive_id,))
            conn.executemany(
                "INSERT INTO attachments(archive_id,ordinal,original_name,stored_name,media_type,disposition,content_id,size,sha256,decode_error) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(stored.archive_id, a.ordinal, a.original_name, a.stored_name, a.media_type,
                  a.disposition, a.content_id, len(a.data), a.sha256, a.decode_error)
                 for a in parsed.attachments],
            )
            self._event(conn, stored.archive_id, remote_id, "preservation", None, "preserved")
            if collision:
                conn.execute(
                    "INSERT INTO diagnostics(archive_id,code,detail,created_at) VALUES (?,?,?,?)",
                    (stored.archive_id, "message_id_collision",
                     f"Message-ID also belongs to {collision['archive_id']}", now),
                )
            for detail in parsed.diagnostics:
                conn.execute(
                    "INSERT INTO diagnostics(archive_id,code,detail,created_at) VALUES (?,?,?,?)",
                    (stored.archive_id, "mime_diagnostic", detail, now),
                )

    def set_archive_state(self, archive_id: str, dimension: str, state: str) -> None:
        columns = {
            "preservation": "preservation_state",
            "enrichment": "enrichment_state",
            "markdown": "markdown_state",
        }
        column = columns[dimension]
        with self.transaction() as conn:
            old = conn.execute(
                f"SELECT {column} FROM archives WHERE archive_id=?", (archive_id,)
            ).fetchone()
            if old is None:
                raise KeyError(archive_id)
            conn.execute(
                f"UPDATE archives SET {column}=?,updated_at=? WHERE archive_id=?",
                (state, utcnow(), archive_id),
            )
            self._event(conn, archive_id, None, dimension, old[0], state)

    def set_note(self, archive_id: str, relpath: str, managed_hash: str) -> None:
        with self.transaction() as conn:
            old = conn.execute(
                "SELECT markdown_state FROM archives WHERE archive_id=?", (archive_id,)
            ).fetchone()[0]
            conn.execute(
                "UPDATE archives SET note_relpath=?,note_managed_sha256=?,markdown_state='complete',updated_at=? WHERE archive_id=?",
                (relpath, managed_hash, utcnow(), archive_id),
            )
            self._event(conn, archive_id, None, "markdown", old, "complete")

    def update_note_paths(self, moves: list[tuple[str, str, str]]) -> None:
        with self.transaction() as conn:
            for archive_id, old, new in moves:
                changed = conn.execute(
                    "UPDATE archives SET note_relpath=?,updated_at=? "
                    "WHERE archive_id=? AND note_relpath=?",
                    (new, utcnow(), archive_id, old),
                )
                if changed.rowcount != 1:
                    raise RuntimeError(f"note path changed during organization: {archive_id}")
                self._event(conn, archive_id, None, "note_path", old, new)

    def save_enrichment(self, archive_id: str, result: EnrichmentResult) -> None:
        payload = json.dumps({
            "categories": result.categories,
            "document_type": result.document_type,
            "document_type_confidence": result.document_type_confidence,
            "entities": result.entities,
            "raw": result.raw,
        }, sort_keys=True)
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO enrichment_results(archive_id,provider,model,input_sha256,config_hash,request_id,duration_ms,result_json,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (archive_id, result.provider, result.model, result.input_hash,
                 result.config_hash, result.request_id, result.duration_ms, payload, utcnow()),
            )
            old = conn.execute("SELECT enrichment_state FROM archives WHERE archive_id=?", (archive_id,)).fetchone()[0]
            conn.execute("UPDATE archives SET enrichment_state='complete',updated_at=? WHERE archive_id=?", (utcnow(), archive_id))
            self._event(conn, archive_id, None, "enrichment", old, "complete")

    def cached_enrichment(self, archive_id: str, provider: str, model: str, input_hash: str, config_hash: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM enrichment_results WHERE archive_id=? AND provider=? AND input_sha256=? AND config_hash=? "
                "ORDER BY id DESC LIMIT 1",
                (archive_id, provider, input_hash, config_hash),
            ).fetchone()

    def load_cached_enrichment(self, archive_id: str, provider: str, model: str,
                               input_hash: str, config_hash: str) -> EnrichmentResult | None:
        row = self.cached_enrichment(archive_id, provider, model, input_hash, config_hash)
        if row is None:
            return None
        payload = json.loads(row["result_json"])
        return EnrichmentResult(
            provider=row["provider"], model=row["model"],
            categories=payload.get("categories", {}),
            document_type=payload.get("document_type"),
            document_type_confidence=payload.get("document_type_confidence"),
            entities=tuple(payload.get("entities", [])), request_id=row["request_id"],
            input_hash=row["input_sha256"], config_hash=row["config_hash"],
            duration_ms=row["duration_ms"], raw=payload.get("raw", {}),
        )

    def complete_verified(self, archive_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT preservation_state,enrichment_state,markdown_state FROM archives WHERE archive_id=?",
                (archive_id,),
            ).fetchone()
            return bool(row and row[0] == "preservation_verified" and row[1] in ("complete", "skipped") and row[2] == "complete")

    def mark_processed(self, remote_id: int) -> None:
        with self.transaction() as conn:
            row = conn.execute("SELECT archive_id,provider_state FROM remote_messages WHERE id=?", (remote_id,)).fetchone()
            conn.execute("UPDATE remote_messages SET provider_state='processed',processed_at=? WHERE id=?", (utcnow(), remote_id))
            self._event(conn, row[0], remote_id, "provider", row[1], "processed")

    def mark_trashed(self, remote_id: int) -> None:
        with self.transaction() as conn:
            row = conn.execute("SELECT archive_id,provider_state FROM remote_messages WHERE id=?", (remote_id,)).fetchone()
            conn.execute("UPDATE remote_messages SET provider_state='trashed',trashed_at=? WHERE id=?", (utcnow(), remote_id))
            self._event(conn, row[0], remote_id, "provider", row[1], "trashed")

    def mark_absent(self, remote_id: int) -> None:
        with self.transaction() as conn:
            row = conn.execute("SELECT archive_id,provider_state FROM remote_messages WHERE id=?", (remote_id,)).fetchone()
            conn.execute("UPDATE remote_messages SET provider_state='absent' WHERE id=?", (remote_id,))
            self._event(conn, row[0], remote_id, "provider", row[1], "absent")

    def prune_candidates(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT r.*,a.* FROM remote_messages r JOIN archives a ON a.archive_id=r.archive_id "
                "WHERE r.provider_state='processed' ORDER BY r.processed_at"
            ).fetchall()

    def trashed_messages(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT r.*,a.raw_sha256 FROM remote_messages r JOIN archives a ON a.archive_id=r.archive_id "
                "WHERE r.provider_state='trashed' AND r.gmail_msgid IS NOT NULL"
            ).fetchall()

    def status(self) -> dict[str, Any]:
        with self.connect() as conn:
            dimensions: dict[str, dict[str, int]] = {}
            for name, table, column in (
                ("preservation", "archives", "preservation_state"),
                ("enrichment", "archives", "enrichment_state"),
                ("markdown", "archives", "markdown_state"),
                ("provider", "remote_messages", "provider_state"),
            ):
                dimensions[name] = {r[0]: r[1] for r in conn.execute(
                    f"SELECT {column},COUNT(*) FROM {table} GROUP BY {column}"
                )}
            failures = conn.execute("SELECT COUNT(*) FROM attempts WHERE outcome='failed'").fetchone()[0]
            recent = [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 10")]
            return {"states": dimensions, "failed_attempts": failures, "recent_events": recent}

    @staticmethod
    def _event(conn: sqlite3.Connection, archive_id: str | None, remote_id: int | None,
               dimension: str, old: str | None, new: str, detail: dict[str, Any] | None = None) -> None:
        conn.execute(
            "INSERT INTO events(archive_id,remote_id,dimension,from_state,to_state,detail_json,created_at) VALUES (?,?,?,?,?,?,?)",
            (archive_id, remote_id, dimension, old, new,
             json.dumps(detail or {}, sort_keys=True), utcnow()),
        )
