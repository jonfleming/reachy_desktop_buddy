import sys
import json
import base64
import importlib
from types import SimpleNamespace
from typing import Any
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

from reachy_desktop_buddy import google_auth
from reachy_desktop_buddy.google_auth import (
    SCOPES,
    GoogleAuthError,
    GoogleNotConfiguredError,
    GoogleLibrariesMissingError,
    load_credentials,
    client_config_from_env,
)
from reachy_desktop_buddy.google_data import (
    MAX_EMAIL_BODY_CHARS,
    FieldError,
    extract_body,
    event_interval,
    calendar_window,
    summarize_message,
)
from reachy_desktop_buddy.profile_store import read_packaged_default_profile
from reachy_desktop_buddy.tools.core_tools import ToolDependencies
from reachy_desktop_buddy.tools.send_gmail import SendGmail
from reachy_desktop_buddy.tools.search_gmail import SearchGmail
from reachy_desktop_buddy.tools.get_gmail_message import GetGmailMessage
from reachy_desktop_buddy.tools.create_gmail_draft import CreateGmailDraft
from reachy_desktop_buddy.tools.list_calendar_events import ListCalendarEvents
from reachy_desktop_buddy.tools.create_calendar_event import CreateCalendarEvent


NOW = datetime(2026, 9, 23, 15, 30, tzinfo=timezone(timedelta(hours=-7)))
_GOOGLE_TOOL_NAMES = (
    "list_calendar_events",
    "create_calendar_event",
    "search_gmail",
    "get_gmail_message",
    "create_gmail_draft",
    "send_gmail",
)


class _Creds:
    def __init__(
        self,
        *,
        valid: bool = False,
        expired: bool = False,
        refresh_token: str | None = "refresh-token",
        scopes_ok: bool = True,
    ) -> None:
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self.scopes_ok = scopes_ok
        self.refresh_calls = 0

    def has_scopes(self, scopes: list[str]) -> bool:
        assert scopes == SCOPES
        return self.scopes_ok

    def refresh(self, request: object) -> None:
        self.refresh_calls += 1
        self.valid = True
        self.expired = False

    def to_json(self) -> str:
        return '{"token": "saved"}'


class _CredentialsApi:
    def __init__(self, credentials: _Creds) -> None:
        self.credentials = credentials

    def from_authorized_user_file(self, path: str, scopes: list[str]) -> _Creds:
        assert scopes == SCOPES
        assert Path(path).is_file()
        return self.credentials


class _Flow:
    def __init__(self, credentials: _Creds) -> None:
        self.credentials = credentials
        self.kwargs: dict[str, Any] = {}

    def run_local_server(self, **kwargs: Any) -> _Creds:
        self.kwargs = kwargs
        return self.credentials


class _FlowApi:
    def __init__(self, flow: _Flow) -> None:
        self.flow = flow
        self.config: dict[str, Any] | None = None

    def from_client_config(self, config: dict[str, Any], scopes: list[str]) -> _Flow:
        assert scopes == SCOPES
        self.config = config
        return self.flow


class _Api:
    """Records Google client calls and returns queued execute() payloads."""

    def __init__(self, results: list[dict[str, Any]]) -> None:
        self.results = results
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.execute_error: Exception | None = None

    def events(self) -> "_Api":
        return self

    def users(self) -> "_Api":
        return self

    def messages(self) -> "_Api":
        return self

    def drafts(self) -> "_Api":
        return self

    def list(self, **kwargs: Any) -> MagicMock:
        return self._request("list", kwargs)

    def insert(self, **kwargs: Any) -> MagicMock:
        return self._request("insert", kwargs)

    def get(self, **kwargs: Any) -> MagicMock:
        return self._request("get", kwargs)

    def create(self, **kwargs: Any) -> MagicMock:
        return self._request("create", kwargs)

    def send(self, **kwargs: Any) -> MagicMock:
        return self._request("send", kwargs)

    def _request(self, name: str, kwargs: dict[str, Any]) -> MagicMock:
        self.calls.append((name, kwargs))
        request = MagicMock()
        if self.execute_error is not None:
            request.execute.side_effect = self.execute_error
        else:
            request.execute.return_value = self.results.pop(0)
        return request


