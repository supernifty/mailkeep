from __future__ import annotations

import hashlib
import json
from datetime import date
from email.message import EmailMessage
from pathlib import Path

import pytest
from pydantic import ValidationError

from mailkeep.cli import main, parser
from mailkeep.config import Config, ProcessorInstance
from mailkeep.processors import engine, reports
from mailkeep.processors.api import canonical, digest, validate_fields
from mailkeep.processors.rescuetime import DEFINITION, create, duration
from mailkeep.service import archive, verify
from mailkeep.state import State
from test_service_verification import FakeProvider


def report(actual="17h 8m", *, threshold="10h 30m", start="October 25, 2026",
           end="November  1, 2026", html_actual=None, plain=True, html=True,
           qualifier="All day, every day", sender="support@rescuetime.com", extra=""):
    """Synthetic structure only: no personal activity, URLs, or original headers."""
    message = EmailMessage()
    message["From"] = f"RescueTime Team <{sender}>"
    message["To"] = "reader@example.invalid"
    message["Subject"] = "14h 24m of Example Category - RescueTime Weekly Summary"
    message["Date"] = "Sat, 31 Oct 2026 23:06:44 +0000"
    text = (f"Your weekly summary\n{start} - {end}\n\nTOTAL TIME\n55h 6m\n"
            "YOUR GOALS FOR THE WEEK\nMore than 21h on All Productive Time\n"
            "All day, every day\n32h 30m\n4h 38m avg per day\n"
            f"Less than {threshold} on All Distracting Time\n*****\n"
            f"{qualifier}\n{actual}\n2h 26m avg per day\n{extra}")
    html_text = (f"<h2>Your weekly summary</h2><h2>{start} - {end}</h2>"
                 "<h3>YOUR GOALS FOR THE WEEK</h3><table><tr><td>"
                 f"<h1>Less than <strong>{threshold}</strong> on All Distracting Time</h1>"
                 f"<small>{qualifier}</small></td></tr><tr><td><p>{html_actual or actual}</p>"
                 "<small>2h 26m avg per day</small></td></tr></table>")
    if plain:
        message.set_content(text)
        if html:
            message.add_alternative(html_text, subtype="html")
    else:
        message.set_content(html_text, subtype="html")
    return message.as_bytes()


def plugin_dict():
    return {"id": "personal-rescuetime", "implementation": "rescuetime-weekly",
            "series_id": "personal", "match": {"from_addresses": ["support@rescuetime.com"],
            "subject_contains": ["RescueTime Weekly Summary"]},
            "options": {"report_timezone": "Australia/Melbourne"}}


def enabled(config):
    values = config.model_dump()
    values["processors"] = {"enabled": True, "instances": [plugin_dict()],
                            "scratch_root": config.paths.database.parent / "scratch"}
    return Config.model_validate(values)


def input_message(raw):
    return engine.message_input(raw, {"archive_id": "synthetic", "raw_sha256": hashlib.sha256(raw).hexdigest()})


@pytest.mark.parametrize("actual,seconds", [("17h 8m", 61680), ("0m", 0), ("0h 0m", 0),
                                            ("1h", 3600), ("59m", 3540), ("2h 1m", 7260)])
def test_fixture_actual_not_goal_or_other_values(actual, seconds):
    plugin = ProcessorInstance.model_validate(plugin_dict())
    message = input_message(report(actual, threshold="8h 1m"))
    result = create().process(message, plugin.options)
    valid = engine.validate_result(result, DEFINITION, message)
    assert valid["records"][0]["distracting_seconds"] == seconds
    assert valid["records"][0]["period_start"] == "2026-10-25"
    assert valid["records"][0]["period_end_exclusive"] == "2026-11-01"
    assert len(valid["evidence"]) == 6


