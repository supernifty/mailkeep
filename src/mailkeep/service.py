from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .attachments import extract_archive, plan_archive
from .config import Config
from .enrichment import enrich, input_hash, no_enrichment, redact
from .locking import data_lock
from .mime import parse_message
from .models import EnrichmentResult, RemoteMessage, StoredObject
from .notes import build_note, note_relative_path, parse_managed, write_note
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
            "note_path": note_relative_path(parsed, archive_id).as_posix(),
            "actions": ["preserve", "extract_attachments", "enrich", "write_note", "verify", "mark_processed"],
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
                    local = verify_archive(
                        state, config.paths.archive_root, config.notes_dir, stored.archive_id,
                        include_derived=False, include_note=False,
                    )
                    if not local.ok:
                        raise RuntimeError("; ".join(local.errors))
                    state.set_archive_state(stored.archive_id, "preservation", "preservation_verified")

                if config.attachment_extraction.enabled:
                    try:
                        with state.attempt("attachment_extraction", config_hash,
                                           archive_id=stored.archive_id, remote_id=remote_id):
                            extract_archive(config, state, stored.archive_id)
                    except Exception:
                        # Derived extraction is deliberately independent of archive completion.
                        pass

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
                            enrichment_hash = config.enrichment_fingerprint()
                            enrichment = state.load_cached_enrichment(
                                stored.archive_id, config.enrichment.provider,
                                config.enrichment.model, input_hash(submitted), enrichment_hash,
                            )
                            if enrichment is None:
                                enrichment = enrich(config, parsed.body_markdown)
                                enrichment = EnrichmentResult(
                                    **{**asdict(enrichment), "config_hash": enrichment_hash}
                                )
                                state.save_enrichment(stored.archive_id, enrichment)
                            else:
                                state.set_archive_state(stored.archive_id, "enrichment", "complete")
                except Exception:
                    state.set_archive_state(stored.archive_id, "enrichment", "failed")
                    raise
                try:
                    with state.attempt("markdown", config_hash, archive_id=stored.archive_id,
                                       remote_id=remote_id):
                        relative_path = note_relative_path(parsed, stored.archive_id)
                        note_path = config.notes_dir / relative_path
                        existing = note_path.read_text(encoding="utf-8") if note_path.exists() else None
                        content, managed_hash = build_note(
                            parsed, stored, config.account.account_id, config.paths.archive_root,
                            enrichment, existing=existing,
                            extractions=state.latest_extractions(stored.archive_id),
                            note_chars_per_attachment=config.attachment_extraction.note_chars_per_attachment,
                            note_chars_per_message=config.attachment_extraction.note_chars_per_message,
                        )
                        write_note(config.notes_dir, relative_path, content)
                        state.set_note(stored.archive_id, relative_path.as_posix(), managed_hash)
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


def _regenerate_note(config: Config, state: State, archive_id: str) -> str:
    with state.connect() as conn:
        row = conn.execute("SELECT * FROM archives WHERE archive_id=?", (archive_id,)).fetchone()
    if row is None:
        raise KeyError(f"unknown archive: {archive_id}")
    relative_object = Path(row["object_relpath"])
    archive_root = config.paths.archive_root.resolve()
    if relative_object.is_absolute() or ".." in relative_object.parts:
        raise ValueError(f"unsafe archive object path: {row['object_relpath']}")
    object_dir = (archive_root / relative_object).resolve()
    if not object_dir.is_relative_to(archive_root):
        raise ValueError(f"archive object path escapes root: {row['object_relpath']}")
    raw = (object_dir / "original.eml").read_bytes()
    if hashlib.sha256(raw).hexdigest() != row["raw_sha256"]:
        raise RuntimeError(f"raw SHA-256 mismatch for {archive_id}")
    parsed = parse_message(raw)
    stored = StoredObject(
        archive_id=archive_id, raw_sha256=row["raw_sha256"], object_dir=object_dir,
        eml_path=object_dir / "original.eml", metadata_path=object_dir / "metadata.json",
        deduplicated=True,
    )
    enrichment = state.latest_enrichment(archive_id)
    if enrichment is None:
        if row["enrichment_state"] != "skipped":
            raise RuntimeError(f"archive {archive_id} has no reusable enrichment result")
        enrichment = no_enrichment(config, parsed.body_markdown)
    relative = Path(row["note_relpath"] or note_relative_path(parsed, archive_id))
    note_path = config.notes_dir / relative
    existing = note_path.read_text(encoding="utf-8") if note_path.exists() else None
    processed_at = None
    if existing:
        try:
            processed_at = parse_managed(existing)[0].get("processed_at")
        except ValueError:
            processed_at = None
    content, managed_hash = build_note(
        parsed, stored, config.account.account_id, config.paths.archive_root, enrichment,
        existing=existing, processed_at=processed_at,
        extractions=state.latest_extractions(archive_id),
        note_chars_per_attachment=config.attachment_extraction.note_chars_per_attachment,
        note_chars_per_message=config.attachment_extraction.note_chars_per_message,
    )
    write_note(config.notes_dir, relative, content)
    state.set_note(archive_id, relative.as_posix(), managed_hash)
    return relative.as_posix()