def _deps(tmp_path: Path) -> ToolDependencies:
    return ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock(), instance_path=tmp_path)


def _use_service(monkeypatch: pytest.MonkeyPatch, service: _Api) -> None:
    monkeypatch.setattr(google_auth, "libraries_installed", lambda: True)

    def build_service(api: str, version: str, instance_path: str | Path | None) -> _Api:
        return service

    monkeypatch.setattr(google_auth, "build_service", build_service)


def _credentials_module(credentials: _Creds) -> SimpleNamespace:
    return SimpleNamespace(Credentials=_CredentialsApi(credentials))


def _flow_module(flow: _Flow) -> SimpleNamespace:
    return SimpleNamespace(InstalledAppFlow=_FlowApi(flow))


def _requests_module() -> SimpleNamespace:
    return SimpleNamespace(Request=lambda: object())


def _clear_client_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(google_auth.CLIENT_ID_ENV, raising=False)
    monkeypatch.delenv(google_auth.CLIENT_SECRET_ENV, raising=False)
    monkeypatch.delenv(google_auth.CLIENT_SECRET_FILE_ENV, raising=False)


def _gmail_payload(body: str, *, html: bool = False) -> dict[str, Any]:
    encoded = base64.urlsafe_b64encode(body.encode()).decode()
    return {
        "id": "abc123",
        "threadId": "thr1",
        "snippet": "short snippet",
        "payload": {
            "mimeType": "text/html" if html else "text/plain",
            "headers": [
                {"name": "Subject", "value": "Hello"},
                {"name": "From", "value": "Ada <ada@example.com>"},
                {"name": "Date", "value": "Wed, 23 Sep 2026 12:00:00 +0000"},
            ],
            "body": {"data": encoded},
        },
    }


def test_scopes_cover_calendar_and_gmail() -> None:
    """Desktop consent asks for calendar edits plus read, draft, and explicit send."""
    assert SCOPES == [
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.send",
    ]


def test_google_tools_match_module_names_and_stay_off_by_default() -> None:
    """Tool access enables a module stem, so each tool name must match its file."""
    enabled = set(read_packaged_default_profile().default_tools)
    for tool_name in _GOOGLE_TOOL_NAMES:
        module = importlib.import_module(f"reachy_desktop_buddy.tools.{tool_name}")
        tool_classes = [
            value
            for value in vars(module).values()
            if isinstance(value, type)
            and value.__module__ == module.__name__
            and any(base.__name__ == "Tool" for base in value.__mro__)
        ]
        assert [tool.name for tool in tool_classes] == [tool_name]
        assert tool_name not in enabled
    assert "only when the user explicitly asks to send" in SendGmail.description
    assert "untrusted" in GetGmailMessage.description
    assert "untrusted" in SearchGmail.description


def test_user_data_dir_uses_localappdata_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows tokens go under LOCALAPPDATA when the app has no instance directory."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", "/tmp/localappdata")
    assert google_auth.user_data_dir() == Path("/tmp/localappdata/reachy_desktop_buddy")


def test_user_data_dir_falls_back_when_localappdata_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows still has a path when LOCALAPPDATA is missing."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("/home/ada")))
    assert google_auth.user_data_dir() == Path("/home/ada/AppData/Local/reachy_desktop_buddy")


def test_user_data_dir_prefers_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-Windows tokens follow XDG, then ~/.local/share."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/xdg")
    assert google_auth.user_data_dir() == Path("/tmp/xdg/reachy_desktop_buddy")
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: Path("/home/ada")))
    assert google_auth.user_data_dir() == Path("/home/ada/.local/share/reachy_desktop_buddy")