@pytest.mark.parametrize("kwargs", [
    {"html_actual": "4h 0m"}, {"qualifier": "Weekdays only"}, {"actual": "17:08"},
    {"actual": "1700h"}, {"actual": "-1h"}, {"actual": "1h 70m"},
    {"start": "October 26, 2026"}, {"start": "October 25"},
    {"extra": "Less than 1h on All Distracting Time\nAll day, every day\n3h\n1h avg per day"},
])
def test_unknown_ambiguous_or_impossible_reports_are_unsupported(kwargs):
    result = create().process(input_message(report(**kwargs)),
                              ProcessorInstance.model_validate(plugin_dict()).options)
    assert result["outcome"] == "unsupported"


@pytest.mark.parametrize("plain,html", [(True, False), (False, True)])
def test_single_mime_representation_supported(plain, html):
    result = create().process(input_message(report(plain=plain, html=html)),
                              ProcessorInstance.model_validate(plugin_dict()).options)
    assert result["records"][0]["distracting_seconds"] == 61680


@pytest.mark.parametrize("start,end", [("December 27, 2026", "January 3, 2027"),
                                      ("February 25, 2024", "March 3, 2024"),
                                      ("September 27, 2026", "October 4, 2026")])
def test_year_leap_and_dst_periods(start, end):
    result = create().process(input_message(report(start=start, end=end)),
                              ProcessorInstance.model_validate(plugin_dict()).options)
    assert result["outcome"] == "success"


def test_strict_mime_decoding_and_unknown_goal():
    message = input_message(report(plain=True, html=False))
    message["parts"][0]["diagnostics"] = ["decode failure"]
    assert create().process(message, {"metric": "total_distracting_time", "layout": "weekly-v1",
                                      "report_timezone": "Australia/Melbourne"})["outcome"] == "unsupported"
    raw = report().replace(b"All Distracting Time", b"Some Distracting Time")
    result = create().process(input_message(raw), ProcessorInstance.model_validate(plugin_dict()).options)
    assert result["outcome"] == "unsupported"


def test_match_requires_exact_sender_not_display_name():
    plugin = ProcessorInstance.model_validate(plugin_dict())
    assert engine.matches(report(), plugin)
    assert not engine.matches(report(sender="support@rescuetime.com.evil.invalid"), plugin)
    assert not engine.matches(report().replace(b"RescueTime Team", b"support@rescuetime.com").replace(
        b"<support@rescuetime.com>", b"<other@example.invalid>"), plugin)
    assert not engine.matches(b"From: support@rescuetime.com, other@example.invalid\nSubject: RescueTime Weekly Summary\n\n", plugin)


def test_config_validation_and_enrichment_fingerprint(config_factory):
    config = config_factory()
    active = enabled(config)
    assert config.enrichment_fingerprint() == active.enrichment_fingerprint()
    old = config.model_dump(mode="json", exclude={"env_file", "attachment_extraction", "processors"})
    assert config.enrichment_fingerprint() == digest(old)
    for mutation in ({"id": "../escape"}, {"options": {"report_timezone": "No/Such"}},
                     {"match": {"from_addresses": [], "subject_contains": ["weekly"]}},
                     {"implementation": "missing"}):
        with pytest.raises(ValidationError):
            ProcessorInstance.model_validate(plugin_dict() | mutation)


def test_real_worker_and_independent_archive_gates(config_factory):
    config = enabled(config_factory())
    provider = FakeProvider([report()])
    result = archive(config, provider)
    assert result[0]["outcome"] == "processed"
    assert result[0]["processors"][0]["outcome"] == "success"
    assert result[0]["processor_reports"][0]["outcome"] == "report_written"
    assert provider.processed == [1]
    assert verify(config).ok
    assert reports.verify_processors(config)["ok"]
    state = State(config.paths.database)
    plugin = config.processors.instances[0]
    data = reports.dataset(config, state, plugin)
    assert data["accepted"][0]["value"] == 61680
    root = reports.report_root(config, plugin)
    manifest, _ = reports.read_report(root, plugin)
    assert (root / manifest["generation"] / "history.png").read_bytes().startswith(b"\x89PNG")
    text = (root / "Index.md").read_text()
    assert "17 h 8 min" in text and "2026-11-01" in text
    csv = (root / manifest["generation"] / "history.csv").read_text()
    assert "61680" in csv and "Australia/Melbourne" in csv
    original = (root / "Index.md").read_bytes()
    second = engine.process_emails(config, plugin.id, execute=True)
    assert second[0]["outcome"] == "cached"
    assert second[-1]["outcome"] == "report_unchanged"
    assert (root / "Index.md").read_bytes() == original
    assert not list(config.processors.scratch_root.glob("worker-*"))


