from __future__ import annotations

from datetime import datetime

from mailkeep.activity import ACTIVITY_HEADER, ACTIVITY_NAME, append_activity
from mailkeep.service import archive, prune
from test_service_verification import FakeProvider


class NoTrashProvider(FakeProvider):
    def find_gmail(self, gmail_msgid, mailbox):
        if "Trash" in mailbox:
            return []
        return super().find_gmail(gmail_msgid, mailbox)


def _activity(config) -> str:
    return (config.notes_dir / ACTIVITY_NAME).read_text(encoding="utf-8")


def test_first_archive_creates_dated_one_line_entry(config_factory, plain_raw):
    config = config_factory()
    archive(config, FakeProvider([plain_raw]))

    text = _activity(config)
    assert text.startswith(f"{ACTIVITY_HEADER}\n\n## {datetime.now().year}\n\n")
    assert f"- {datetime.now():%Y-%m-%d}: archived 1; 1 total.\n" in text


def test_archive_entry_counts_deduplicated_messages(config_factory, plain_raw):
    config = config_factory()
    result = archive(config, FakeProvider([plain_raw, plain_raw]))

    assert [item["deduplicated"] for item in result] == [False, True]
    assert "archived 2 (1 deduplicated); 1 total." in _activity(config)


def test_failed_archive_is_recorded(config_factory, plain_raw, monkeypatch):
    config = config_factory(enrichment="ollama")
    monkeypatch.setattr(
        "mailkeep.service.enrich",
        lambda config, text: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    archive(config, FakeProvider([plain_raw]))

    assert "archived 0, 1 failed; 1 total." in _activity(config)


def test_idle_archive_writes_nothing(config_factory):
    config = config_factory()
    assert archive(config, FakeProvider([])) == []
    assert not (config.notes_dir / ACTIVITY_NAME).exists()


def test_prune_execute_records_trash_and_absent_actions(config_factory, plain_raw):
    config = config_factory(deletion_enabled=True, retention_days=0)
    provider = NoTrashProvider([plain_raw])
    archive(config, provider)
    prune(config, provider, execute=True)
    assert "moved 1 to Gmail Trash." in _activity(config)

    prune(config, provider, execute=True)
    assert "recorded 1 absent." in _activity(config)


def test_prune_execute_records_blocked_due_message(config_factory, plain_raw):
    config = config_factory(deletion_enabled=False, retention_days=0)
    provider = FakeProvider([plain_raw])
    archive(config, provider)

    prune(config, provider, execute=True)

    assert "1 blocked by safety gates." in _activity(config)


def test_prune_dry_run_never_writes(config_factory, plain_raw):
    config = config_factory(deletion_enabled=True, retention_days=0)
    provider = FakeProvider([plain_raw])
    archive(config, provider)
    before = _activity(config)

    prune(config, provider, execute=False)

    assert _activity(config) == before


def test_append_activity_groups_years_and_preserves_content(config_factory):
    config = config_factory()
    path = config.notes_dir / ACTIVITY_NAME
    path.parent.mkdir(parents=True)
    path.write_text(
        f"{ACTIVITY_HEADER}\n\n## 2024\n\n- 2024-01-02: archived 1; 1 total.\n\n"
        "Operator note.\n",
        encoding="utf-8",
    )

    append_activity(config, "archived 2; 3 total", now=datetime(2026, 10, 13, 9, 0))
    append_activity(config, "moved 1 to Gmail Trash", now=datetime(2026, 10, 14, 9, 0))
    append_activity(config, "archived 1; 4 total", now=datetime(2027, 1, 1, 9, 0))

    text = _activity(config)
    assert "## 2024\n\n- 2024-01-02: archived 1; 1 total.\n\nOperator note." in text
    assert ("## 2026\n\n- 2026-10-13: archived 2; 3 total.\n"
            "- 2026-10-14: moved 1 to Gmail Trash.\n") in text
    assert "## 2027\n\n- 2027-01-01: archived 1; 4 total.\n" in text


def test_append_activity_leaves_no_temporary_files(config_factory):
    config = config_factory()
    append_activity(config, "archived 1; 1 total", now=datetime(2026, 10, 13))

    assert [path.name for path in config.notes_dir.iterdir()] == [ACTIVITY_NAME]
