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