def test_dry_run_old_schema_no_mutation_or_worker(config_factory, monkeypatch):
    config = config_factory()
    archive(config, FakeProvider([report()]))
    config = enabled(config)
    state = State(config.paths.database)
    with state.connect() as conn:
        for table in ("processor_reports", "processor_resolutions", "processor_selections", "processor_attempts", "processor_results"):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("DELETE FROM schema_migrations WHERE version=3")
    before_db = config.paths.database.read_bytes()
    files = {p: p.read_bytes() for p in config.paths.archive_root.rglob("*") if p.is_file()}
    monkeypatch.setattr(engine, "run_worker", lambda *a: pytest.fail("dry-run ran plugin"))
    plan = engine.process_emails(config, "personal-rescuetime", execute=False)
    assert plan[0]["action"] == "process"
    assert config.paths.database.read_bytes() == before_db
    assert {p: p.read_bytes() for p in config.paths.archive_root.rglob("*") if p.is_file()} == files
    assert not config.processors.scratch_root.exists()


def test_duplicate_reports_and_conflict_resolution(config_factory):
    config = enabled(config_factory())
    raw = report()
    duplicate = raw.replace(b"reader@example.invalid", b"another@example.invalid")
    different = report("7h 1m")
    archive(config, FakeProvider([raw, duplicate]))
    state = State(config.paths.database)
    plugin = config.processors.instances[0]
    data = engine.observations(state, plugin)
    assert len(data["accepted"]) == 1 and len(data["accepted"][0]["sources"]) == 2
    archive(config, FakeProvider([raw, duplicate, different]))
    data = engine.observations(state, plugin)
    assert not data["accepted"] and len(data["conflicts"]) == 1
    chosen = data["conflicts"][0]["candidates"][0]["source"]["id"]
    result = engine.resolve(config, plugin.id, chosen, "Prefer the original report")
    assert result["conflicts"] == 0
    assert len(engine.observations(state, plugin)["accepted"]) == 1
    # A newly introduced disagreement invalidates the previous group resolution.
    archive(config, FakeProvider([raw, duplicate, different, report("2h 1m")]))
    assert len(engine.observations(state, plugin)["conflicts"]) == 1


def test_failed_upgrade_retains_stale_value_and_retry(config_factory, monkeypatch):
    config = enabled(config_factory())
    archive(config, FakeProvider([report()]))
    plugin = config.processors.instances[0]
    original_definition = plugin.plugin_definition()
    newer = original_definition.model_copy(update={"processor_version": "2"})
    monkeypatch.setattr(ProcessorInstance, "plugin_definition", lambda self: newer)
    monkeypatch.setattr(engine, "run_worker", lambda *args: {"outcome": "unsupported", "reason": "new layout"})
    result = engine.process_emails(config, plugin.id, execute=True)
    assert result[0]["outcome"] == "unsupported"
    data = engine.observations(State(config.paths.database), plugin)
    assert data["accepted"][0]["value"] == 61680 and data["stale_count"] == 1
    assert "previous accepted values" in (reports.report_root(config, plugin) / "Index.md").read_text()
    monkeypatch.setattr(engine, "run_worker", lambda *args: pytest.fail("failure retried without request"))
    engine.process_emails(config, plugin.id, execute=True)
    monkeypatch.setattr(engine, "run_worker", lambda config, plugin, message: create().process(message, plugin.options))
    result = engine.process_emails(config, plugin.id, execute=True, retry_failed=True)
    assert result[0]["outcome"] == "success"
    assert engine.observations(State(config.paths.database), plugin)["stale_count"] == 0


