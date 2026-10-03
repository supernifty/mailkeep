from __future__ import annotations

from mailkeep.models import RemoteMessage
from mailkeep.provider import ImapProvider


class FakeImap:
    def __init__(self):
        self.calls = []
        self.readonly = None
        self.confirm_processed = True
        self.mailbox = None
        self.source_present = True
        self.processed_present = False
        self.remove_source = True
        self.move_supported = False

    def select(self, mailbox, readonly=True):
        self.calls.append(("select", mailbox, readonly))
        self.readonly = readonly
        self.mailbox = mailbox
        return "OK", [b"1"]

    def response(self, name):
        return name, [b"42"]

    def capability(self):
        capabilities = b"IMAP4rev1 X-GM-EXT-1"
        if self.move_supported:
            capabilities += b" MOVE"
        return "OK", [capabilities]

    def uid(self, command, *args):
        self.calls.append(("uid", command, *args))
        if command == "SEARCH" and args[-2] == "X-GM-MSGID":
            present = self.processed_present if self.mailbox == "Mailkeep-Processed" else self.source_present
            if self.mailbox == "Mailkeep-Processed" and not self.confirm_processed:
                present = False
            return "OK", [b"7" if present else b""]
        if command == "FETCH" and args[-1] == "(BODY.PEEK[])":
            return "OK", [(b"1 (UID 7 BODY[] {4}", b"raw!"), b")"]
        if command == "STORE":
            operation, label = args[-2], args[-1]
            if operation.startswith("+") and "Mailkeep-Processed" in label:
                self.processed_present = True
            if operation.startswith("-") and "Mailkeep" in label and self.remove_source:
                self.source_present = False
            return "OK", [b""]
        if command == "MOVE":
            self.processed_present = True
            if self.remove_source:
                self.source_present = False
            return "OK", [b""]
        return "OK", [b""]

    def list(self):
        return "OK", [b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Bin"']


def make_provider(config_factory):
    provider = object.__new__(ImapProvider)
    provider.config = config_factory()
    provider.imap = FakeImap()
    return provider


def test_fetch_uses_uid_and_body_peek(config_factory):
    provider = make_provider(config_factory)
    message = RemoteMessage("test-account", "Mailkeep", 42, 7, "123")
    assert provider.fetch(message) == b"raw!"
    assert ("uid", "FETCH", "7", "(BODY.PEEK[])") in provider.imap.calls
    assert provider.imap.readonly is True


def test_uidvalidity_change_refuses_old_uid(config_factory):
    provider = make_provider(config_factory)
    message = RemoteMessage("test-account", "Mailkeep", 41, 7, "123")
    try:
        provider.fetch(message)
    except RuntimeError as exc:
        assert "UIDVALIDITY changed" in str(exc)
    else:
        raise AssertionError("stale UID was fetched")


def test_processed_labels_are_added_before_source_removed(config_factory):
    provider = make_provider(config_factory)
    message = RemoteMessage("test-account", "Mailkeep", 42, 7, "123")
    provider.mark_processed(message)
    stores = [call for call in provider.imap.calls if call[:2] == ("uid", "STORE")]
    assert stores[0][4] == '("Mailkeep-Processed")'
    assert stores[1][3] == "-X-GM-LABELS"
    assert provider.imap.source_present is False


def test_move_capability_uses_atomic_uid_move(config_factory):
    provider = make_provider(config_factory)
    provider.imap.move_supported = True
    message = RemoteMessage("test-account", "Mailkeep", 42, 7, "123")
    provider.mark_processed(message)
    mutations = [call for call in provider.imap.calls
                 if call[:2] in (("uid", "MOVE"), ("uid", "STORE"))]
    assert mutations == [("uid", "MOVE", "7", '"Mailkeep-Processed"')]
    assert provider.imap.source_present is False
    assert provider.imap.processed_present is True


def test_source_label_is_retained_when_processed_label_cannot_be_confirmed(config_factory):
    provider = make_provider(config_factory)
    provider.imap.confirm_processed = False
    message = RemoteMessage("test-account", "Mailkeep", 42, 7, "123")
    try:
        provider.mark_processed(message)
    except RuntimeError as exc:
        assert "source label retained" in str(exc)
    else:
        raise AssertionError("unconfirmed label change was accepted")
    removals = [call for call in provider.imap.calls
                if call[:2] == ("uid", "STORE") and call[3].startswith("-")]
    assert removals == []


def test_processed_state_fails_when_source_label_removal_is_not_confirmed(config_factory):
    provider = make_provider(config_factory)
    provider.imap.remove_source = False
    message = RemoteMessage("test-account", "Mailkeep", 42, 7, "123")
    try:
        provider.mark_processed(message)
    except RuntimeError as exc:
        assert "source label removal could not be confirmed" in str(exc)
    else:
        raise AssertionError("unconfirmed source-label removal was accepted")


def test_trash_is_discovered_by_special_use_flag(config_factory):
    provider = make_provider(config_factory)
    assert provider.trash_mailbox() == "[Gmail]/Bin"
