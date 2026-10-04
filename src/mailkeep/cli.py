from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from . import __version__
from .config import Config, load_config
from .enrichment import list_jev_models
from .provider import connect
from .service import (archive, dry_run_archive, extract_attachments,
                      organize_notes, prune, regenerate_notes, scan, verify)
from .state import State


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="mailkeep")
    root.add_argument("--config", default="config.yaml", help="YAML configuration file")
    root.add_argument("--version", action="version", version=f"mailkeep {__version__}")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="validate local and remote configuration")
    scan_parser = commands.add_parser("scan", help="list source-label candidates")
    scan_parser.add_argument("--limit", type=int)
    archive_parser = commands.add_parser("archive", help="archive or plan source messages")
    archive_parser.add_argument("--limit", type=int)
    archive_parser.add_argument("--dry-run", action="store_true")
    archive_parser.add_argument("--with-enrichment", action="store_true")
    status_parser = commands.add_parser("status", help="show lifecycle state")
    status_parser.add_argument("--json", action="store_true")
    verify_parser = commands.add_parser("verify", help="verify preserved data")
    verify_parser.add_argument("--archive-id")
    organize_parser = commands.add_parser("organize-notes", help="organize notes by message date")
    organize_modes = organize_parser.add_mutually_exclusive_group(required=True)
    organize_modes.add_argument("--dry-run", action="store_true")
    organize_modes.add_argument("--execute", action="store_true")
    extract_parser = commands.add_parser(
        "extract-attachments", help="backfill or retry local attachment extraction"
    )
    extract_modes = extract_parser.add_mutually_exclusive_group(required=True)
    extract_modes.add_argument("--dry-run", action="store_true")
    extract_modes.add_argument("--execute", action="store_true")
    extract_parser.add_argument("--archive-id")
    extract_parser.add_argument("--retry-failed", action="store_true")
    regenerate_parser = commands.add_parser(
        "regenerate-notes", help="regenerate managed Obsidian reading notes"
    )
    regenerate_modes = regenerate_parser.add_mutually_exclusive_group(required=True)
    regenerate_modes.add_argument("--dry-run", action="store_true")
    regenerate_modes.add_argument("--execute", action="store_true")
    regenerate_parser.add_argument("--archive-id")
    prune_parser = commands.add_parser("prune", help="evaluate or execute retention")
    modes = prune_parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--execute", action="store_true")
    return root


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str))


def _prune_has_blocked_due_item(result: list[dict[str, Any]]) -> bool:
    return any(
        item.get("gates", {}).get("retention_elapsed", False) and not item.get("eligible", False)
        for item in result
    )