def test_archive_plugin_failure_warns_but_still_processes(config_factory, monkeypatch, capsys):
    config = enabled(config_factory())
    monkeypatch.setattr(engine, "run_worker", lambda *args: (_ for _ in ()).throw(TimeoutError("timeout")))
    provider = FakeProvider([report()])
    result = archive(config, provider)
    assert result[0]["outcome"] == "processed" and provider.processed == [1]
    assert result[0]["processors"][0]["outcome"] == "failed"
    assert "processor personal-rescuetime: failed" in capsys.readouterr().err
    assert verify(config).ok
    assert reports.verify_processors(config)["ok"]


def test_unsafe_worker_envelopes_and_evidence_rejected():
    message = input_message(report())
    result = create().process(message, ProcessorInstance.model_validate(plugin_dict()).options)
    result["records"][0]["distracting_seconds"] = True
    with pytest.raises(ValueError, match="integer"):
        engine.validate_result(result, DEFINITION, message)
    result["records"][0]["distracting_seconds"] = 61680
    result["evidence"][0]["text"] = "invented source text"
    with pytest.raises(ValueError, match="evidence"):
        engine.validate_result(result, DEFINITION, message)


def test_corrupt_graph_and_index_repair_separate_from_archive(config_factory):
    config = enabled(config_factory())
    archive(config, FakeProvider([report()]))
    plugin = config.processors.instances[0]
    root = reports.report_root(config, plugin)
    manifest, _ = reports.read_report(root, plugin)
    (root / manifest["generation"] / "history.png").write_bytes(b"damaged")
    assert not reports.verify_processors(config)["ok"]
    assert verify(config).ok
    assert reports.render_reports(config, plugin.id, execute=True)[0]["outcome"] == "report_written"
    assert reports.verify_processors(config)["ok"]
    # Reconcile an index published before a simulated DB crash.
    with State(config.paths.database).connect() as conn:
        conn.execute("UPDATE processor_reports SET generation='interrupted'")
    assert not reports.verify_processors(config)["ok"]
    assert reports.render_reports(config, plugin.id, execute=True)[0]["outcome"] == "report_unchanged"
    assert reports.verify_processors(config)["ok"]


def test_unowned_report_and_symlink_are_preserved(config_factory, tmp_path):
    config = enabled(config_factory())
    archive(config_factory(), FakeProvider([report()]))
    root = config.notes_dir / "Reports" / "personal-rescuetime"
    root.mkdir(parents=True)
    (root / "Index.md").write_text("my personal note")
    result = engine.process_emails(config, "personal-rescuetime", execute=True)
    assert result[-1]["outcome"] == "failed"
    assert (root / "Index.md").read_text() == "my personal note"
    with pytest.raises(ValueError):
        engine.safe_path(root, "../escape")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        engine.safe_path(root, "link/file")


def test_report_failure_keeps_previous_index(config_factory, monkeypatch):
    config = enabled(config_factory())
    raw = report()
    archive(config, FakeProvider([raw]))
    plugin = config.processors.instances[0]
    root = reports.report_root(config, plugin)
    before = (root / "Index.md").read_bytes()
    monkeypatch.setattr(reports, "plot_history", lambda *args: (_ for _ in ()).throw(OSError("disk unavailable")))
    result = archive(config, FakeProvider([raw, report("3h", start="November 1, 2026", end="November 8, 2026")]))
    assert all(r["outcome"] == "processed" for r in result)
    assert (root / "Index.md").read_bytes() == before
    assert not list(root.glob(".staging-*"))
    assert not reports.verify_processors(config)["ok"]


