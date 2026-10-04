from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from mailkeep.enrichment import no_enrichment
from mailkeep.mime import parse_message
from mailkeep.models import EnrichmentResult
from mailkeep.notes import (
    PERSONAL_END,
    PERSONAL_START,
    build_note,
    note_relative_path,
    parse_managed,
    verify_managed,
)
from mailkeep.storage import identity, reconcile_staging, store


def test_exact_bytes_are_deduplicated(tmp_path: Path, plain_raw):
    parsed = parse_message(plain_raw)
    first = store(tmp_path, plain_raw, parsed)
    second = store(tmp_path, plain_raw, parsed)
    assert first.archive_id == second.archive_id
    assert not first.deduplicated and second.deduplicated
    assert first.eml_path.read_bytes() == plain_raw


def test_identity_changes_for_different_bytes_with_same_message_id(plain_raw):
    changed = plain_raw.replace(b"Hello Bob", b"Hello Alice")
    assert identity(plain_raw) != identity(changed)


def test_note_relative_path_uses_message_year_and_month(plain_raw):
    parsed = parse_message(plain_raw)

    path = note_relative_path(parsed, "12345678-0000-0000-0000-000000000000")

    assert path.parent == Path("2024/10")
    assert path.name.startswith("2024-10-01 - ")


def test_note_relative_path_uses_undated_folder():
    parsed = parse_message(b"Subject: Bare\r\n\r\nbody")

    path = note_relative_path(parsed, "12345678-0000-0000-0000-000000000000")

    assert path.parent == Path("undated")
    assert path.name.startswith("undated - ")


def test_staging_reconciliation_only_removes_strict_names(tmp_path: Path):
    parent = tmp_path / "objects/aa/bb"
    parent.mkdir(parents=True)
    valid = parent / ".123e4567-e89b-12d3-a456-426614174000.staging-abcdef123456"
    invalid = parent / ".important.staging-stuff"
    valid.mkdir()
    invalid.mkdir()
    assert reconcile_staging(tmp_path) == [valid]
    assert invalid.exists()


def test_note_has_encoded_file_links_and_preserves_personal_notes(config_factory, plain_raw):
    config = config_factory()
    parsed = parse_message(plain_raw)
    stored = store(config.paths.archive_root, plain_raw, parsed)
    result = no_enrichment(config, parsed.body_markdown)
    first, digest = build_note(parsed, stored, config.account.account_id,
                               config.paths.archive_root, result)
    assert "%20" in first or "file://" in first
    annotated = first.replace(PERSONAL_START + "\n" + PERSONAL_END,
                              PERSONAL_START + "\nMy private note.\n" + PERSONAL_END)
    second, _ = build_note(parsed, stored, config.account.account_id,
                           config.paths.archive_root, result, existing=annotated)
    assert "My private note." in second
    assert verify_managed(second)[0]


def test_personal_note_edits_do_not_break_managed_hash(config_factory, plain_raw):
    config = config_factory()
    parsed = parse_message(plain_raw)
    stored = store(config.paths.archive_root, plain_raw, parsed)
    text, _ = build_note(parsed, stored, config.account.account_id,
                         config.paths.archive_root, no_enrichment(config, parsed.body_markdown))
    changed = text.replace(PERSONAL_START, PERSONAL_START + "\nAnything I like")
    assert verify_managed(changed)[0]


def test_generated_edit_breaks_managed_hash(config_factory, plain_raw):
    config = config_factory()
    parsed = parse_message(plain_raw)
    stored = store(config.paths.archive_root, plain_raw, parsed)
    text, _ = build_note(parsed, stored, config.account.account_id,
                         config.paths.archive_root, no_enrichment(config, parsed.body_markdown))
    assert not verify_managed(text.replace("Hello Bob", "Tampered"))[0]


