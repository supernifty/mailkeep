from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Config
from .enrichment import enrich, input_hash, no_enrichment, redact
from .locking import data_lock
from .mime import parse_message
from .models import EnrichmentResult, RemoteMessage
from .notes import build_note, note_filename, write_note
from .provider import Provider
from .state import State
from .storage import identity, object_relative_path, reconcile_staging, store
from .verification import verify_archive, verify_all


def _setup(config: Config) -> State:
    state = State(config.paths.database)
    state.initialize()
    state.ensure_account(config.account.account_id, config.account.provider,
                         config.account.username)
    return state


def scan(config: Config, provider: Provider, limit: int | None = None) -> list[dict[str, Any]]:
    known: dict[tuple[str, int, int], dict[str, Any]] = {}
    if config.paths.database.exists():
        state = State(config.paths.database)
        with state.connect() as conn:
            for row in conn.execute("SELECT * FROM remote_messages WHERE account_id=?", (config.account.account_id,)):
                known[(row["mailbox"], row["uidvalidity"], row["uid"])] = dict(row)
    result = []
    for message in provider.discover(limit):
        row = known.get((message.mailbox, message.uidvalidity, message.uid), {})
        result.append({**asdict(message), "known_state": row.get("provider_state", "unknown"),
                       "archive_id": row.get("archive_id")})
    return result


def dry_run_archive(config: Config, provider: Provider, limit: int | None,
                    with_enrichment: bool) -> list[dict[str, Any]]:
    plan: list[dict[str, Any]] = []
    byte_total = 0
    for remote in provider.discover(limit or config.limits.messages_per_run):
        raw = provider.fetch(remote)
        byte_total += len(raw)
        if byte_total > config.limits.bytes_per_run:
            break
        digest, archive_id = identity(raw)
        parsed = parse_message(raw)
        item: dict[str, Any] = {
            "remote": asdict(remote), "archive_id": archive_id, "raw_sha256": digest,
            "object_path": str(config.paths.archive_root / object_relative_path(digest, archive_id)),
            "note_name": note_filename(parsed, archive_id),
            "actions": ["preserve", "enrich", "write_note", "verify", "mark_processed"],
        }
        if with_enrichment:
            item["enrichment"] = asdict(enrich(config, parsed.body_markdown))
        plan.append(item)
    return plan


