"""Argument checks and response shaping for the Google Calendar and Gmail tools."""

import re
import base64
from html import unescape
from typing import Any
from datetime import date, time, tzinfo, datetime, timedelta
from dataclasses import dataclass
from email.message import EmailMessage
from collections.abc import Mapping


MAX_RESULTS = 20
DEFAULT_RESULTS = 10
MAX_EMAIL_BODY_CHARS = 4000
MAX_EVENT_DESCRIPTION_CHARS = 500
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")
_MESSAGE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"[ \t]+\n|\n{3,}|[ \t]{2,}")


@dataclass(frozen=True)
class FieldError:
    """A tool argument the model must correct."""

    message: str


@dataclass(frozen=True)
class ClockTime:
    """A parsed calendar bound."""

    moment: datetime
    date_only: bool


def aware_now(now: datetime | None = None) -> datetime:
    """Return an aware local time, using now when the caller supplies one."""
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        return current.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return current


def parse_clock_time(value: object, *, now: datetime) -> ClockTime | FieldError:
    """Parse an ISO datetime, a calendar date, or today/tomorrow."""
    if not isinstance(value, str) or not value.strip():
        return FieldError("must be an ISO 8601 datetime, a YYYY-MM-DD date, or today/tomorrow")
    text = value.strip()
    word = text.lower()
    if word in {"today", "tomorrow"}:
        day = now.date() + timedelta(days=1 if word == "tomorrow" else 0)
        return ClockTime(_at_midnight(day, now.tzinfo), True)
    if _DATE_ONLY.fullmatch(text):
        try:
            day = date.fromisoformat(text)
        except ValueError:
            return FieldError("must be an ISO 8601 datetime, a YYYY-MM-DD date, or today/tomorrow")
        return ClockTime(_at_midnight(day, now.tzinfo), True)
    normalized = f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return FieldError("must be an ISO 8601 datetime, a YYYY-MM-DD date, or today/tomorrow")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    return ClockTime(parsed, False)


def calendar_window(
    start: object,
    end: object,
    *,
    now: datetime | None = None,
) -> tuple[str, str] | FieldError:
    """Resolve a list window. Date-only and today/tomorrow ends include that local day."""
    current = aware_now(now)
    if start is None or (isinstance(start, str) and not start.strip()):
        start_parsed: ClockTime | FieldError = parse_clock_time("today", now=current)
    else:
        start_parsed = parse_clock_time(start, now=current)
    if isinstance(start_parsed, FieldError):
        return FieldError(f"start {start_parsed.message}")

    if end is None or (isinstance(end, str) and not end.strip()):
        end_moment = start_parsed.moment + timedelta(days=1)
        return _rfc3339(start_parsed.moment), _rfc3339(end_moment)

    end_parsed = parse_clock_time(end, now=current)
    if isinstance(end_parsed, FieldError):
        return FieldError(f"end {end_parsed.message}")
    end_moment = _inclusive_end(end_parsed, current.tzinfo) if end_parsed.date_only else end_parsed.moment
    if end_moment <= start_parsed.moment:
        return FieldError("end must be after start")
    return _rfc3339(start_parsed.moment), _rfc3339(end_moment)


def event_interval(
    start: object,
    end: object,
    *,
    now: datetime | None = None,
) -> dict[str, dict[str, str]] | FieldError:
    """Build a Calendar event start/end. Two dates include the end date as an all-day event."""
    current = aware_now(now)
    start_parsed = parse_clock_time(start, now=current)
    if isinstance(start_parsed, FieldError):
        return FieldError(f"start {start_parsed.message}")
    end_parsed = parse_clock_time(end, now=current)
    if isinstance(end_parsed, FieldError):
        return FieldError(f"end {end_parsed.message}")

    if start_parsed.date_only and end_parsed.date_only:
        start_day = start_parsed.moment.date()
        exclusive_end = end_parsed.moment.date() + timedelta(days=1)
        if exclusive_end <= start_day:
            return FieldError("end must be after start")
        return {"start": {"date": start_day.isoformat()}, "end": {"date": exclusive_end.isoformat()}}

    start_moment = start_parsed.moment
    end_moment = _inclusive_end(end_parsed, current.tzinfo) if end_parsed.date_only else end_parsed.moment
    if end_moment <= start_moment:
        return FieldError("end must be after start")
    return {"start": {"dateTime": _rfc3339(start_moment)}, "end": {"dateTime": _rfc3339(end_moment)}}


