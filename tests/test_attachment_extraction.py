from __future__ import annotations

import hashlib
import io
import subprocess
from email.message import EmailMessage
from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from mailkeep.activity import ACTIVITY_NAME
from mailkeep.attachments import _run_worker
from mailkeep.config import AttachmentExtractionConfig
from mailkeep.notes import PERSONAL_END, PERSONAL_START
from mailkeep.models import EnrichmentResult
from mailkeep.service import archive, extract_attachments, verify
from mailkeep.state import State

from test_service_verification import FakeProvider


def _pdf(*texts: str, rotate: bool = False, encrypted: bool = False) -> bytes:
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
            NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
        })
        font_ref = writer._add_object(font)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})
        })
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
        if rotate:
            page.rotate(90)
    if encrypted:
        writer.encrypt("secret")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _table_pdf() -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})
    })
    stream = DecodedStreamObject()
    stream.set_data(
        b"BT /F1 12 Tf 72 720 Td (Item) Tj 180 0 Td (Value) Tj "
        b"-180 -20 Td (Alpha) Tj 180 0 Td (42) Tj ET"
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _message(data: bytes, media_type: str, filename: str | None,
             *, message_id: str = "one@example.test", charset: str | None = None) -> bytes:
    message = EmailMessage()
    message["From"] = "Alice <alice@example.test>"
    message["To"] = "Bob <bob@example.test>"
    message["Subject"] = "Attachment"
    message["Message-ID"] = f"<{message_id}>"
    message["Date"] = "Tue, 01 Oct 2024 10:30:00 +1000"
    message.set_content("Body only for Jev.")
    maintype, subtype = media_type.split("/", 1)
    params = {"charset": charset} if charset else None
    message.add_attachment(data, maintype=maintype, subtype=subtype,
                           filename=filename, params=params)
    return message.as_bytes()


def _only_note(config) -> Path:
    with State(config.paths.database).connect() as conn:
        relative = conn.execute("SELECT note_relpath FROM archives").fetchone()[0]
    return config.notes_dir / relative


def test_real_pypdf_multi_page_rotated_unicode_and_misleading_name(config_factory):
    config = config_factory()
    pdf = _pdf("First page", "caf\N{LATIN SMALL LETTER E WITH ACUTE}", rotate=True)
    raw = _message(pdf, "application/octet-stream", "not-really.txt")
    original_hash = hashlib.sha256(pdf).hexdigest()

    result = archive(config, FakeProvider([raw]))

    assert result[0]["outcome"] == "processed"
    with State(config.paths.database).connect() as conn:
        attachment = conn.execute("SELECT * FROM attachments").fetchone()
        extraction = conn.execute("SELECT * FROM attachment_extraction_results").fetchone()
    original = (config.paths.archive_root / conn_path(config) / "attachments" /
                attachment["stored_name"]).read_bytes()
    assert hashlib.sha256(original).hexdigest() == original_hash
    assert extraction["outcome"] == "success"
    assert extraction["parser"] == "pypdf-layout"
    assert extraction["page_count"] == 2
    note = _only_note(config).read_text(encoding="utf-8")
    assert "## Attachments" in note
    assert "First page" in note and "caf\N{LATIN SMALL LETTER E WITH ACUTE}" in note
    assert "#### Page 1" in note and "#### Page 2" in note
    assert verify(config).ok


def test_pdf_layout_columns_are_preserved_in_derived_text_and_note(config_factory):
    config = config_factory()
    raw = _message(_table_pdf(), "application/pdf", "table.pdf")

    archive(config, FakeProvider([raw]))

    with State(config.paths.database).connect() as conn:
        result = conn.execute("SELECT * FROM attachment_extraction_results").fetchone()
    text = (config.paths.archive_root / result["artifact_relpath"]).read_text(encoding="utf-8")
    assert "Item" in text and "Value" in text
    assert "Item  " in text
    note = _only_note(config).read_text(encoding="utf-8")
    assert "~~~~text" in note
    assert "Item  " in note


def conn_path(config) -> Path:
    with State(config.paths.database).connect() as conn:
        return Path(conn.execute("SELECT object_relpath FROM archives").fetchone()[0])


def test_plain_text_declared_charset_and_marker_escaping(config_factory):
    config = config_factory()
    text = "caf\N{LATIN SMALL LETTER E WITH ACUTE}\n<!-- mailkeep:generated:end -->\n"
    raw = _message(text.encode("latin-1"), "text/plain", "note.txt", charset="iso-8859-1")

    archive(config, FakeProvider([raw]))

    note = _only_note(config).read_text(encoding="utf-8")
    assert "caf\N{LATIN SMALL LETTER E WITH ACUTE}" in note
    assert "&lt;!-- mailkeep:generated:end -->" in note
    assert note.count("<!-- mailkeep:generated:end -->") == 1
    assert verify(config).ok


def test_duplicate_attachment_reuses_cache_across_messages(config_factory):
    config = config_factory()
    payload = _pdf("Shared")
    messages = [
        _message(payload, "application/pdf", "a.pdf", message_id="a@example.test"),
        _message(payload, "application/pdf", "b.pdf", message_id="b@example.test"),
    ]

    archive(config, FakeProvider(messages))

    with State(config.paths.database).connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM attachment_extraction_results").fetchone()[0] == 1
        attempts = conn.execute(
            "SELECT cache_reused FROM attachment_extraction_attempts ORDER BY id"
        ).fetchall()
    assert [row[0] for row in attempts] == [0, 1]


def test_extraction_failure_is_nonfatal_and_visible(config_factory, monkeypatch):
    config = config_factory()
    raw = _message(_pdf("Text"), "application/pdf", "file.pdf")
    monkeypatch.setattr("mailkeep.attachments._run_worker", lambda *args, **kwargs: {
        "outcome": "timeout", "error": "test timeout",
    })

    result = archive(config, FakeProvider([raw]))

    assert result[0]["outcome"] == "processed"
    note = _only_note(config).read_text(encoding="utf-8")
    assert "Extraction status: `timeout`: test timeout" in note
    report = verify(config)
    assert report.ok
    assert any("extraction timeout" in warning for warning in report.warnings)


def test_disabled_then_backfill_preserves_personal_region(config_factory):
    enabled = config_factory()
    disabled = enabled.model_copy(update={
        "attachment_extraction": enabled.attachment_extraction.model_copy(update={"enabled": False})
    })
    raw = _message(_pdf("Backfilled"), "application/pdf", "file.pdf")
    archive(disabled, FakeProvider([raw]))
    note_path = _only_note(enabled)
    note = note_path.read_text(encoding="utf-8")
    note_path.write_text(
        note.replace(PERSONAL_START + "\n" + PERSONAL_END,
                     PERSONAL_START + "\nPrivate words.\n" + PERSONAL_END),
        encoding="utf-8",
    )

    dry_run = extract_attachments(enabled, execute=False)
    assert dry_run[0]["action"] == "extract"
    executed = extract_attachments(enabled, execute=True)

    assert executed[0]["outcome"] == "complete"
    regenerated = note_path.read_text(encoding="utf-8")
    assert "Backfilled" in regenerated
    assert "Private words." in regenerated
    assert verify(enabled).ok


def test_retry_failed_and_missing_success_artifact_verification(config_factory, monkeypatch):
    config = config_factory()
    raw = _message(_pdf("Retry me"), "application/pdf", "file.pdf")
    real_worker = _run_worker
    monkeypatch.setattr("mailkeep.attachments._run_worker", lambda *args, **kwargs: {
        "outcome": "crashed", "error": "test crash",
    })
    archive(config, FakeProvider([raw]))
    monkeypatch.setattr("mailkeep.attachments._run_worker", real_worker)
    with State(config.paths.database).connect() as conn:
        archive_id = conn.execute("SELECT archive_id FROM archives").fetchone()[0]

    extract_attachments(config, execute=True, archive_id=archive_id, retry_failed=True)
    assert verify(config).ok
    extraction = State(config.paths.database).latest_extractions(archive_id)
    artifact = config.paths.archive_root / next(iter(extraction.values())).artifact_relpath
    artifact.unlink()

    report = verify(config)
    assert not report.ok
    assert any("invalid derived extraction" in error for error in report.errors)

    repaired = extract_attachments(config, execute=True, archive_id=archive_id)
    assert repaired[0]["attachments"][0]["action"] == "repair_cache"
    assert verify(config).ok


def test_encrypted_blank_malformed_oversized_and_unsupported_are_explicit(config_factory):
    config = config_factory()
    config = config.model_copy(update={
        "attachment_extraction": AttachmentExtractionConfig(max_pdf_bytes=1500)
    })
    messages = [
        _message(_pdf("secret", encrypted=True), "application/pdf", "encrypted.pdf",
                 message_id="encrypted@example.test"),
        _message(_pdf(""), "application/pdf", "blank.pdf", message_id="blank@example.test"),
        _message(b"%PDF-not valid", "application/pdf", "bad.pdf", message_id="bad@example.test"),
        _message(_pdf("large") + b"x" * 2000, "application/pdf", "large.pdf",
                 message_id="large@example.test"),
        _message(b"png", "image/png", None, message_id="image@example.test"),
    ]

    results = archive(config, FakeProvider(messages))

    assert all(item["outcome"] == "processed" for item in results)
    with State(config.paths.database).connect() as conn:
        outcomes = {row[0] for row in conn.execute(
            "SELECT outcome FROM attachment_extraction_attempts"
        )}
    assert {"encrypted", "blank", "malformed", "oversized", "unsupported"} <= outcomes
    assert all("## Attachments" in path.read_text(encoding="utf-8")
               for path in config.notes_dir.rglob("*.md")
               if path.name != ACTIVITY_NAME)


def test_note_and_output_limits_are_bounded(config_factory):
    config = config_factory()
    config = config.model_copy(update={
        "attachment_extraction": AttachmentExtractionConfig(
            max_output_bytes=80, note_chars_per_attachment=20,
            note_chars_per_message=20,
        )
    })
    raw = _message(("0123456789" * 30).encode(), "text/plain", "long.txt")

    archive(config, FakeProvider([raw]))

    with State(config.paths.database).connect() as conn:
        result = conn.execute("SELECT * FROM attachment_extraction_results").fetchone()
    assert result["output_size"] <= 80
    assert "output_truncated" in result["warnings_json"]
    note = _only_note(config).read_text(encoding="utf-8")
    assert "open the complete extracted text" in note


def test_page_and_supported_attachment_count_limits_are_nonfatal(config_factory):
    config = config_factory()
    config = config.model_copy(update={
        "attachment_extraction": AttachmentExtractionConfig(
            max_pdf_pages=1, max_supported_attachments_per_message=1
        )
    })
    message = EmailMessage()
    message["From"] = "a@example.test"
    message["To"] = "b@example.test"
    message["Subject"] = "Limits"
    message.set_content("body")
    message.add_attachment(_pdf("one", "two"), maintype="application", subtype="pdf",
                           filename="pages.pdf")
    message.add_attachment(_pdf("second"), maintype="application", subtype="pdf",
                           filename="second.pdf")

    result = archive(config, FakeProvider([message.as_bytes()]))

    assert result[0]["outcome"] == "processed"
    with State(config.paths.database).connect() as conn:
        outcomes = [row[0] for row in conn.execute(
            "SELECT outcome FROM attachment_extraction_attempts ORDER BY id"
        )]
    assert outcomes == ["oversized", "limit_exceeded"]


def test_worker_timeout_crash_and_resource_signal_are_classified(config_factory, monkeypatch):
    config = config_factory()
    path = config.paths.obsidian_vault / "input.txt"
    path.write_text("text", encoding="utf-8")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr("mailkeep.attachments.subprocess.run", timeout)
    assert _run_worker(config, path, "text", None, 0.1)["outcome"] == "timeout"
    monkeypatch.setattr("mailkeep.attachments.subprocess.run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 3, "", "boom"))
    assert _run_worker(config, path, "text", None, 1)["outcome"] == "crashed"
    monkeypatch.setattr("mailkeep.attachments.subprocess.run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], -9, "", ""))
    assert _run_worker(config, path, "text", None, 1)["outcome"] == "resource_limited"


def test_attachment_text_is_not_sent_to_enrichment_or_its_cache_key(config_factory, monkeypatch):
    first = config_factory(enrichment="ollama")
    raw = _message(b"SECRET ATTACHMENT", "text/plain", "secret.txt")
    calls: list[str] = []

    def fake_enrich(config, text):
        calls.append(text)
        return EnrichmentResult(
            provider="ollama", model="test-model",
            input_hash=hashlib.sha256(text.encode()).hexdigest(),
            config_hash=config.fingerprint(),
        )

    monkeypatch.setattr("mailkeep.service.enrich", fake_enrich)
    archive(first, FakeProvider([raw]))
    second = first.model_copy(update={
        "attachment_extraction": first.attachment_extraction.model_copy(
            update={"max_text_bytes": 1024}
        )
    })
    archive(second, FakeProvider([raw]))

    assert calls == ["Body only for Jev."]


def test_parser_version_change_creates_new_cached_result(config_factory, monkeypatch):
    config = config_factory()
    raw = _message(_pdf("Versioned"), "application/pdf", "file.pdf")
    archive(config, FakeProvider([raw]))
    monkeypatch.setattr("mailkeep.attachments.pypdf.__version__", "6.19.test-upgrade")

    plan = extract_attachments(config, execute=False)
    assert plan[0]["action"] == "extract"
    extract_attachments(config, execute=True)

    with State(config.paths.database).connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM attachment_extraction_results").fetchone()[0] == 2


def test_extraction_fingerprint_does_not_change_enrichment_fingerprint(config_factory):
    first = config_factory(enrichment="ollama")
    second = first.model_copy(update={
        "attachment_extraction": first.attachment_extraction.model_copy(
            update={"max_pdf_pages": 2}
        )
    })
    assert first.enrichment_fingerprint() == second.enrichment_fingerprint()
    assert first.attachment_extraction.fingerprint() != second.attachment_extraction.fingerprint()


def test_backfill_dry_run_does_not_apply_migration(config_factory):
    config = config_factory()
    state = State(config.paths.database)
    state.initialize()
    with state.transaction() as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version=2")
        conn.execute("DROP TABLE attachment_extraction_attempts")
        conn.execute("DROP TABLE attachment_extraction_results")
    # SQLite cannot drop the added column, which is harmless: the assertion is
    # that dry-run does not apply or parse anything.
    before = config.paths.database.read_bytes()

    result = extract_attachments(config, execute=False)

    assert result == []
    assert config.paths.database.read_bytes() == before
