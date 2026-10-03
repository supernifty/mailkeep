from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from mailkeep.models import RemoteMessage
from mailkeep.models import EnrichmentResult
from mailkeep.service import archive, dry_run_archive, organize_notes, prune, verify
from mailkeep.state import State


@dataclass
class FakeProvider:
    raw_messages: list[bytes]
    processed: list[int] = field(default_factory=list)
    trashed: list[int] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    def discover(self, limit=None):
        self.calls.append("discover")
        messages = [RemoteMessage("test-account", "Mailkeep", 10, index + 1,
                                  str(1000 + index), len(raw))
                    for index, raw in enumerate(self.raw_messages)]
        return messages[:limit]

    def fetch(self, message):
        self.calls.append(f"fetch:{message.uid}")
        return self.raw_messages[message.uid - 1]

    def mark_processed(self, message):
        self.calls.append(f"processed:{message.uid}")
        self.processed.append(message.uid)

    def find_gmail(self, gmail_msgid, mailbox):
        index = int(gmail_msgid) - 1000
        self.calls.append(f"find:{gmail_msgid}:{mailbox}")
        return [RemoteMessage("test-account", "Mailkeep-Processed", 11, index + 1,
                              gmail_msgid, len(self.raw_messages[index]))]

    def move_to_trash(self, message):
        self.calls.append(f"trash:{message.uid}")
        self.trashed.append(message.uid)

    def trash_mailbox(self):
        return "[Gmail]/Trash"


def test_dry_run_has_no_local_or_remote_mutation(config_factory, plain_raw):
    config = config_factory()
    provider = FakeProvider([plain_raw])
    result = dry_run_archive(config, provider, None, False)
    assert result[0]["raw_sha256"] == hashlib.sha256(plain_raw).hexdigest()
    assert not config.paths.database.exists()
    assert not config.paths.archive_root.exists()
    assert provider.processed == []


def test_full_archive_reaches_verified_before_label_change(config_factory, plain_raw):
    config = config_factory()
    provider = FakeProvider([plain_raw])
    result = archive(config, provider)
    assert result[0]["outcome"] == "processed"
    assert provider.processed == [1]
    state = State(config.paths.database)
    with state.connect() as conn:
        row = conn.execute("SELECT * FROM archives").fetchone()
        remote = conn.execute("SELECT * FROM remote_messages").fetchone()
    assert row["preservation_state"] == "preservation_verified"
    assert row["enrichment_state"] == "skipped"
    assert row["markdown_state"] == "complete"
    assert Path(row["note_relpath"]).parent == Path("2024/10")
    assert (config.notes_dir / row["note_relpath"]).is_file()
    assert remote["provider_state"] == "processed"
    assert verify(config).ok


def test_organize_notes_moves_legacy_note_without_changing_bytes(config_factory, plain_raw):
    config = config_factory()
    archive(config, FakeProvider([plain_raw]))
    state = State(config.paths.database)
    with state.connect() as conn:
        row = conn.execute("SELECT archive_id,note_relpath FROM archives").fetchone()
    organized = config.notes_dir / row["note_relpath"]
    legacy_name = organized.name
    legacy = config.notes_dir / legacy_name
    original = organized.read_bytes()
    organized.replace(legacy)
    organized.parent.rmdir()
    organized.parent.parent.rmdir()
    with state.transaction() as conn:
        conn.execute("UPDATE archives SET note_relpath=? WHERE archive_id=?",
                     (legacy_name, row["archive_id"]))

    plan = organize_notes(config, execute=False)
    assert plan[0]["action"] == "planned"
    assert legacy.read_bytes() == original

    result = organize_notes(config, execute=True)

    assert result[0]["action"] == "moved"
    assert organized.read_bytes() == original
    assert not legacy.exists()
    with state.connect() as conn:
        assert conn.execute("SELECT note_relpath FROM archives").fetchone()[0] == row["note_relpath"]
    assert verify(config).ok


def test_organize_notes_refuses_destination_collision(config_factory, plain_raw):
    config = config_factory()
    archive(config, FakeProvider([plain_raw]))
    state = State(config.paths.database)
    with state.connect() as conn:
        row = conn.execute("SELECT archive_id,note_relpath FROM archives").fetchone()
    organized = config.notes_dir / row["note_relpath"]
    legacy = config.notes_dir / organized.name
    legacy.write_bytes(organized.read_bytes())
    with state.transaction() as conn:
        conn.execute("UPDATE archives SET note_relpath=? WHERE archive_id=?",
                     (legacy.name, row["archive_id"]))

    try:
        organize_notes(config, execute=True)
    except RuntimeError as exc:
        assert "destination already exists" in str(exc)
    else:
        raise AssertionError("note collision was accepted")
    assert legacy.is_file()
    assert organized.is_file()