def bounded_int(value: object, *, default: int = DEFAULT_RESULTS, limit: int = MAX_RESULTS) -> int | FieldError:
    """Accept an integer result cap, including a digit string from the model."""
    if value is None:
        return default
    if isinstance(value, bool):
        return FieldError(f"max_results must be an integer from 1 to {limit}")
    parsed = value
    if isinstance(parsed, str) and parsed.strip().isdigit():
        parsed = int(parsed.strip())
    if isinstance(parsed, str):
        return FieldError(f"max_results must be an integer from 1 to {limit}")
    if not isinstance(parsed, int):
        return FieldError(f"max_results must be an integer from 1 to {limit}")
    if parsed < 1 or parsed > limit:
        return FieldError(f"max_results must be an integer from 1 to {limit}")
    return parsed


def plain_line(value: object, field: str, *, limit: int) -> str | FieldError:
    """Return a single-line field, or an error when it is missing or too long."""
    if not isinstance(value, str) or not value.strip():
        return FieldError(f"{field} must be a non-empty string")
    text = " ".join(value.split())
    if len(text) > limit:
        return FieldError(f"{field} is too long")
    return text


def optional_line(value: object, field: str, *, limit: int) -> str | None | FieldError:
    """Return an optional single-line field. Blank means omitted."""
    if value is None:
        return None
    if not isinstance(value, str):
        return FieldError(f"{field} must be a string")
    if not value.strip():
        return None
    text = " ".join(value.split())
    if len(text) > limit:
        return FieldError(f"{field} is too long")
    return text


def optional_text(value: object, field: str, *, limit: int) -> str | None | FieldError:
    """Return optional multiline text. Blank means omitted."""
    if value is None:
        return None
    if not isinstance(value, str):
        return FieldError(f"{field} must be a string")
    text = value.strip()
    if not text:
        return None
    if len(text) > limit:
        return FieldError(f"{field} is too long")
    return text


def body_text(value: object, *, limit: int = 20000) -> str | FieldError:
    """Return an email or event body, preserving newlines."""
    if not isinstance(value, str) or not value.strip():
        return FieldError("body must be a non-empty string")
    text = value.strip()
    if len(text) > limit:
        return FieldError("body is too long")
    return text