def doctor(config: Config) -> tuple[bool, list[dict[str, Any]]]:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})

    add("archive_parent", config.paths.archive_root.parent.is_dir(), str(config.paths.archive_root.parent))
    add("vault", config.paths.obsidian_vault.is_dir(), str(config.paths.obsidian_vault))
    add("gmail_secret", config.password() is not None, config.account.password_env)
    if config.enrichment.provider == "jev":
        try:
            import typesafe_sdk  # noqa: F401
            sdk = True
        except ImportError:
            sdk = False
        add("jev_sdk", sdk, "official typesafe-sdk import")
        add("jev_secret", config.enrichment_key() is not None,
            config.enrichment.api_key_env or "not configured")
        if sdk and config.enrichment_key() is not None:
            try:
                models = list_jev_models(config)
                add("jev_model", config.enrichment.model in models,
                    f"configured={config.enrichment.model!r}; available={models}")
            except Exception as exc:
                add("jev_model", False, f"{type(exc).__name__}: {exc}")
    elif config.enrichment.provider == "ollama":
        try:
            response = httpx.get((config.enrichment.endpoint or "http://localhost:11434").rstrip("/") + "/api/tags",
                                 timeout=config.enrichment.timeout_seconds)
            response.raise_for_status()
            models = [item.get("name") for item in response.json().get("models", [])]
            add("ollama", config.enrichment.model in models,
                f"configured model {config.enrichment.model!r}; available={models}")
        except Exception as exc:
            add("ollama", False, f"{type(exc).__name__}: {exc}")
    try:
        if config.paths.database.exists():
            with State(config.paths.database).connect() as conn:
                conn.execute("SELECT 1").fetchone()
            add("sqlite", True, "existing database is readable")
        else:
            add("sqlite", config.paths.database.parent.is_dir(), "database will be created by archive")
    except sqlite3.Error as exc:
        add("sqlite", False, str(exc))
    remote = None
    try:
        remote = connect(config)
        caps = remote.capabilities()
        folders = remote.folders()
        add("imap", True, f"capabilities={sorted(caps)}")
        names = {name for _, name in folders}
        add("source_label", config.account.source_label in names, config.account.source_label)
        add("processed_label", config.account.processed_label in names, config.account.processed_label)
        if config.account.provider == "gmail":
            add("gmail_extensions", "X-GM-EXT-1" in caps, "X-GM-EXT-1")
            try:
                add("gmail_trash", bool(remote.trash_mailbox()), remote.trash_mailbox())
            except Exception as exc:
                add("gmail_trash", False, str(exc))
    except Exception as exc:
        add("imap", False, f"{type(exc).__name__}: {exc}")
    finally:
        if remote:
            remote.close()
    return all(check["ok"] for check in checks), checks


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except (OSError, ValueError, ValidationError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if args.command == "doctor":
        ok, checks = doctor(config)
        _print_json(checks)
        return 0 if ok else 1
    if args.command == "status":
        if not config.paths.database.exists():
            value = {"states": {}, "failed_attempts": 0, "recent_events": []}
        else:
            value = State(config.paths.database).status()
        if args.json:
            print(json.dumps(value, sort_keys=True, default=str))
        else:
            _print_json(value)
        return 0
    if args.command == "verify":
        if not config.paths.database.exists():
            print("verification error: database does not exist", file=sys.stderr)
            return 1
        report = verify(config, args.archive_id)
        _print_json({"ok": report.ok, "checked": report.checked,
                     "errors": report.errors, "warnings": report.warnings})
        return 0 if report.ok else 1
    if args.command == "organize-notes":
        try:
            result = organize_notes(config, execute=args.execute)
            _print_json(result)
            return 0
        except Exception as exc:
            print(f"organize-notes error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    if args.command == "extract-attachments":
        if args.retry_failed and not args.execute:
            print("usage error: --retry-failed requires --execute", file=sys.stderr)
            return 2
        try:
            result = extract_attachments(
                config, execute=args.execute, archive_id=args.archive_id,
                retry_failed=args.retry_failed,
            )
            _print_json(result)
            return 0
        except Exception as exc:
            print(f"extract-attachments error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    if args.command == "regenerate-notes":
        try:
            result = regenerate_notes(
                config, execute=args.execute, archive_id=args.archive_id,
            )
            _print_json(result)
            return 0
        except Exception as exc:
            print(f"regenerate-notes error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

    remote = None
    try:
        remote = connect(config)
        if args.command == "scan":
            _print_json(scan(config, remote, args.limit))
            return 0
        if args.command == "archive":
            if args.with_enrichment and not args.dry_run:
                print("usage error: --with-enrichment is only valid with --dry-run", file=sys.stderr)
                return 2
            result = (dry_run_archive(config, remote, args.limit, args.with_enrichment)
                      if args.dry_run else archive(config, remote, args.limit))
            _print_json(result)
            return 0 if all(item.get("outcome") != "failed" for item in result) else 1
        if args.command == "prune":
            if args.execute and not config.deletion_enabled:
                print("prune refused: deletion_enabled is false", file=sys.stderr)
                return 2
            result = prune(config, remote, execute=args.execute)
            _print_json(result)
            return 1 if args.execute and _prune_has_blocked_due_item(result) else 0
    except Exception as exc:
        print(f"{args.command} error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if remote:
            remote.close()
    return 2