def extract_attachments(config: Config, execute: bool, archive_id: str | None = None,
                        retry_failed: bool = False) -> list[dict[str, Any]]:
    if not config.paths.database.exists():
        raise RuntimeError("database does not exist")
    state = State(config.paths.database, readonly=not execute)
    if execute:
        state.initialize()
    plans = plan_archive(config, state, archive_id, retry_failed)
    if not execute:
        return plans
    archive_ids = sorted({item["archive_id"] for item in plans})
    output: list[dict[str, Any]] = []
    with data_lock(config.paths.archive_root):
        for current in archive_ids:
            items = extract_archive(config, state, current, retry_failed, force=True)
            note = _regenerate_note(config, state, current)
            output.append({"archive_id": current, "attachments": items,
                           "note": note, "outcome": "complete"})
    return output


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


def organize_notes(config: Config, execute: bool) -> list[dict[str, Any]]:
    if not config.paths.database.exists():
        return []
    state = State(config.paths.database)
    with state.connect() as conn:
        rows = conn.execute(
            "SELECT archive_id,message_date,note_relpath FROM archives "
            "WHERE markdown_state='complete' ORDER BY archive_id"
        ).fetchall()

    plans: list[tuple[str, str, str, Path, Path]] = []
    output: list[dict[str, Any]] = []
    for row in rows:
        old = row["note_relpath"]
        if not old:
            raise RuntimeError(f"complete Markdown has no note path for {row['archive_id']}")
        if row["message_date"]:
            message_date = datetime.fromisoformat(row["message_date"])
            directory = Path(f"{message_date.year:04d}") / f"{message_date.month:02d}"
        else:
            directory = Path("undated")
        new = (directory / Path(old).name).as_posix()
        item = {"archive_id": row["archive_id"], "from": old, "to": new,
                "action": "already_organized" if old == new else "planned"}
        output.append(item)
        if old != new:
            source = config.notes_dir / old
            destination = config.notes_dir / new
            plans.append((row["archive_id"], old, new, source, destination))

    if not execute:
        return output

    with data_lock(config.paths.archive_root):
        for archive_id, _, _, source, destination in plans:
            report = verify_archive(
                state, config.paths.archive_root, config.notes_dir, archive_id
            )
            if not report.ok:
                raise RuntimeError("; ".join(report.errors))
            if not source.is_file():
                raise RuntimeError(f"note source does not exist: {source}")
            if destination.exists():
                raise RuntimeError(f"note destination already exists: {destination}")

        moved: list[tuple[Path, Path]] = []
        try:
            for _, _, _, source, destination in plans:
                destination.parent.mkdir(parents=True, exist_ok=True)
                source.replace(destination)
                moved.append((source, destination))
            state.update_note_paths([(archive_id, old, new)
                                     for archive_id, old, new, _, _ in plans])
        except BaseException:
            for source, destination in reversed(moved):
                if destination.exists() and not source.exists():
                    destination.replace(source)
            raise

    for item in output:
        if item["action"] == "planned":
            item["action"] = "moved"
    return output