def test_token_path_prefers_the_app_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The running app instance wins over the process-wide fallback."""
    monkeypatch.setattr(google_auth.config, "INSTANCE_PATH", tmp_path / "elsewhere")
    assert google_auth.token_path(tmp_path) == tmp_path / "google_oauth_token.json"
    assert google_auth.token_path(None) == tmp_path / "elsewhere" / "google_oauth_token.json"


def test_client_config_from_env_and_secret_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Client id/secret and a Desktop JSON file both produce an installed-app config."""
    _clear_client_env(monkeypatch)
    assert client_config_from_env() is None

    monkeypatch.setenv(google_auth.CLIENT_ID_ENV, "client-id")
    monkeypatch.setenv(google_auth.CLIENT_SECRET_ENV, "client-secret")
    config = client_config_from_env()
    assert config is not None
    assert config["installed"]["client_id"] == "client-id"
    assert config["installed"]["client_secret"] == "client-secret"

    secret_path = tmp_path / "client_secret.json"
    secret_path.write_text(json.dumps({"installed": {"client_id": "from-file"}}), encoding="utf-8")
    monkeypatch.setenv(google_auth.CLIENT_SECRET_FILE_ENV, str(secret_path))
    from_file = client_config_from_env()
    assert from_file is not None
    assert from_file["installed"]["client_id"] == "from-file"

    web_path = tmp_path / "web.json"
    web_path.write_text(json.dumps({"web": {"client_id": "web"}}), encoding="utf-8")
    monkeypatch.setenv(google_auth.CLIENT_SECRET_FILE_ENV, str(web_path))
    with pytest.raises(GoogleAuthError, match="Desktop"):
        client_config_from_env()


def test_calendar_window_and_event_interval() -> None:
    """today/tomorrow and date-only ends include that local day; datetimes are exact."""
    assert calendar_window(None, None, now=NOW) == ("2026-09-23T00:00:00-07:00", "2026-09-24T00:00:00-07:00")
    assert calendar_window("tomorrow", None, now=NOW) == ("2026-09-24T00:00:00-07:00", "2026-09-25T00:00:00-07:00")
    assert calendar_window("today", "tomorrow", now=NOW) == ("2026-09-23T00:00:00-07:00", "2026-09-25T00:00:00-07:00")
    assert calendar_window("2026-09-23T18:00:00Z", "2026-09-23T19:00:00Z", now=NOW) == (
        "2026-09-23T18:00:00+00:00",
        "2026-09-23T19:00:00+00:00",
    )
    assert isinstance(calendar_window("next week", None, now=NOW), FieldError)
    assert isinstance(calendar_window("2026-09-24T12:00:00+00:00", "2026-09-24T11:00:00+00:00", now=NOW), FieldError)

    all_day = event_interval("today", "today", now=NOW)
    assert all_day == {"start": {"date": "2026-09-23"}, "end": {"date": "2026-09-24"}}
    spanning = event_interval("today", "tomorrow", now=NOW)
    assert spanning == {"start": {"date": "2026-09-23"}, "end": {"date": "2026-09-25"}}
    timed = event_interval("2026-09-23T15:00:00-07:00", "2026-09-23T16:00:00-07:00", now=NOW)
    assert timed == {
        "start": {"dateTime": "2026-09-23T15:00:00-07:00"},
        "end": {"dateTime": "2026-09-23T16:00:00-07:00"},
    }


def test_extract_body_prefers_plain_text_and_strips_html() -> None:
    """HTML-only messages become text, and a plain part wins over HTML."""
    html_only = _gmail_payload("<p>Hi</p> <b>there</b>", html=True)
    assert extract_body(html_only["payload"]) == "Hi there"
    mixed = {
        "mimeType": "multipart/alternative",
        "parts": [
            _gmail_payload("<p>html</p>", html=True)["payload"],
            _gmail_payload("plain words")["payload"],
        ],
    }
    assert extract_body(mixed) == "plain words"


def test_summarize_message_truncates_the_body() -> None:
    """Large bodies are capped before they reach the model."""
    message = _gmail_payload("a" * (MAX_EMAIL_BODY_CHARS + 50))
    summary = summarize_message(message, include_body=True)
    assert summary["subject"] == "Hello"
    assert summary["from"] == "Ada <ada@example.com>"
    assert summary["body"].endswith("…[truncated]")
    assert len(summary["body"]) < MAX_EMAIL_BODY_CHARS + 20


