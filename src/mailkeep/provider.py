from __future__ import annotations

import imaplib
import re
from dataclasses import dataclass
from typing import Protocol

from .config import Config
from .models import RemoteMessage


class Provider(Protocol):
    def capabilities(self) -> set[str]: ...
    def folders(self) -> list[tuple[set[str], str]]: ...
    def discover(self, limit: int | None = None) -> list[RemoteMessage]: ...
    def fetch(self, message: RemoteMessage) -> bytes: ...
    def find_gmail(self, gmail_msgid: str, mailbox: str) -> list[RemoteMessage]: ...
    def mark_processed(self, message: RemoteMessage) -> None: ...
    def trash_mailbox(self) -> str: ...
    def move_to_trash(self, message: RemoteMessage) -> None: ...
    def close(self) -> None: ...


_UID_RE = re.compile(rb"\bUID\s+(\d+)\b")
_GMID_RE = re.compile(rb"\bX-GM-MSGID\s+(\d+)\b")
_SIZE_RE = re.compile(rb"\bRFC822\.SIZE\s+(\d+)\b")
_UIDVALIDITY_RE = re.compile(rb"\[UIDVALIDITY\s+(\d+)\]", re.I)


def _quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class ImapProvider:
    def __init__(self, config: Config, connection: imaplib.IMAP4_SSL | None = None):
        self.config = config
        if connection is None:
            password = config.password()
            if password is None:
                raise ValueError(f"missing secret environment variable {config.account.password_env}")
            connection = imaplib.IMAP4_SSL(config.account.host, config.account.port)
            connection.login(config.account.username, password)
        self.imap = connection

    def capabilities(self) -> set[str]:
        status, data = self.imap.capability()
        if status != "OK":
            raise RuntimeError("IMAP CAPABILITY failed")
        return {item.decode("ascii", "replace").upper() for row in data for item in row.split()}

    def folders(self) -> list[tuple[set[str], str]]:
        status, data = self.imap.list()
        if status != "OK":
            raise RuntimeError("IMAP LIST failed")
        result: list[tuple[set[str], str]] = []
        for raw in data:
            if not raw:
                continue
            text = raw.decode("utf-8", "replace")
            match = re.match(r"\((.*?)\)\s+(?:\".*?\"|NIL)\s+(.+)$", text)
            if match:
                flags = {flag.upper() for flag in match.group(1).split()}
                name = match.group(2).strip().strip('"')
                result.append((flags, name))
        return result

    def _select(self, mailbox: str, readonly: bool = True) -> int:
        status, _ = self.imap.select(mailbox, readonly=readonly)
        if status != "OK":
            raise RuntimeError(f"cannot select mailbox {mailbox!r}")
        response = self.imap.response("UIDVALIDITY")
        if response and response[1] and response[1][0]:
            return int(response[1][0])
        values = getattr(self.imap, "untagged_responses", {}).get("OK", [])
        for value in values:
            match = _UIDVALIDITY_RE.search(value)
            if match:
                return int(match.group(1))
        raise RuntimeError("selected mailbox did not report UIDVALIDITY")

    def discover(self, limit: int | None = None) -> list[RemoteMessage]:
        mailbox = self.config.account.source_label
        uidvalidity = self._select(mailbox, readonly=True)
        status, data = self.imap.uid("SEARCH", None, "ALL")
        if status != "OK":
            raise RuntimeError("UID SEARCH failed")
        uids = data[0].split() if data and data[0] else []
        if limit is not None:
            uids = uids[:limit]
        messages: list[RemoteMessage] = []
        for uid_bytes in uids:
            attributes = "(UID X-GM-MSGID RFC822.SIZE)" if self.config.account.provider == "gmail" else "(UID RFC822.SIZE)"
            status, rows = self.imap.uid("FETCH", uid_bytes, attributes)
            if status != "OK":
                raise RuntimeError(f"metadata fetch failed for UID {uid_bytes.decode()}")
            header = b" ".join(row if isinstance(row, bytes) else row[0]
                               for row in rows if row)
            uid_match, gm_match, size_match = _UID_RE.search(header), _GMID_RE.search(header), _SIZE_RE.search(header)
            messages.append(RemoteMessage(
                account_id=self.config.account.account_id, mailbox=mailbox,
                uidvalidity=uidvalidity, uid=int(uid_match.group(1) if uid_match else uid_bytes),
                gmail_msgid=gm_match.group(1).decode() if gm_match else None,
                size=int(size_match.group(1)) if size_match else None,
            ))
        return messages

    def fetch(self, message: RemoteMessage) -> bytes:
        current = self._select(message.mailbox, readonly=True)
        if current != message.uidvalidity:
            raise RuntimeError("UIDVALIDITY changed; rediscovery is required")
        status, rows = self.imap.uid("FETCH", str(message.uid), "(BODY.PEEK[])")
        if status != "OK":
            raise RuntimeError(f"BODY.PEEK fetch failed for UID {message.uid}")
        payloads = [row[1] for row in rows if isinstance(row, tuple) and len(row) > 1]
        if len(payloads) != 1 or not isinstance(payloads[0], bytes):
            raise RuntimeError(f"ambiguous message body for UID {message.uid}")
        return payloads[0]

    def find_gmail(self, gmail_msgid: str, mailbox: str) -> list[RemoteMessage]:
        uidvalidity = self._select(mailbox, readonly=True)
        status, data = self.imap.uid("SEARCH", None, "X-GM-MSGID", gmail_msgid)
        if status != "OK":
            raise RuntimeError("Gmail stable-ID search failed")
        return [RemoteMessage(self.config.account.account_id, mailbox, uidvalidity, int(uid), gmail_msgid)
                for uid in (data[0].split() if data and data[0] else [])]

    def _labels(self, uid: int, operation: str, label: str) -> None:
        status, _ = self.imap.uid("STORE", str(uid), operation, f"({_quoted(label)})")
        if status != "OK":
            raise RuntimeError(f"Gmail label operation failed for {label!r}")

    def _move(self, uid: int, mailbox: str) -> None:
        status, _ = self.imap.uid("MOVE", str(uid), _quoted(mailbox))
        if status != "OK":
            raise RuntimeError(f"IMAP move failed for mailbox {mailbox!r}")

    def mark_processed(self, message: RemoteMessage) -> None:
        if self.config.account.provider != "gmail":
            raise RuntimeError("provider-side movement is Gmail-only in v1")
        if not message.gmail_msgid:
            raise RuntimeError("no stable Gmail ID is available; source label retained")
        source = self.find_gmail(message.gmail_msgid, self.config.account.source_label)
        if len(source) != 1:
            raise RuntimeError("source message could not be uniquely relocated; source label retained")

        if "MOVE" in self.capabilities():
            self._select(self.config.account.source_label, readonly=False)
            self._move(source[0].uid, self.config.account.processed_label)
        else:
            self._select(self.config.account.source_label, readonly=False)
            self._labels(source[0].uid, "+X-GM-LABELS", self.config.account.processed_label)
            processed = self.find_gmail(message.gmail_msgid, self.config.account.processed_label)
            if len(processed) != 1:
                raise RuntimeError("processed label could not be confirmed; source label retained")
            source = self.find_gmail(message.gmail_msgid, self.config.account.source_label)
            if len(source) != 1:
                raise RuntimeError("source message could not be uniquely relocated; source label retained")
            self._select(self.config.account.source_label, readonly=False)
            self._labels(source[0].uid, "-X-GM-LABELS", self.config.account.source_label)

        processed = self.find_gmail(message.gmail_msgid, self.config.account.processed_label)
        if len(processed) != 1:
            raise RuntimeError("processed label could not be confirmed; provider state remains source")
        remaining = self.find_gmail(message.gmail_msgid, self.config.account.source_label)
        if remaining:
            raise RuntimeError("source label removal could not be confirmed; provider state remains source")

    def trash_mailbox(self) -> str:
        matches = [name for flags, name in self.folders() if "\\TRASH" in flags]
        if len(matches) != 1:
            raise RuntimeError("Gmail Trash mailbox was not uniquely discoverable")
        return matches[0]

    def move_to_trash(self, message: RemoteMessage) -> None:
        if self.config.account.provider != "gmail":
            raise RuntimeError("pruning is Gmail-only in v1")
        self._select(message.mailbox, readonly=False)
        trash = self.trash_mailbox()
        self._labels(message.uid, "+X-GM-LABELS.SILENT", trash)
        self._labels(message.uid, "-X-GM-LABELS.SILENT", self.config.account.processed_label)

    def close(self) -> None:
        try:
            self.imap.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def connect(config: Config) -> ImapProvider:
    return ImapProvider(config)
