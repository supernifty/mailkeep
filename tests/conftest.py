from __future__ import annotations

from pathlib import Path

import pytest

from mailkeep.config import Config

# The configured `--basetemp=tmp/pytest` is created without parent
# directories, so a fresh checkout would fail before the first test runs.
Path("tmp").mkdir(exist_ok=True)


@pytest.fixture
def config_factory(tmp_path: Path):
    def build(*, enrichment: str = "none", deletion_enabled: bool = False,
              retention_days: int = 0) -> Config:
        vault = tmp_path / "vault"
        vault.mkdir(exist_ok=True)
        return Config.model_validate({
            "schema_version": 1,
            "account": {
                "account_id": "test-account", "provider": "gmail",
                "host": "imap.example.test", "username": "test@example.test",
                "password_env": "TEST_IMAP_PASSWORD", "source_label": "Mailkeep",
                "processed_label": "Mailkeep-Processed",
            },
            "paths": {
                "archive_root": tmp_path / "archive", "obsidian_vault": vault,
                "obsidian_folder": "Mailkeep", "database": tmp_path / "state.sqlite3",
            },
            "retention_days": retention_days,
            "deletion_enabled": deletion_enabled,
            "categories": {"receipt": 0.7},
            "document_types": ["receipt", "invoice"],
            "extraction_fields": ["amount", "due_date"],
            "enrichment": ({"provider": "none", "privacy": "local"} if enrichment == "none"
                           else {"provider": enrichment, "model": "test-model",
                                 "endpoint": "http://localhost:11434", "privacy": "local"}),
        })
    return build


@pytest.fixture
def plain_raw() -> bytes:
    return (b"From: Alice <alice@example.com>\r\nTo: Bob <bob@example.com>\r\n"
            b"Subject: Receipt\r\nMessage-ID: <same@example.com>\r\n"
            b"Date: Tue, 01 Oct 2024 10:30:00 +1000\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\nHello Bob.\r\n> quoted history\r\n")