@pytest.mark.asyncio
async def test_list_calendar_events_returns_a_short_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid window is sent to the primary calendar and the response is trimmed."""
    service = _Api(
        [
            {
                "items": [
                    {
                        "id": "evt1",
                        "summary": "Standup",
                        "start": {"dateTime": "2026-09-24T09:00:00-07:00"},
                        "end": {"dateTime": "2026-09-24T09:15:00-07:00"},
                        "location": "Desk",
                        "description": "x" * 800,
                        "attendees": [{"email": "ada@example.com"}, {"displayName": "no email"}],
                    }
                ]
            }
        ]
    )
    _use_service(monkeypatch, service)
    result = await ListCalendarEvents()(
        _deps(tmp_path),
        start="2026-09-24T00:00:00-07:00",
        end="2026-09-25T00:00:00-07:00",
        max_results="5",
    )
    assert "error" not in result
    assert result["events"][0]["title"] == "Standup"
    assert result["events"][0]["attendees"] == ["ada@example.com"]
    assert result["events"][0]["description"].endswith("…[truncated]")
    assert service.calls[0][0] == "list"
    assert service.calls[0][1]["calendarId"] == "primary"
    assert service.calls[0][1]["maxResults"] == 5
    assert service.calls[0][1]["timeMin"] == "2026-09-24T00:00:00-07:00"


@pytest.mark.asyncio
async def test_list_calendar_events_rejects_a_bad_cap_before_oauth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Argument errors do not open a browser."""
    monkeypatch.setattr(google_auth, "build_service", MagicMock(side_effect=AssertionError("oauth")))
    result = await ListCalendarEvents()(_deps(tmp_path), max_results=True)
    assert result == {"error": "max_results must be an integer from 1 to 20"}


@pytest.mark.asyncio
async def test_create_calendar_event_inserts_attendees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Title, times, and attendees become a primary-calendar insert."""
    service = _Api([{"id": "evt2", "htmlLink": "https://calendar.example/evt2"}])
    _use_service(monkeypatch, service)
    result = await CreateCalendarEvent()(
        _deps(tmp_path),
        title="Sync",
        start="2026-09-23T15:00:00-07:00",
        end="2026-09-23T16:00:00-07:00",
        location="Lab",
        attendees=["ada@example.com", "grace@example.com"],
    )
    assert result["event_id"] == "evt2"
    assert result["html_link"] == "https://calendar.example/evt2"
    body = service.calls[0][1]["body"]
    assert body["summary"] == "Sync"
    assert body["attendees"] == [{"email": "ada@example.com"}, {"email": "grace@example.com"}]
    assert body["location"] == "Lab"


@pytest.mark.asyncio
async def test_create_calendar_event_rejects_a_bad_attendee(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An invite without an email address is rejected before the API call."""
    monkeypatch.setattr(google_auth, "build_service", MagicMock(side_effect=AssertionError("oauth")))
    result = await CreateCalendarEvent()(
        _deps(tmp_path),
        title="Sync",
        start="2026-09-23T15:00:00Z",
        end="2026-09-23T16:00:00Z",
        attendees="not-an-email",
    )
    assert result == {"error": "attendees must be an email address"}


