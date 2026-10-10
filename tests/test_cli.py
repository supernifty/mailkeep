import pytest

from mailkeep.cli import _prune_has_blocked_due_item, parser


def test_prune_newer_message_is_a_successful_noop():
    result = [{"eligible": False, "gates": {"retention_elapsed": False}}]

    assert not _prune_has_blocked_due_item(result)


def test_prune_due_message_blocked_by_safety_gate_is_a_failure():
    result = [{
        "eligible": False,
        "gates": {"retention_elapsed": True, "remote_hash_match": False},
    }]

    assert _prune_has_blocked_due_item(result)


def test_extract_attachments_cli_modes_and_scope():
    args = parser().parse_args([
        "extract-attachments", "--execute", "--archive-id", "archive-1",
        "--retry-failed",
    ])
    assert args.execute and args.retry_failed and args.archive_id == "archive-1"
    with pytest.raises(SystemExit):
        parser().parse_args(["extract-attachments"])


def test_regenerate_notes_cli_modes_and_scope():
    args = parser().parse_args([
        "regenerate-notes", "--execute", "--archive-id", "archive-1",
    ])
    assert args.execute and args.archive_id == "archive-1"
    with pytest.raises(SystemExit):
        parser().parse_args(["regenerate-notes"])


def test_setup_dashboard_cli_requires_exactly_one_mode():
    dry_run = parser().parse_args(["setup-dashboard", "--dry-run"])
    execute = parser().parse_args(["setup-dashboard", "--execute"])

    assert dry_run.dry_run and not dry_run.execute
    assert execute.execute and not execute.dry_run
    with pytest.raises(SystemExit):
        parser().parse_args(["setup-dashboard"])
    with pytest.raises(SystemExit):
        parser().parse_args(["setup-dashboard", "--dry-run", "--execute"])
