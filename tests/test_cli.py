from mailkeep.cli import _prune_has_blocked_due_item


def test_prune_newer_message_is_a_successful_noop():
    result = [{"eligible": False, "gates": {"retention_elapsed": False}}]

    assert not _prune_has_blocked_due_item(result)


def test_prune_due_message_blocked_by_safety_gate_is_a_failure():
    result = [{
        "eligible": False,
        "gates": {"retention_elapsed": True, "remote_hash_match": False},
    }]

    assert _prune_has_blocked_due_item(result)
