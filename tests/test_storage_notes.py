from __future__ import annotations

import hashlib
from pathlib import Path

from mailkeep.enrichment import no_enrichment
from mailkeep.mime import parse_message
from mailkeep.notes import PERSONAL_END, PERSONAL_START, build_note, note_filename, verify_managed
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
