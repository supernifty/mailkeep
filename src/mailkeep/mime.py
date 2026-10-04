from __future__ import annotations

import hashlib
import re
from datetime import datetime
from email import policy
from email.headerregistry import Address as EmailAddress
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

import bleach
from bs4 import BeautifulSoup
from markdownify import markdownify

from .models import Address, Attachment, ParsedMessage


_UNSAFE_FILENAME = re.compile(r"[^\w.()@+-]+", re.UNICODE)
def safe_filename(value: str | None, fallback: str) -> str:
    name = Path(value or fallback).name.replace("\x00", "")
    name = _UNSAFE_FILENAME.sub("_", name).strip("._")
    return name[:160] or fallback


def _addresses(values: list[str]) -> tuple[Address, ...]:
    return tuple(Address(name=name or "", email=addr or "") for name, addr in getaddresses(values))


def _date(value: str | None) -> tuple[datetime | None, str]:
    if not value:
        return None, "missing"
    try:
        return parsedate_to_datetime(value), "header"
    except (TypeError, ValueError, OverflowError):
        return None, "invalid"


def _decoded_payload(part) -> tuple[bytes, str | None]:
    try:
        payload = part.get_payload(decode=True)
        if payload is not None:
            return payload, None
        if part.get_content_type() == "message/rfc822":
            nested = part.get_payload()
            if isinstance(nested, list):
                return b"\n".join(item.as_bytes(policy=policy.default) for item in nested), None
        content = part.get_content()
        return (content.encode(part.get_content_charset() or "utf-8")
                if isinstance(content, str) else bytes(content)), None
    except Exception as exc:  # malformed MIME must remain preservable
        return b"", f"{type(exc).__name__}: {exc}"


def _text(part) -> str:
    try:
        value = part.get_content()
        return value if isinstance(value, str) else value.decode("utf-8", "replace")
    except Exception:
        raw = part.get_payload(decode=True) or b""
        return raw.decode(part.get_content_charset() or "utf-8", "replace")


def _html_to_markdown(html: str, cid_names: dict[str, str]) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for element in soup.find_all(("head", "style", "script", "title", "meta", "link")):
        element.decompose()
    for element in soup.find_all(style=True):
        if element.attrs is None:
            continue
        style = re.sub(r"\s+", "", str(element.get("style", ""))).lower()
        if "display:none" in style or "visibility:hidden" in style:
            element.decompose()
    for image in soup.find_all("img"):
        if image.attrs is None:
            continue
        source = str(image.get("src", ""))
        if re.match(r"^(?:https?:)?//", source, re.I):
            alt = str(image.get("alt", "")).strip()
            image.replace_with(f"[image: {alt}]" if alt else "")
        elif source.lower().startswith("cid:"):
            cid = source[4:].strip("<>")
            image["src"] = f"attachments/{cid_names[cid]}" if cid in cid_names else "cid-unavailable"
        elif source.lower().startswith("data:"):
            alt = str(image.get("alt", "")).strip()
            image.replace_with(f"[image: {alt}]" if alt else "")
    tables = soup.find_all("table")
    if len(tables) > 4:
        for cell in soup.find_all(("td", "th")):
            cell.append("\n")
        for row in soup.find_all("tr"):
            row.append("\n")
        for element in soup.find_all(("table", "thead", "tbody", "tfoot", "tr", "td", "th")):
            element.unwrap()
    html = str(soup)

    def replace_cid(match: re.Match[str]) -> str:
        cid = match.group(1).strip("<>")
        return f"attachments/{cid_names[cid]}" if cid in cid_names else "cid-unavailable"

    html = re.sub(r"cid:([^'\" >]+)", replace_cid, html, flags=re.I)
    clean = bleach.clean(
        html,
        tags={"p", "br", "div", "span", "a", "img", "ul", "ol", "li", "blockquote",
              "strong", "b", "em", "i", "code", "pre", "h1", "h2", "h3", "h4", "table",
              "thead", "tbody", "tr", "th", "td"},
        attributes={"a": ["href"], "img": ["src", "alt", "title"]},
        protocols={"http", "https", "mailto", "cid"},
        strip=True,
    )
    converted = markdownify(clean, heading_style="ATX").strip()
    return re.sub(r"\n{3,}", "\n\n", converted)


def parse_message(raw: bytes) -> ParsedMessage:
    diagnostics: list[str] = []
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception as exc:
        message = BytesParser(policy=policy.default.clone(raise_on_defect=False)).parsebytes(raw)
        diagnostics.append(f"parser recovered after {type(exc).__name__}")
    diagnostics.extend(f"email defect: {defect}" for defect in message.defects)

    leaves = list(message.walk())
    attachments: list[Attachment] = []
    cid_names: dict[str, str] = {}
    body_candidates: list[tuple[int, str, str]] = []
    for index, part in enumerate(leaves, start=1):
        if part.is_multipart() and part.get_content_type() != "message/rfc822":
            continue
        media_type = part.get_content_type().lower()
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        cid = (part.get("Content-ID") or "").strip("<>") or None
        is_body = media_type in ("text/plain", "text/html") and disposition != "attachment" and not filename
        if is_body:
            text = _text(part)
            if text.strip():
                body_candidates.append((0 if media_type == "text/plain" else 1, media_type, text))
            continue
        data, error = _decoded_payload(part)
        extension = ".eml" if media_type == "message/rfc822" else ""
        fallback = f"part-{index}{extension}"
        stored = f"{index:04d}-{safe_filename(filename, fallback)}"
        attachment = Attachment(
            ordinal=str(index), original_name=filename, stored_name=stored,
            media_type=media_type, disposition=disposition, content_id=cid,
            data=data, sha256=hashlib.sha256(data).hexdigest(),
            declared_charset=part.get_content_charset(), decode_error=error,
        )
        attachments.append(attachment)
        if cid:
            cid_names[cid] = stored
        if error:
            diagnostics.append(f"part {index} decode failed: {error}")

    if body_candidates:
        _, media_type, body = min(body_candidates, key=lambda item: item[0])
        body_markdown = body.strip() if media_type == "text/plain" else _html_to_markdown(body, cid_names)
    else:
        body_markdown = ""

    date, date_source = _date(message.get("Date"))
    try:
        subject = str(message.get("Subject", ""))
    except Exception:
        subject = "[undecodable subject]"
        diagnostics.append("subject header could not be decoded")
    return ParsedMessage(
        subject=subject,
        message_id=str(message.get("Message-ID")) if message.get("Message-ID") else None,
        message_date=date,
        date_source=date_source,
        senders=_addresses(message.get_all("From", [])),
        recipients=_addresses(message.get_all("To", []) + message.get_all("Cc", [])),
        body_markdown=body_markdown,
        attachments=tuple(attachments),
        diagnostics=tuple(diagnostics),
    )