def test_custom_plugin_dry_run_never_imports_module(config_factory, monkeypatch):
    config = config_factory()
    archive(config, FakeProvider([report()]))
    values = config.model_dump()
    custom = plugin_dict() | {"implementation": "does_not_exist:create", "definition": DEFINITION.model_dump()}
    values["processors"] = {"enabled": True, "instances": [custom]}
    config = Config.model_validate(values)
    plan = engine.process_emails(config, custom["id"], execute=False)
    assert plan[0]["action"] == "process"
    # Missing module is a recorded worker failure, not an import in the host.
    result = engine.process_emails(config, custom["id"], execute=True)
    assert result[0]["outcome"] == "failed"


def test_plugin_cli_modes_and_local_dispatch(config_factory, tmp_path, monkeypatch, capsys):
    import yaml
    import mailkeep.cli as cli
    config = enabled(config_factory())
    archive(config, FakeProvider([report()]))
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    monkeypatch.setattr(cli, "connect", lambda *args: pytest.fail("local plugin command opened IMAP"))
    for command in (["processors", "list"], ["processors", "status"], ["processors", "verify"],
                    ["process-emails", "--processor", "personal-rescuetime", "--dry-run"],
                    ["render-reports", "--processor", "personal-rescuetime", "--execute"]):
        assert main(["--config", str(path), *command]) == 0
    assert main(["--config", str(path), "process-emails", "--processor", "unknown", "--dry-run"]) == 2
    with pytest.raises(SystemExit):
        parser().parse_args(["process-emails", "--processor", "p"])
    with pytest.raises(SystemExit):
        parser().parse_args(["process-emails", "--processor", "p", "--execute", "--dry-run"])


def test_custom_factory_loads_through_same_worker(config_factory):
    base = config_factory()
    archive(base, FakeProvider([report()]))
    values = enabled(base).model_dump()
    custom = plugin_dict() | {"implementation": "mailkeep.processors.rescuetime:create",
                              "definition": DEFINITION.model_dump()}
    values["processors"]["instances"] = [custom]
    config = Config.model_validate(values)
    result = engine.process_emails(config, custom["id"], execute=True)
    assert result[0]["outcome"] == "success"
    assert reports.verify_processors(config)["ok"]


def test_second_dataset_uses_generic_storage_and_renderer(config_factory, monkeypatch):
    base = config_factory()
    archive(base, FakeProvider([report()]))
    values = enabled(base).model_dump()
    definition = {
        "id": "example-energy", "processor_version": "1", "schema_version": "1", "options": {},
        "fields": {"start": {"type": "date"}, "end": {"type": "date"}, "kwh": {"type": "number"}},
        "view": {"start": "start", "end_exclusive": "end", "value": "kwh", "unit": "kWh",
                 "label": "Energy use", "metric": "weekly_kwh"},
    }
    custom = plugin_dict() | {"id": "energy", "implementation": "example_energy:create",
                              "options": {}, "definition": definition}
    values["processors"]["instances"] = [custom]
    config = Config.model_validate(values)
    monkeypatch.setattr(engine, "run_worker", lambda config, plugin, message: {
        "outcome": "success", "records": [{"start": "2026-10-25", "end": "2026-11-01", "kwh": 17.5}],
        "evidence": [{"part": 2, "field": "kwh", "text": "17h 8m"}],
    })
    result = engine.process_emails(config, "energy", execute=True)
    assert result[0]["outcome"] == "success"
    assert "17.5 kWh" in (config.notes_dir / "Reports/energy/Index.md").read_text()
    assert reports.verify_processors(config)["ok"]