@pytest.mark.asyncio
async def test_search_gmail_returns_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Search lists ids and then fetches subject metadata, not full bodies."""
    service = _Api(
        [
            {"messages": [{"id": "abc123"}], "resultSizeEstimate": 4},
            _gmail_payload("hello there"),
        ]
    )
    _use_service(monkeypatch, service)
    result = await SearchGmail()(_deps(tmp_path), query="from:ada@example.com newer_than:7d", max_results=3)
    assert result["result_size_estimate"] == 4
    assert result["messages"] == [
        {
            "id": "abc123",
            "thread_id": "thr1",
            "subject": "Hello",
            "from": "Ada <ada@example.com>",
            "date": "Wed, 23 Sep 2026 12:00:00 +0000",
            "snippet": "short snippet",
        }
    ]
    assert service.calls[0][1]["q"] == "from:ada@example.com newer_than:7d"
    assert service.calls[1] == (
        "get",
        {"userId": "me", "id": "abc123", "format": "metadata", "metadataHeaders": ["Subject", "From", "Date"]},
    )


@pytest.mark.asyncio
async def test_get_gmail_message_truncates_and_rejects_a_bad_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The body handed back to the model is truncated, and ids are checked first."""
    service = _Api([_gmail_payload("b" * (MAX_EMAIL_BODY_CHARS + 80))])
    _use_service(monkeypatch, service)
    result = await GetGmailMessage()(_deps(tmp_path), message_id="abc123")
    assert result["body"].endswith("…[truncated]")
    assert await GetGmailMessage()(_deps(tmp_path), message_id="not an id") == {
        "error": "message_id must be a Gmail message id"
    }


@pytest.mark.asyncio
async def test_create_gmail_draft_does_not_send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A draft is created from the validated message and is not a send."""
    service = _Api([{"id": "draft1", "message": {"id": "msg1"}}])
    _use_service(monkeypatch, service)
    result = await CreateGmailDraft()(
        _deps(tmp_path),
        to="ada@example.com, grace@example.com",
        subject="Running late",
        body="I'll be five minutes late.",
    )
    assert result["draft_id"] == "draft1"
    assert result["message_id"] == "msg1"
    assert service.calls[0][0] == "create"
    raw = service.calls[0][1]["body"]["message"]["raw"]
    decoded = base64.urlsafe_b64decode(raw.encode("ascii")).decode()
    assert "To: ada@example.com, grace@example.com" in decoded
    assert "Subject: Running late" in decoded
    assert "I'll be five minutes late." in decoded
    assert await CreateGmailDraft()(_deps(tmp_path), to="", subject="Hi", body="Hello") == {
        "error": "to must be a non-empty string"
    }


@pytest.mark.asyncio
async def test_send_gmail_sends_only_through_the_send_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """send_gmail calls messages.send and still rejects a missing body."""
    service = _Api([{"id": "sent1"}])
    _use_service(monkeypatch, service)
    result = await SendGmail()(
        _deps(tmp_path),
        to="ada@example.com",
        subject="On my way",
        body="<p>Leaving now</p>",
        html=True,
    )
    assert result == {"sent": True, "message_id": "sent1", "to": "ada@example.com", "subject": "On my way"}
    assert service.calls[0][0] == "send"
    raw = service.calls[0][1]["body"]["raw"]
    decoded = base64.urlsafe_b64decode(raw.encode("ascii")).decode()
    assert "Content-Type: text/html" in decoded
    assert await SendGmail()(_deps(tmp_path), to="ada@example.com", subject="Hi", body="  ") == {
        "error": "body must be a non-empty string"
    }


@pytest.mark.asyncio
async def test_google_api_errors_stay_inside_the_tool_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """API failures become an error dict, including a hint to consent again for missing scopes."""
    service = _Api([])
    service.execute_error = RuntimeError("insufficient authentication scopes")
    _use_service(monkeypatch, service)
    result = await ListCalendarEvents()(
        _deps(tmp_path),
        start="2026-09-24T00:00:00+00:00",
        end="2026-09-25T00:00:00+00:00",
    )
    assert result["error"].startswith("RuntimeError")
    assert "consent again" in result["error"]


@pytest.mark.asyncio
async def test_missing_google_libraries_are_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool tells the user to install the extra instead of raising."""
    monkeypatch.setattr(google_auth, "libraries_installed", lambda: False)
    result = await ListCalendarEvents()(_deps(tmp_path), start="today")
    assert "uv sync --extra google" in result["error"]