def archive(config: Config, provider: Provider, limit: int | None = None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    with data_lock(config.paths.archive_root):
        state = _setup(config)
        reconcile_staging(config.paths.archive_root)
        byte_total = 0
        candidates = provider.discover(limit or config.limits.messages_per_run)
        for remote in candidates:
            remote_id = state.discover(remote)
            config_hash = config.fingerprint()
            try:
                with state.attempt("fetch", config_hash, remote_id=remote_id):
                    raw = provider.fetch(remote)
                byte_total += len(raw)
                if byte_total > config.limits.bytes_per_run:
                    results.append({"uid": remote.uid, "outcome": "deferred", "reason": "byte limit"})
                    break
                with state.attempt("preservation", config_hash, remote_id=remote_id):
                    parsed = parse_message(raw)
                    stored = store(config.paths.archive_root, raw, parsed)
                    relative = stored.object_dir.relative_to(config.paths.archive_root).as_posix()
                    state.record_preserved(remote_id, stored, parsed, relative, config_hash)
                    local = verify_archive(state, config.paths.archive_root, config.notes_dir, stored.archive_id)
                    if not local.ok:
                        raise RuntimeError("; ".join(local.errors))
                    state.set_archive_state(stored.archive_id, "preservation", "preservation_verified")

                try:
                    with state.attempt("enrichment", config_hash, archive_id=stored.archive_id,
                                       remote_id=remote_id):
                        if config.enrichment.provider == "none":
                            enrichment = no_enrichment(config, parsed.body_markdown)
                            state.set_archive_state(stored.archive_id, "enrichment", "skipped")
                        else:
                            submitted = (redact(parsed.body_markdown)[0]
                                         if config.enrichment.provider == "jev"
                                         else parsed.body_markdown)
                            enrichment = state.load_cached_enrichment(
                                stored.archive_id, config.enrichment.provider,
                                config.enrichment.model, input_hash(submitted), config_hash,
                            )
                            if enrichment is None:
                                enrichment = enrich(config, parsed.body_markdown)
                                state.save_enrichment(stored.archive_id, enrichment)
                            else:
                                state.set_archive_state(stored.archive_id, "enrichment", "complete")
                except Exception:
                    state.set_archive_state(stored.archive_id, "enrichment", "failed")
                    raise
                try:
                    with state.attempt("markdown", config_hash, archive_id=stored.archive_id,
                                       remote_id=remote_id):
                        filename = note_filename(parsed, stored.archive_id)
                        note_path = config.notes_dir / filename
                        existing = note_path.read_text(encoding="utf-8") if note_path.exists() else None
                        content, managed_hash = build_note(
                            parsed, stored, config.account.account_id, config.paths.archive_root,
                            enrichment, existing=existing,
                        )
                        write_note(config.notes_dir, filename, content)
                        state.set_note(stored.archive_id, filename, managed_hash)
                        full = verify_archive(state, config.paths.archive_root, config.notes_dir, stored.archive_id)
                        if not full.ok:
                            raise RuntimeError("; ".join(full.errors))
                except Exception:
                    state.set_archive_state(stored.archive_id, "markdown", "failed")
                    raise
                if not state.complete_verified(stored.archive_id):
                    raise RuntimeError("archive did not reach complete_verified")
                with state.attempt("provider_move", config_hash, archive_id=stored.archive_id,
                                   remote_id=remote_id):
                    provider.mark_processed(remote)
                    state.mark_processed(remote_id)
                results.append({"uid": remote.uid, "archive_id": stored.archive_id,
                                "outcome": "processed", "deduplicated": stored.deduplicated})
            except Exception as exc:
                results.append({"uid": remote.uid, "outcome": "failed",
                                "error": f"{type(exc).__name__}: {exc}"})
    return results


def prune(config: Config, provider: Provider, execute: bool) -> list[dict[str, Any]]:
    if not config.paths.database.exists():
        return []
    state = State(config.paths.database)
    output: list[dict[str, Any]] = []
    lock = data_lock(config.paths.archive_root) if execute else nullcontext()
    with lock:
        previously_trashed = state.trashed_messages()
        cutoff = datetime.now(UTC) - timedelta(days=config.retention_days)
        for row in state.prune_candidates():
            processed_at = datetime.fromisoformat(row["processed_at"]) if row["processed_at"] else None
            gates: dict[str, bool] = {
                "deletion_enabled": config.deletion_enabled,
                "gmail_provider": config.account.provider == "gmail",
                "retention_elapsed": bool(processed_at and processed_at <= cutoff),
                "preservation_verified": row["preservation_state"] == "preservation_verified",
                "enrichment_ready": row["enrichment_state"] in ("complete", "skipped"),
                "markdown_complete": row["markdown_state"] == "complete",
                "gmail_identity_present": bool(row["gmail_msgid"]),
                "local_verification": False,
                "unique_remote_identity": False,
                "remote_hash_match": False,
            }
            report = verify_archive(state, config.paths.archive_root, config.notes_dir, row["archive_id"])
            gates["local_verification"] = report.ok
            found = provider.find_gmail(row["gmail_msgid"], config.account.processed_label)
            gates["unique_remote_identity"] = len(found) == 1
            if len(found) == 1:
                raw = provider.fetch(found[0])
                gates["remote_hash_match"] = hashlib.sha256(raw).hexdigest() == row["raw_sha256"]
            eligible = all(gates.values())
            item = {"archive_id": row["archive_id"], "gmail_msgid": row["gmail_msgid"],
                    "gates": gates, "eligible": eligible, "action": "none"}
            if execute and eligible:
                provider.move_to_trash(found[0])
                state.mark_trashed(row["id"])
                item["action"] = "moved_to_trash"
            output.append(item)
        trash_mailbox = provider.trash_mailbox() if execute and config.account.provider == "gmail" else None
        for row in previously_trashed:
            if not execute:
                output.append({"archive_id": row["archive_id"], "gmail_msgid": row["gmail_msgid"],
                               "state": "trashed", "action": "absence_not_checked_in_dry_run"})
                continue
            found = provider.find_gmail(row["gmail_msgid"], trash_mailbox)
            if not found:
                state.mark_absent(row["id"])
                output.append({"archive_id": row["archive_id"], "gmail_msgid": row["gmail_msgid"],
                               "state": "absent", "action": "recorded_deleted"})
    return output


def verify(config: Config, archive_id: str | None = None):
    state = State(config.paths.database)
    return verify_all(state, config.paths.archive_root, config.notes_dir, archive_id)