def test_note_front_matter_contains_only_useful_nonempty_properties(config_factory, plain_raw):
    config = config_factory()
    parsed = parse_message(plain_raw)
    stored = store(config.paths.archive_root, plain_raw, parsed)
    enrichment = EnrichmentResult(
        provider="jev", model="jev-latest", categories={"receipt": 0.9},
        document_type="receipt",
        entities=({"field": "amount", "value": "$12.00",
                   "evidence": "Total $12.00"},),
    )

    text, _ = build_note(
        parsed, stored, config.account.account_id, config.paths.archive_root,
        enrichment, property_fields=config.extraction_fields,
    )
    front, _ = parse_managed(text)

    assert set(front) == {"date", "from", "to", "subject", "categories",
                          "document_type", "amount"}
    assert front["amount"] == "$12.00"
    assert "due_date" not in front
    assert front["from"] == ["Alice <alice@example.com>"]
    assert front["to"] == ["Bob <bob@example.com>"]
    assert "archive_id" not in front and "managed_sha256" not in front
    assert "<!-- mailkeep:managed-sha256:" in text
    assert "## Extracted details" in text
    assert "**Amount:** $12.00 — evidence: Total $12.00" in text
    assert "## Attachments" not in text
    assert "## Source" in text and "Open original email" in text
    assert verify_managed(text)[0]
    metadata = json.loads(stored.metadata_path.read_text(encoding="utf-8"))
    assert metadata["archive_id"] == stored.archive_id
    assert metadata["raw_sha256"] == stored.raw_sha256


def test_only_selected_safe_entity_fields_become_properties(config_factory, plain_raw):
    config = config_factory()
    parsed = parse_message(plain_raw)
    stored = store(config.paths.archive_root, plain_raw, parsed)
    enrichment = EnrichmentResult(
        provider="jev", model="jev-latest",
        entities=(
            {"field": "due_date", "value": "2024-10-31", "evidence": "Due 31 Oct"},
            {"field": "selected_extra", "value": "keep", "evidence": "keep"},
            {"field": "not_selected", "value": "omit", "evidence": "omit"},
            {"field": "bad-field", "value": "omit", "evidence": "omit"},
            {"field": "subject", "value": "cannot replace", "evidence": "omit"},
            {"field": "amount", "value": "", "evidence": "empty"},
        ),
    )

    text, _ = build_note(
        parsed, stored, config.account.account_id, config.paths.archive_root,
        enrichment,
        property_fields=("due_date", "selected_extra", "bad-field", "subject", "amount"),
    )
    front, _ = parse_managed(text)

    assert front["due_date"] == "2024-10-31"
    assert front["subject"] == parsed.subject
    assert front["selected_extra"] == "keep"
    assert "not_selected" not in front
    assert "bad-field" not in front
    assert "amount" not in front


def test_empty_optional_properties_are_omitted(config_factory, plain_raw):
    config = config_factory()
    parsed = parse_message(plain_raw)
    stored = store(config.paths.archive_root, plain_raw, parsed)
    text, _ = build_note(
        parsed, stored, config.account.account_id, config.paths.archive_root,
        no_enrichment(config, parsed.body_markdown),
    )
    front, _ = parse_managed(text)
    assert set(front) == {"date", "from", "to", "subject"}


def test_legacy_yaml_managed_hash_remains_verifiable(config_factory, plain_raw):
    config = config_factory()
    parsed = parse_message(plain_raw)
    stored = store(config.paths.archive_root, plain_raw, parsed)
    text, digest = build_note(
        parsed, stored, config.account.account_id, config.paths.archive_root,
        no_enrichment(config, parsed.body_markdown),
    )
    front, _ = parse_managed(text)
    front["managed_sha256"] = digest
    _, rest = text[4:].split("\n---\n", 1)
    rest = rest.replace(f"<!-- mailkeep:managed-sha256: {digest} -->\n\n", "", 1)
    legacy = "---\n" + yaml.safe_dump(front, allow_unicode=True, sort_keys=False).rstrip() + \
             "\n---\n" + rest
    assert verify_managed(legacy)[0]
