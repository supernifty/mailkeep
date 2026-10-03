from __future__ import annotations

from email.message import EmailMessage
from email import policy

from mailkeep.mime import parse_message


def test_plain_text_and_quoted_history_are_preserved(plain_raw):
    parsed = parse_message(plain_raw)
    assert parsed.subject == "Receipt"
    assert "> quoted history" in parsed.body_markdown
    assert parsed.senders[0].email == "alice@example.com"


def test_plain_alternative_wins_over_html():
    message = EmailMessage()
    message["Subject"] = "Alternatives"
    message.set_content("Plain body")
    message.add_alternative("<p>HTML body</p>", subtype="html")
    parsed = parse_message(message.as_bytes(policy=policy.SMTP))
    assert parsed.body_markdown == "Plain body"


def test_html_fallback_suppresses_remote_images_and_rewrites_cid():
    message = EmailMessage()
    message["Subject"] = "HTML"
    message.set_content('<p>Hello</p><img src="https://tracker/x"><img src="cid:logo">', subtype="html")
    message.add_attachment(b"png", maintype="image", subtype="png", filename="logo.png",
                           disposition="inline", cid="<logo>")
    parsed = parse_message(message.as_bytes(policy=policy.SMTP))
    assert "https://tracker" not in parsed.body_markdown
    assert "attachments/" in parsed.body_markdown


def test_html_fallback_removes_css_scripts_and_hidden_preheaders():
    message = EmailMessage()
    message["Subject"] = "Styled message"
    message.set_content(
        "<html><head><style>.receipt { color: red; } @media screen { body { margin: 0; } }</style>"
        "<script>window.tracker = true;</script></head><body>"
        "<div style='display:none'>secret preheader</div><p>Amount due: $12.00</p>"
        "</body></html>",
        subtype="html",
    )
    parsed = parse_message(message.as_bytes(policy=policy.SMTP))
    assert "Amount due: $12.00" in parsed.body_markdown
    assert "color: red" not in parsed.body_markdown
    assert "window.tracker" not in parsed.body_markdown
    assert "secret preheader" not in parsed.body_markdown


def test_html_fallback_flattens_layout_tables_but_keeps_content():
    rows = "".join(f"<table><tr><td>Field {i}</td><td>Value {i}</td></tr></table>" for i in range(6))
    message = EmailMessage()
    message.set_content(f"<html><body>{rows}</body></html>", subtype="html")
    parsed = parse_message(message.as_bytes(policy=policy.SMTP))
    assert "Field 0" in parsed.body_markdown
    assert "Value 5" in parsed.body_markdown
    assert "|" not in parsed.body_markdown
    assert max(map(len, parsed.body_markdown.splitlines())) < 100


def test_unicode_header_and_duplicate_attachment_names():
    message = EmailMessage()
    message["Subject"] = "Café receipt"
    message.set_content("body")
    message.add_attachment(b"one", maintype="application", subtype="octet-stream", filename="same.txt")
    message.add_attachment(b"two", maintype="application", subtype="octet-stream", filename="same.txt")
    parsed = parse_message(message.as_bytes(policy=policy.SMTP))
    assert parsed.subject == "Café receipt"
    assert len({item.stored_name for item in parsed.attachments}) == 2


def test_nested_message_is_archived():
    nested = EmailMessage()
    nested["Subject"] = "Nested"
    nested.set_content("inside")
    outer = EmailMessage()
    outer.set_content("outside")
    outer.add_attachment(nested)
    parsed = parse_message(outer.as_bytes(policy=policy.SMTP))
    assert any(item.media_type == "message/rfc822" and item.stored_name.endswith(".eml")
               for item in parsed.attachments)


def test_missing_date_and_message_id_are_explicit():
    parsed = parse_message(b"Subject: Bare\r\n\r\nbody")
    assert parsed.message_date is None
    assert parsed.date_source == "missing"
    assert parsed.message_id is None
