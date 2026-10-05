from datetime import datetime
import os
from pathlib import Path
import plistlib
import sqlite3
import subprocess
from zoneinfo import ZoneInfo

from mailkeep.schedule_status import LABEL, render_status


TZ = ZoneInfo("Australia/Melbourne")
NOW = datetime(2026, 10, 5, 18, 30, tzinfo=TZ)


def _install_plist(home: Path, log_dir: Path) -> None:
    target = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    target.parent.mkdir(parents=True)
    with target.open("wb") as handle:
        plistlib.dump(
            {
                "Label": LABEL,
                "StartCalendarInterval": {"Hour": 9, "Minute": 0},
                "StandardOutPath": str(log_dir / "periodic.out.log"),
                "StandardErrorPath": str(log_dir / "periodic.err.log"),
            },
            handle,
        )


def _runner(output: str, returncode: int = 0):
    calls: list[list[str]] = []

    def run(args, **kwargs):
        calls.append(args)
        assert kwargs == {"capture_output": True, "text": True, "check": False}
        return subprocess.CompletedProcess(args, returncode, output, "")

    return run, calls


def _archive_database(tmp_path: Path) -> Path:
    database = tmp_path / "mailkeep.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.executescript(
            "CREATE TABLE archives (preservation_state, enrichment_state, "
            "markdown_state, created_at);"
            "CREATE TABLE remote_messages (provider_state);"
            "CREATE TABLE attempts (outcome);"
            "CREATE TABLE attachment_extraction_attempts "
            "(id INTEGER, attachment_id INTEGER, outcome);"
            "INSERT INTO archives VALUES "
            "('preservation_verified','complete','complete','2026-10-05T01:00:00+00:00'),"
            "('preservation_verified','failed','complete','2026-10-05T02:00:00+00:00');"
            "INSERT INTO remote_messages VALUES ('processed'),('source');"
            "INSERT INTO attempts VALUES ('failed'),('success');"
            "INSERT INTO attachment_extraction_attempts VALUES "
            "(1,1,'failed'),(2,1,'success'),(3,2,'unsupported');"
        )
    config = tmp_path / "config.yaml"
    config.write_text(f"paths:\n  database: {database}\n", encoding="utf-8")
    return config


def test_status_reports_loaded_job_last_activity_and_next_run(tmp_path: Path):
    home = tmp_path / "home"
    logs = tmp_path / "logs"
    logs.mkdir()
    _install_plist(home, logs)
    output_log = logs / "periodic.out.log"
    output_log.write_text("done\n", encoding="utf-8")
    activity = datetime(2026, 10, 5, 9, 11, 19, tzinfo=TZ)
    os.utime(output_log, (activity.timestamp(), activity.timestamp()))
    runner, calls = _runner(
        "state = not running\nruns = 3\nlast exit code = 0\n"
    )
    config = _archive_database(tmp_path)

    rendered, exit_code = render_status(
        home=home, uid=501, now=NOW, config_path=config, runner=runner
    )

    assert exit_code == 0
    assert "State: loaded; not running" in rendered
    assert "Runs since load: 3" in rendered
    assert "Last run: 2026-10-05 09:11:19 AEDT (inferred" in rendered
    assert "Last result: success (exit code 0)" in rendered
    assert "Next run: 2026-10-06 09:00:00 AEDT" in rendered
    assert str(output_log) in rendered
    assert "Messages archived: 2" in rendered
    assert "Messages processed: 1" in rendered
    assert "Preservation verified: 2" in rendered
    assert "Enrichment complete: 1" in rendered
    assert "Provider: processed 1, source 1, trashed 0, absent 0" in rendered
    assert "Attachments: success 1, unsupported 1, failed 0" in rendered
    assert "Latest archive: 2026-10-05 13:00:00 AEDT" in rendered
    assert "Historical failed attempts: 1" in rendered
    database = tmp_path / "mailkeep.sqlite3"
    assert not database.with_name(f"{database.name}-wal").exists()
    assert not database.with_name(f"{database.name}-shm").exists()
    assert 'launchctl kickstart -k "gui/501/local.mailkeep.daily"' in rendered
    assert calls == [["/bin/launchctl", "print", "gui/501/local.mailkeep.daily"]]


def test_status_reports_failed_job_without_logs(tmp_path: Path):
    home = tmp_path / "home"
    _install_plist(home, tmp_path / "missing-logs")
    runner, _ = _runner("state = not running\nruns = 1\nlast exit code = 1\n")

    rendered, exit_code = render_status(
        home=home, uid=501, now=NOW, runner=runner
    )

    assert exit_code == 0
    assert "Last run: unknown (no scheduler log activity)" in rendered
    assert "Last result: failure (exit code 1)" in rendered


def test_status_reports_installed_but_unloaded_job(tmp_path: Path):
    home = tmp_path / "home"
    _install_plist(home, tmp_path / "logs")
    runner, _ = _runner("", returncode=113)

    rendered, exit_code = render_status(
        home=home, uid=501, now=NOW, runner=runner
    )

    assert exit_code == 1
    assert "State: installed but not loaded" in rendered
    assert "Last result: unavailable (service is not loaded)" in rendered
    assert "Next run: 2026-10-06 09:00:00 AEDT" in rendered


def test_status_reports_missing_plist_without_calling_launchctl(tmp_path: Path):
    def unexpected(*args, **kwargs):
        raise AssertionError("launchctl must not be called")

    rendered, exit_code = render_status(
        home=tmp_path / "home", uid=501, now=NOW, runner=unexpected
    )

    assert exit_code == 1
    assert "State: not installed" in rendered
    assert "Last run: unknown (LaunchAgent plist is missing)" in rendered


def test_status_keeps_scheduler_readable_when_database_is_missing(tmp_path: Path):
    home = tmp_path / "home"
    _install_plist(home, tmp_path / "logs")
    config = tmp_path / "config.yaml"
    config.write_text("paths:\n  database: missing.sqlite3\n", encoding="utf-8")
    runner, _ = _runner("state = not running\nruns = 0\nlast exit code = (never exited)\n")

    rendered, exit_code = render_status(
        home=home, uid=501, now=NOW, config_path=config, runner=runner
    )

    assert exit_code == 0
    assert "State: loaded; not running" in rendered
    assert "Archive: unavailable (configured database is missing)" in rendered


def test_status_refuses_archive_snapshot_with_active_wal(tmp_path: Path):
    home = tmp_path / "home"
    _install_plist(home, tmp_path / "logs")
    config = _archive_database(tmp_path)
    database = tmp_path / "mailkeep.sqlite3"
    database.with_name(f"{database.name}-wal").write_bytes(b"active")
    runner, _ = _runner("state = running\nruns = 1\nlast exit code = (never exited)\n")

    rendered, exit_code = render_status(
        home=home, uid=501, now=NOW, config_path=config, runner=runner
    )

    assert exit_code == 0
    assert "State: loaded; running" in rendered
    assert "Archive: unavailable (database has an active WAL)" in rendered
