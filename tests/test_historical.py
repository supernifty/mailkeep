from pathlib import Path

import pytest

from mailkeep.processors import reports
from mailkeep.processors.historical import END, START, normalized_end, parse_note
from mailkeep.service import archive, verify
from mailkeep.state import State
from test_processors import enabled, report
from test_service_verification import FakeProvider


def note(rows):
    return START + "\n| Supplied date | Week end (exclusive) | Distracting hours |\n| --- | --- | --- |\n" + "".join(
        f"| {date} | {normalized_end(date).isoformat()} | {value} |\n" for date, value in rows) + END + "\n"


@pytest.mark.parametrize("original,expected", [("31-10-26", "2026-11-01"), ("1-Nov-2026", "2026-11-01"),
                                             ("2026-11-01", "2026-11-01"), ("12-Jan-2025", "2025-01-12")])
def test_date_normalization(original, expected):
    assert normalized_end(original).isoformat() == expected


@pytest.mark.parametrize("value", ["NaN", "-1", "1e2", "170.0", "1.0001", "=1+1"])
def test_bad_hours_rejected(value):
    with pytest.raises(ValueError):
        parse_note(note([("1-Nov-2026", value)]), "Australia/Melbourne")


def test_bad_dates_duplicates_and_markers():
    with pytest.raises(ValueError):
        normalized_end("2-Nov-2026")
    with pytest.raises(ValueError):
        parse_note(note([("31-10-26", "1.0"), ("1-Nov-2026", "2.0")]), "Australia/Melbourne")
    with pytest.raises(ValueError):
        parse_note("unmarked table", "Australia/Melbourne")
    with pytest.raises(ValueError):
        parse_note(note([("31-10-26", "1.0")]).replace("2026-11-01", "2026-10-31"), "Australia/Melbourne")


def test_historical_only_decimal_precision_and_no_fake_archives(config_factory):
    config = enabled(config_factory())
    # An unrelated archive creates the database; historical values stay in the vault.
    archive(config, FakeProvider([report(sender="other@example.invalid")]))
    state = State(config.paths.database)
    plugin = config.processors.instances[0]
    root = reports.report_root(config, plugin)
    root.mkdir(parents=True, exist_ok=True)
    source = root / "Historical data.md"
    source.write_text(note([("12-Jan-2025", "16.0"), ("31-10-26", "17.1")]))
    source_before = source.read_bytes()
    result = reports.render_reports(config, plugin.id, execute=True)
    assert result[0]["outcome"] == "report_written"
    data = reports.dataset(config, state, plugin)
    assert len(data["accepted"]) == 2
    assert data["accepted"][-1]["record"]["precision_seconds"] == 360
    assert "17.1 h" in (root / "Index.md").read_text()
    with state.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM archives").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM processor_results").fetchone()[0] == 0
    assert source.read_bytes() == source_before
    assert reports.verify_processors(config)["ok"]


def test_precise_email_preferred_once_and_future_mail_retains_history(config_factory):
    config = enabled(config_factory())
    raw = report()
    archive(config, FakeProvider([raw]))
    plugin = config.processors.instances[0]
    root = reports.report_root(config, plugin)
    source = root / "Historical data.md"
    source.write_text(note([("12-Jan-2025", "16.0"), ("31-10-26", "17.1")]))
    before = source.read_bytes()
    reports.render_reports(config, plugin.id, execute=True)
    data = reports.dataset(config, State(config.paths.database), plugin)
    assert len(data["accepted"]) == 2
    assert data["accepted"][-1]["value"] == 61680
    assert len(data["accepted"][-1]["sources"]) == 2
    assert not data["historical_warnings"]
    assert reports.render_reports(config, plugin.id, execute=True)[0]["outcome"] == "report_unchanged"
    archive(config, FakeProvider([raw, report("12h", start="November 1, 2026", end="November 8, 2026")]))
    data = reports.dataset(config, State(config.paths.database), plugin)
    assert len(data["accepted"]) == 3
    assert source.read_bytes() == before
    assert reports.verify_processors(config)["ok"]
    manifest, _ = reports.read_report(root, plugin)
    csv = (root / manifest["generation"] / "history.csv").read_text()
    assert "historical_supplied_date" in csv and "31-10-26,17.1" in csv
    assert verify(config).ok


def test_reconciliation_warnings_and_source_edit_detected(config_factory):
    config = enabled(config_factory())
    archive(config, FakeProvider([report()]))
    plugin = config.processors.instances[0]
    root = reports.report_root(config, plugin)
    source = root / "Historical data.md"
    source.write_text(note([("31-10-26", "16.0")]))
    reports.render_reports(config, plugin.id, execute=True)
    data = reports.dataset(config, State(config.paths.database), plugin)
    assert data["accepted"][0]["value"] == 61680 and data["historical_warnings"]
    assert reports.verify_processors(config)["ok"]
    source.write_text(note([("31-10-26", "17.1")]))
    assert not reports.verify_processors(config)["ok"]
    reports.render_reports(config, plugin.id, execute=True)
    assert reports.verify_processors(config)["ok"]
    before = (root / "Index.md").read_bytes()
    source.write_text("invalid table")
    result = reports.render_reports(config, plugin.id, execute=True)
    assert result[0]["outcome"] == "failed"
    assert (root / "Index.md").read_bytes() == before


def test_history_does_not_hide_email_conflicts(config_factory):
    config = enabled(config_factory())
    archive(config, FakeProvider([report(), report("12h")]))
    plugin = config.processors.instances[0]
    root = reports.report_root(config, plugin)
    (root / "Historical data.md").write_text(note([("31-10-26", "17.1")]))
    reports.render_reports(config, plugin.id, execute=True)
    data = reports.dataset(config, State(config.paths.database), plugin)
    assert not data["accepted"] and data["conflicts"] and data["historical_warnings"]