def optional_bool(value: object, field: str, *, default: bool = False) -> bool | FieldError:
    """Return a boolean flag."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return FieldError(f"{field} must be a boolean")


def email_address(value: object, field: str) -> str | FieldError:
    """Return one email address."""
    text = plain_line(value, field, limit=320)
    if isinstance(text, FieldError):
        return text
    local, separator, domain = text.partition("@")
    if separator != "@" or not local or not domain or "@" in domain:
        return FieldError(f"{field} must be an email address")
    return text


def email_addresses(value: object, field: str) -> list[str] | FieldError:
    """Return email addresses from a list or a comma-separated string."""
    parts: list[object] = []
    if isinstance(value, str):
        parts.extend(value.split(","))
    elif isinstance(value, list):
        parts.extend(value)
    else:
        return FieldError(f"{field} must be an email address")
    emails: list[str] = []
    for part in parts:
        checked = email_address(part, field)
        if isinstance(checked, FieldError):
            return checked
        emails.append(checked)
    if not emails:
        return FieldError(f"{field} must be an email address")
    return emails


def message_id(value: object) -> str | FieldError:
    """Return a Gmail message id."""
    if not isinstance(value, str) or _MESSAGE_ID.fullmatch(value.strip()) is None:
        return FieldError("message_id must be a Gmail message id")
    return value.strip()


def truncate_text(text: str, limit: int) -> str:
    """Cap text returned to the model."""
    if len(text) <= limit:
        return text
    return text[:limit] + "…[truncated]"


def format_event(item: Any) -> dict[str, Any] | None:
    """Return the calendar fields the model needs."""
    if not isinstance(item, dict):
        return None
    raw_start = item.get("start")
    raw_end = item.get("end")
    start: dict[str, Any] = raw_start if isinstance(raw_start, dict) else {}
    end: dict[str, Any] = raw_end if isinstance(raw_end, dict) else {}
    attendees: list[str] = []
    raw_attendees = item.get("attendees")
    if isinstance(raw_attendees, list):
        for person in raw_attendees:
            if isinstance(person, dict) and isinstance(person.get("email"), str):
                attendees.append(person["email"])
    description = _string_field(item, "description")
    summary = _string_field(item, "summary")
    location = _string_field(item, "location")
    start_value = start.get("dateTime") or start.get("date") or ""
    end_value = end.get("dateTime") or end.get("date") or ""
    return {
        "id": _string_field(item, "id"),
        "title": summary or "(no title)",
        "start": start_value if isinstance(start_value, str) else "",
        "end": end_value if isinstance(end_value, str) else "",
        "location": location,
        "description": truncate_text(description, MAX_EVENT_DESCRIPTION_CHARS),
        "attendees": attendees,
    }


def header_value(payload: Any, name: str) -> str:
    """Read one Gmail header."""
    if not isinstance(payload, dict):
        return ""
    headers = payload.get("headers")
    if not isinstance(headers, list):
        return ""
    for header in headers:
        if not isinstance(header, dict):
            continue
        header_name = header.get("name")
        if isinstance(header_name, str) and header_name.lower() == name.lower():
            value = header.get("value")
            return value if isinstance(value, str) else ""
    return ""


def extract_body(payload: Any) -> str:
    """Prefer text/plain, then a tag-stripped text/html part."""
    if not isinstance(payload, dict):
        return ""
    parts = payload.get("parts")
    if isinstance(parts, list) and parts:
        plain = ""
        html = ""
        for part in parts:
            if not isinstance(part, dict):
                continue
            text = extract_body(part)
            mime = part.get("mimeType")
            if mime == "text/plain" and text:
                plain = text
            elif mime == "text/html" and text:
                html = text
            elif text and not plain:
                plain = text
        return plain or html
    return _decode_body(payload)


def summarize_message(message: Any, *, include_body: bool) -> dict[str, Any]:
    """Return subject, sender, date, and a truncated body when requested."""
    if not isinstance(message, dict):
        return {"id": "", "subject": "", "from": "", "date": "", "snippet": ""}
    payload = message.get("payload")
    snippet = _string_field(message, "snippet")
    summary: dict[str, Any] = {
        "id": message.get("id") if isinstance(message.get("id"), str) else "",
        "thread_id": message.get("threadId") if isinstance(message.get("threadId"), str) else "",
        "subject": header_value(payload, "Subject"),
        "from": header_value(payload, "From"),
        "date": header_value(payload, "Date"),
        "snippet": truncate_text(snippet, MAX_EMAIL_BODY_CHARS),
    }
    if include_body:
        summary["body"] = truncate_text(extract_body(payload), MAX_EMAIL_BODY_CHARS)
    return summary


def prepare_gmail_message(fields: Mapping[str, Any]) -> tuple[str, str, str, bool] | FieldError:
    """Validate to, subject, body, and html for a draft or a send."""
    recipients = email_addresses(fields.get("to"), "to")
    if isinstance(recipients, FieldError):
        return recipients
    subject = plain_line(fields.get("subject"), "subject", limit=300)
    if isinstance(subject, FieldError):
        return subject
    body = body_text(fields.get("body"))
    if isinstance(body, FieldError):
        return body
    html = optional_bool(fields.get("html"), "html")
    if isinstance(html, FieldError):
        return html
    return ", ".join(recipients), subject, body, html


def raw_message(*, to: str, subject: str, body: str, html: bool) -> str:
    """Encode a single-recipient RFC 2822 message for the Gmail API."""
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    if html:
        message.set_content(body, subtype="html")
    else:
        message.set_content(body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


def _string_field(item: Mapping[Any, Any], key: str) -> str:
    value = item.get(key)
    if isinstance(value, str):
        return value
    return ""


def _at_midnight(day: date, zone: tzinfo | None) -> datetime:
    return datetime.combine(day, time.min, tzinfo=zone)


def _inclusive_end(parsed: ClockTime, zone: tzinfo | None) -> datetime:
    return _at_midnight(parsed.moment.date() + timedelta(days=1), zone)


def _rfc3339(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _decode_body(payload: dict[str, Any]) -> str:
    body = payload.get("body")
    if not isinstance(body, dict):
        return ""
    data = body.get("data")
    if not isinstance(data, str) or not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    try:
        text = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")
    except (ValueError, UnicodeError):
        return ""
    mime = payload.get("mimeType")
    if mime == "text/html":
        return _strip_html(text)
    if isinstance(mime, str) and mime.startswith("text/"):
        return text
    return ""


def _strip_html(html: str) -> str:
    text = unescape(_TAG.sub(" ", html))
    return _WHITESPACE.sub(" ", text).strip()