@pytest.mark.asyncio
async def test_missing_client_is_reported_without_opening_a_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool call explains which env vars to set when no Desktop client is configured."""
    _clear_client_env(monkeypatch)
    monkeypatch.setattr(google_auth, "libraries_installed", lambda: True)
    monkeypatch.setattr(google_auth, "_CREDENTIALS_MODULE", _credentials_module(_Creds()))
    monkeypatch.setattr(google_auth, "_REQUESTS_MODULE", _requests_module())
    monkeypatch.setattr(google_auth, "_FLOW_MODULE", _flow_module(_Flow(_Creds(valid=True))))
    monkeypatch.setattr(google_auth, "_DISCOVERY_MODULE", SimpleNamespace(build=MagicMock()))

    result = await SearchGmail()(_deps(tmp_path), query="in:inbox")
    assert "GOOGLE_OAUTH_CLIENT_ID" in result["error"]
    assert "browser consent" in result["error"]


def test_load_credentials_refreshes_then_consents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A saved token is refreshed, and a missing or scope-short token opens the browser once."""
    _clear_client_env(monkeypatch)
    monkeypatch.setenv(google_auth.CLIENT_ID_ENV, "client-id")
    monkeypatch.setenv(google_auth.CLIENT_SECRET_ENV, "client-secret")
    monkeypatch.setattr(google_auth, "_REQUESTS_MODULE", _requests_module())

    expired = _Creds(valid=False, expired=True)
    token = tmp_path / "google_oauth_token.json"
    token.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(google_auth, "_CREDENTIALS_MODULE", _credentials_module(expired))
    monkeypatch.setattr(google_auth, "_FLOW_MODULE", _flow_module(_Flow(_Creds(valid=True))))
    loaded = load_credentials(tmp_path)
    assert loaded is expired
    assert expired.refresh_calls == 1
    assert token.read_text(encoding="utf-8") == '{"token": "saved"}'

    fresh = _Creds(valid=True)
    flow = _Flow(fresh)
    monkeypatch.setattr(google_auth, "_CREDENTIALS_MODULE", _credentials_module(_Creds(valid=True, scopes_ok=False)))
    monkeypatch.setattr(google_auth, "_FLOW_MODULE", _flow_module(flow))
    assert load_credentials(tmp_path) is fresh
    assert flow.kwargs["prompt"] == "consent"
    assert flow.kwargs["open_browser"] is True


def test_load_credentials_reports_an_unreadable_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A corrupt token file tells the user to delete it instead of crashing."""

    class _BrokenCredentials:
        @staticmethod
        def from_authorized_user_file(path: str, scopes: list[str]) -> _Creds:
            raise ValueError("bad token")

    token = tmp_path / "google_oauth_token.json"
    token.write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(google_auth, "_CREDENTIALS_MODULE", SimpleNamespace(Credentials=_BrokenCredentials))
    monkeypatch.setattr(google_auth, "_REQUESTS_MODULE", _requests_module())
    monkeypatch.setattr(google_auth, "_FLOW_MODULE", _flow_module(_Flow(_Creds(valid=True))))
    with pytest.raises(GoogleAuthError, match="could not be read"):
        load_credentials(tmp_path)


def test_load_credentials_reports_missing_libraries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The helper fails clearly when the google extra is not installed."""
    monkeypatch.setattr(google_auth, "_CREDENTIALS_MODULE", None)
    monkeypatch.setattr(google_auth, "_REQUESTS_MODULE", None)
    monkeypatch.setattr(google_auth, "_FLOW_MODULE", None)
    with pytest.raises(GoogleLibrariesMissingError, match="uv sync --extra google"):
        load_credentials(tmp_path)


def test_load_credentials_reports_missing_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No token and no client config raises before any browser flow."""
    _clear_client_env(monkeypatch)
    monkeypatch.setattr(google_auth, "_CREDENTIALS_MODULE", _credentials_module(_Creds()))
    monkeypatch.setattr(google_auth, "_REQUESTS_MODULE", _requests_module())
    flow_api = _FlowApi(_Flow(_Creds(valid=True)))
    monkeypatch.setattr(google_auth, "_FLOW_MODULE", SimpleNamespace(InstalledAppFlow=flow_api))
    with pytest.raises(GoogleNotConfiguredError, match="GOOGLE_OAUTH_CLIENT_ID"):
        load_credentials(tmp_path)
    assert flow_api.config is None
