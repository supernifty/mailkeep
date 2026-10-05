from __future__ import annotations

import os
from datetime import datetime, time, timedelta
from pathlib import Path
import plistlib
import re
import sqlite3
import subprocess
from typing import Callable

import yaml


LABEL = "local.mailkeep.daily"


def _field(output: str, name: str) -> str | None:
    match = re.search(rf"^\s*{re.escape(name)} = (.+)$", output, re.MULTILINE)
    return match.group(1).strip() if match else None


def _next_run(now: datetime, schedule: object) -> datetime | None:
    if not isinstance(schedule, dict):
        return None
    try:
        hour = int(schedule["Hour"])
        minute = int(schedule["Minute"])
    except (KeyError, TypeError, ValueError):
        return None
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    candidate = datetime.combine(now.date(), time(hour, minute), tzinfo=now.tzinfo)
    if candidate <= now:
        candidate = datetime.combine(
            now.date() + timedelta(days=1), time(hour, minute), tzinfo=now.tzinfo
        )
    return candidate


def _format_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S %Z").rstrip()


def _last_activity(paths: list[Path], now: datetime) -> datetime | None:
    timestamps = [path.stat().st_mtime for path in paths if path.is_file()]
    if not timestamps:
        return None
    return datetime.fromtimestamp(max(timestamps), tz=now.tzinfo)


def _database_path(config_path: Path) -> Path:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    value = raw["paths"]["database"]
    expanded = Path(os.path.expandvars(str(value))).expanduser()
    return expanded if expanded.is_absolute() else (config_path.parent / expanded).resolve()


def _archive_stats(config_path: Path | None, now: datetime) -> list[str]:
    if config_path is None or not config_path.is_file():
        return ["Archive: unavailable (configuration file is missing)"]
    try:
        database = _database_path(config_path)
        if not database.is_file():
            return ["Archive: unavailable (configured database is missing)"]
        wal = database.with_name(f"{database.name}-wal")
        if wal.is_file() and wal.stat().st_size:
            return ["Archive: unavailable (database has an active WAL)"]
        uri = f"{database.resolve().as_uri()}?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True) as conn:
            conn.execute("PRAGMA query_only=ON")
            total, verified, enriched, notes, latest = conn.execute(
                "SELECT COUNT(*),"
                "SUM(preservation_state='preservation_verified'),"
                "SUM(enrichment_state='complete'),SUM(markdown_state='complete'),"
                "MAX(created_at) FROM archives"
            ).fetchone()
            provider = dict(conn.execute(
                "SELECT provider_state,COUNT(*) FROM remote_messages GROUP BY provider_state"
            ))
            failed_attempts = conn.execute(
                "SELECT COUNT(*) FROM attempts WHERE outcome='failed'"
            ).fetchone()[0]
            try:
                attachments = dict(conn.execute(
                    "SELECT outcome,COUNT(*) FROM attachment_extraction_attempts "
                    "WHERE id IN (SELECT MAX(id) FROM attachment_extraction_attempts "
                    "GROUP BY attachment_id) GROUP BY outcome"
                ))
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc):
                    raise
                attachments = {}
    except (KeyError, TypeError, ValueError, OSError, sqlite3.Error, yaml.YAMLError):
        return ["Archive: unavailable (configuration or database could not be read)"]

    latest_text = "none"
    if latest:
        try:
            latest_value = datetime.fromisoformat(str(latest)).astimezone(now.tzinfo)
            latest_text = _format_time(latest_value)
        except ValueError:
            latest_text = str(latest)
    return [
        "Archive:",
        f"  Messages archived: {total}",
        f"  Messages processed: {provider.get('processed', 0)}",
        f"  Preservation verified: {verified or 0}",
        f"  Enrichment complete: {enriched or 0}",
        f"  Notes complete: {notes or 0}",
        "  Provider: " + ", ".join(
            f"{name} {provider.get(name, 0)}"
            for name in ("processed", "source", "trashed", "absent")
        ),
        "  Attachments: " + ", ".join(
            f"{name} {attachments.get(name, 0)}"
            for name in ("success", "unsupported", "failed")
        ),
        f"  Latest archive: {latest_text}",
        f"  Historical failed attempts: {failed_attempts}",
    ]


def render_status(
    *,
    home: Path,
    uid: int,
    now: datetime,
    config_path: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[str, int]:
    domain = f"gui/{uid}/{LABEL}"
    plist_path = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    trigger = f'launchctl kickstart -k "{domain}"'

    if not plist_path.is_file():
        lines = [
            "Mailkeep daily service",
            "State: not installed",
            "Runs since load: unavailable",
            "Last run: unknown (LaunchAgent plist is missing)",
            "Last result: unavailable",
            "Next run: unavailable",
            f"Plist: {plist_path}",
        ]
        lines.extend(_archive_stats(config_path, now))
        lines.append(f"Trigger: {trigger}")
        return "\n".join(lines), 1

    with plist_path.open("rb") as handle:
        job = plistlib.load(handle)
    log_paths = [
        Path(value).expanduser()
        for value in (job.get("StandardOutPath"), job.get("StandardErrorPath"))
        if isinstance(value, str)
    ]

    result = runner(
        ["/bin/launchctl", "print", domain],
        capture_output=True,
        text=True,
        check=False,
    )
    loaded = result.returncode == 0
    output = result.stdout if loaded else ""
    state = _field(output, "state") if loaded else None
    runs = _field(output, "runs") if loaded else None
    last_exit = _field(output, "last exit code") if loaded else None

    activity = _last_activity(log_paths, now)
    if activity is None:
        last_run = "unknown (no scheduler log activity)"
    else:
        last_run = (
            f"{_format_time(activity)} "
            "(inferred from latest scheduler log write; may predate current load)"
        )

    if not loaded:
        last_result = "unavailable (service is not loaded)"
    elif last_exit == "0":
        last_result = "success (exit code 0)"
    elif last_exit and last_exit != "(never exited)":
        last_result = f"failure (exit code {last_exit})"
    else:
        last_result = "not recorded since current load"

    next_value = _next_run(now, job.get("StartCalendarInterval"))
    next_run = _format_time(next_value) if next_value else "unavailable"

    lines = [
        "Mailkeep daily service",
        f"State: {'loaded; ' + (state or 'state unknown') if loaded else 'installed but not loaded'}",
        f"Runs since load: {runs or 'unavailable'}",
        f"Last run: {last_run}",
        f"Last result: {last_result}",
        f"Next run: {next_run}",
    ]
    if log_paths:
        lines.append("Logs:")
        lines.extend(f"  {path}" for path in log_paths)
    else:
        lines.append("Logs: unavailable")
    lines.extend(_archive_stats(config_path, now))
    lines.append(f"Trigger: {trigger}")
    return "\n".join(lines), 0 if loaded else 1


def main() -> int:
    config_path = Path(os.environ.get("MAILKEEP_CONFIG", "config.yaml")).expanduser().resolve()
    output, exit_code = render_status(
        home=Path.home(), uid=os.getuid(), now=datetime.now().astimezone(),
        config_path=config_path,
    )
    print(output)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