def test_enrichment_failure_never_moves_remote_label(config_factory, plain_raw, monkeypatch):
    config = config_factory(enrichment="ollama")
    provider = FakeProvider([plain_raw])
    monkeypatch.setattr("mailkeep.service.enrich", lambda config, text: (_ for _ in ()).throw(RuntimeError("offline")))
    result = archive(config, provider)
    assert result[0]["outcome"] == "failed"
    assert provider.processed == []
    with State(config.paths.database).connect() as conn:
        row = conn.execute("SELECT preservation_state,enrichment_state FROM archives").fetchone()
        failed = conn.execute("SELECT stage,outcome FROM attempts WHERE outcome='failed'").fetchone()
        assert row[0] == "preservation_verified"
        assert row[1] == "failed"
        assert tuple(failed) == ("enrichment", "failed")


def test_message_id_collision_records_diagnostic(config_factory, plain_raw):
    second = plain_raw.replace(b"Hello Bob", b"Hello Sue")
    config = config_factory()
    provider = FakeProvider([plain_raw, second])
    result = archive(config, provider)
    assert [item["outcome"] for item in result] == ["processed", "processed"]
    with State(config.paths.database).connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM diagnostics WHERE code='message_id_collision'").fetchone()[0]
    assert count == 1


def test_verifier_detects_tampered_eml(config_factory, plain_raw):
    config = config_factory()
    provider = FakeProvider([plain_raw])
    archive(config, provider)
    with State(config.paths.database).connect() as conn:
        rel = conn.execute("SELECT object_relpath FROM archives").fetchone()[0]
    (config.paths.archive_root / rel / "original.eml").write_bytes(b"tampered")
    report = verify(config)
    assert not report.ok
    assert any("raw SHA-256 mismatch" in item for item in report.errors)


def test_verifier_detects_missing_attachment(config_factory):
    raw = (b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=x\r\n\r\n"
           b"--x\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
           b"--x\r\nContent-Type: application/octet-stream\r\n"
           b"Content-Disposition: attachment; filename=a.bin\r\n\r\ndata\r\n--x--\r\n")
    config = config_factory()
    archive(config, FakeProvider([raw]))
    with State(config.paths.database).connect() as conn:
        rel = conn.execute("SELECT object_relpath FROM archives").fetchone()[0]
        name = conn.execute("SELECT stored_name FROM attachments").fetchone()[0]
    (config.paths.archive_root / rel / "attachments" / name).unlink()
    assert any("missing attachment" in item for item in verify(config).errors)


def test_prune_rechecks_hash_and_moves_only_when_all_gates_pass(config_factory, plain_raw):
    config = config_factory(deletion_enabled=True, retention_days=0)
    provider = FakeProvider([plain_raw])
    archive(config, provider)
    result = prune(config, provider, execute=True)
    assert result[0]["eligible"]
    assert result[0]["action"] == "moved_to_trash"
    assert provider.trashed == [1]


def test_prune_hash_mismatch_blocks_trash(config_factory, plain_raw):
    config = config_factory(deletion_enabled=True, retention_days=0)
    provider = FakeProvider([plain_raw])
    archive(config, provider)
    provider.raw_messages[0] = b"different remote bytes"
    result = prune(config, provider, execute=True)
    assert not result[0]["gates"]["remote_hash_match"]
    assert provider.trashed == []


def test_prune_dry_run_never_moves(config_factory, plain_raw):
    config = config_factory(deletion_enabled=True, retention_days=0)
    provider = FakeProvider([plain_raw])
    archive(config, provider)
    prune(config, provider, execute=False)
    assert provider.trashed == []


def test_prune_reports_retention_gate_before_boundary(config_factory, plain_raw):
    config = config_factory(deletion_enabled=True, retention_days=10)
    provider = FakeProvider([plain_raw])
    archive(config, provider)
    result = prune(config, provider, execute=False)
    assert not result[0]["gates"]["retention_elapsed"]
    assert not result[0]["eligible"]


def test_enrichment_cache_reuses_same_configuration(config_factory, plain_raw, monkeypatch):
    config = config_factory(enrichment="ollama")
    provider = FakeProvider([plain_raw])
    calls = []

    def fake_enrich(config, text):
        calls.append(config.fingerprint())
        return EnrichmentResult(provider="ollama", model="exact-build",
                                input_hash=hashlib.sha256(text.encode()).hexdigest(),
                                config_hash=config.fingerprint())

    monkeypatch.setattr("mailkeep.service.enrich", fake_enrich)
    assert archive(config, provider)[0]["outcome"] == "processed"
    assert archive(config, provider)[0]["outcome"] == "processed"
    assert len(calls) == 1


def test_uidvalidity_change_updates_stable_gmail_occurrence(config_factory):
    config = config_factory()
    state = State(config.paths.database)
    state.initialize()
    state.ensure_account("test-account", "gmail", "test@example.test")
    first = state.discover(RemoteMessage("test-account", "Mailkeep", 10, 1, "stable"))
    second = state.discover(RemoteMessage("test-account", "Mailkeep", 11, 99, "stable"))
    assert first == second
    with state.connect() as conn:
        row = conn.execute("SELECT uidvalidity,uid FROM remote_messages").fetchone()
    assert tuple(row) == (11, 99)