def test_worker_timeout_and_size_limits(config_factory, monkeypatch):
    import subprocess
    config = enabled(config_factory())
    plugin = config.processors.instances[0]
    def timeout(*args, **kwargs):
        assert "MAILKEEP_GMAIL_PASSWORD" not in kwargs["env"]
        assert kwargs["timeout"] == 10
        assert kwargs["cwd"].is_relative_to(config.processors.scratch_root)
        raise subprocess.TimeoutExpired(args[0], 10)
    monkeypatch.setattr(engine.subprocess, "run", timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        engine.run_worker(config, plugin, input_message(report()))
    assert not list(config.processors.scratch_root.glob("worker-*"))
    with pytest.raises(ValueError, match="input exceeds"):
        engine.run_worker(config, plugin, {"oversized": "x" * (6 * 1024 * 1024)})


def test_result_and_selection_commit_atomically(config_factory):
    config = enabled(config_factory())
    archive(config_factory(), FakeProvider([report()]))
    state = State(config.paths.database)
    with state.connect() as conn:
        row = conn.execute("SELECT * FROM archives").fetchone()
        conn.execute("CREATE TRIGGER reject_selection BEFORE INSERT ON processor_selections "
                     "BEGIN SELECT RAISE(ABORT,'simulated interruption'); END")
    with pytest.raises(Exception, match="simulated interruption"):
        engine.process_one(config, state, config.processors.instances[0], row)
    with state.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM processor_results").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM processor_attempts").fetchone()[0] == 0


def test_processing_commands_respect_existing_lock(config_factory):
    from mailkeep.locking import data_lock
    config = enabled(config_factory())
    archive(config_factory(), FakeProvider([report()]))
    with data_lock(config.paths.archive_root):
        with pytest.raises(RuntimeError, match="another mutating"):
            engine.process_emails(config, "personal-rescuetime", execute=True)
        # Planning remains possible without taking a lock or writing anything.
        assert engine.process_emails(config, "personal-rescuetime", execute=False)


def test_changed_series_excludes_old_values_and_failures(config_factory):
    config = enabled(config_factory())
    archive(config, FakeProvider([report(), report("invalid")]))
    values = config.model_dump()
    values["processors"]["instances"][0]["series_id"] = "other-account"
    config = Config.model_validate(values)
    data = reports.dataset(config, State(config.paths.database), config.processors.instances[0])
    assert not data["accepted"] and not data["issues"]


def test_plot_does_not_connect_missing_periods(config_factory, monkeypatch):
    from matplotlib.figure import Figure
    captured = []
    monkeypatch.setattr(Figure, "savefig", lambda figure, *args, **kwargs:
                        captured.extend(len(line.get_xdata()) for line in figure.axes[0].lines))
    config = enabled(config_factory())
    data = {"accepted": [
        {"start": "2026-10-04", "end": "2026-10-11", "value": 3600},
        {"start": "2026-10-18", "end": "2026-10-25", "value": 7200},
        {"start": "2026-10-25", "end": "2026-11-01", "value": 3600},
    ]}
    reports.plot_history(config, config.processors.instances[0], data, config.paths.database.parent / "plot.png")
    assert captured == [1, 2]


def test_empty_report_uses_explanation_without_png(config_factory):
    config = enabled(config_factory())
    archive(config_factory(), FakeProvider([report(sender="other@example.invalid")]))
    result = engine.process_emails(config, "personal-rescuetime", execute=True)
    assert result[0]["outcome"] == "report_written"
    plugin = config.processors.instances[0]
    root = reports.report_root(config, plugin)
    manifest, _ = reports.read_report(root, plugin)
    assert set(manifest["files"]) == {"history.csv"}
    assert "No accepted observations" in (root / "Index.md").read_text()


def test_corrupt_source_is_detected_and_csv_formulas_neutralized(config_factory):
    config = enabled(config_factory())
    archive(config, FakeProvider([report()]))
    with State(config.paths.database).connect() as conn:
        row = conn.execute("SELECT * FROM archives").fetchone()
    (config.paths.archive_root / row["object_relpath"] / "original.eml").write_bytes(b"corrupt")
    assert not reports.verify_processors(config)["ok"]
    assert reports.csv_cell("  =SUM(A1)") == "'  =SUM(A1)"
    assert reports.csv_cell(-1) == -1
