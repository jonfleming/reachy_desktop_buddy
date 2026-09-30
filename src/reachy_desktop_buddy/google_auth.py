"""Desktop OAuth for the local Google Calendar and Gmail tools."""

import os
import sys
import json
import asyncio
import logging
import importlib
import threading
from typing import Any
from pathlib import Path
from collections.abc import Callable

from reachy_desktop_buddy.config import config


logger = logging.getLogger(__name__)

CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
SCOPES: list[str] = [
    CALENDAR_EVENTS_SCOPE,
    GMAIL_READONLY_SCOPE,
    GMAIL_COMPOSE_SCOPE,
    GMAIL_SEND_SCOPE,
]

TOKEN_FILENAME = "google_oauth_token.json"
CLIENT_ID_ENV = "GOOGLE_OAUTH_CLIENT_ID"
CLIENT_SECRET_ENV = "GOOGLE_OAUTH_CLIENT_SECRET"
CLIENT_SECRET_FILE_ENV = "GOOGLE_OAUTH_CLIENT_SECRET_FILE"

MISSING_LIBRARIES_MESSAGE = (
    "Google API libraries are not installed. From the app environment run: uv sync --extra google"
)
MISSING_CLIENT_MESSAGE = (
    "Google OAuth client is not configured. Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET, "
    "or set GOOGLE_OAUTH_CLIENT_SECRET_FILE to a Desktop OAuth client JSON downloaded from Google Cloud. "
    "Then call this tool again and complete one browser consent. "
    "The token stays on this computer."
)

_AUTH_LOCK = threading.Lock()


class GoogleLibrariesMissingError(RuntimeError):
    """The optional google extra is not installed."""


class GoogleNotConfiguredError(RuntimeError):
    """Client id and secret are missing."""


class GoogleAuthError(RuntimeError):
    """Browser consent or token refresh failed."""


def _load_optional(module_name: str) -> Any:
    try:
        return importlib.import_module(module_name)
    except ImportError:
        return None


_CREDENTIALS_MODULE = _load_optional("google.oauth2.credentials")
_REQUESTS_MODULE = _load_optional("google.auth.transport.requests")
_FLOW_MODULE = _load_optional("google_auth_oauthlib.flow")
_DISCOVERY_MODULE = _load_optional("googleapiclient.discovery")


def libraries_installed() -> bool:
    """Return whether the google extra imported successfully."""
    return all(
        module is not None for module in (_CREDENTIALS_MODULE, _REQUESTS_MODULE, _FLOW_MODULE, _DISCOVERY_MODULE)
    )


def user_data_dir() -> Path:
    """Return the per-user directory used when the app has no instance path."""
    if sys.platform == "win32":
        local_app_data = os.getenv("LOCALAPPDATA", "").strip()
        if local_app_data:
            return Path(local_app_data) / "reachy_desktop_buddy"
        return Path.home() / "AppData" / "Local" / "reachy_desktop_buddy"
    data_home = os.getenv("XDG_DATA_HOME", "").strip()
    if data_home:
        return Path(data_home).expanduser() / "reachy_desktop_buddy"
    return Path.home() / ".local" / "share" / "reachy_desktop_buddy"


def token_path(instance_path: str | Path | None) -> Path:
    """Return the local OAuth token path for this app instance."""
    if instance_path is not None:
        return Path(instance_path).expanduser() / TOKEN_FILENAME
    if config.INSTANCE_PATH is not None:
        return Path(config.INSTANCE_PATH).expanduser() / TOKEN_FILENAME
    return user_data_dir() / TOKEN_FILENAME


def client_config_from_env() -> dict[str, Any] | None:
    """Build an installed-app client config from the environment."""
    secret_file = os.getenv(CLIENT_SECRET_FILE_ENV, "").strip()
    if secret_file:
        path = Path(secret_file).expanduser()
        try:
            loaded: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise GoogleAuthError(f"Could not read {CLIENT_SECRET_FILE_ENV} at {path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise GoogleAuthError(f"{CLIENT_SECRET_FILE_ENV} must be a Desktop OAuth client JSON object.")
        if "installed" not in loaded:
            if "web" in loaded:
                raise GoogleAuthError(
                    f"{CLIENT_SECRET_FILE_ENV} is a web OAuth client. Create a Desktop app client instead."
                )
            raise GoogleAuthError(f"{CLIENT_SECRET_FILE_ENV} must contain an 'installed' Desktop client.")
        return loaded

    client_id = os.getenv(CLIENT_ID_ENV, "").strip()
    client_secret = os.getenv(CLIENT_SECRET_ENV, "").strip()
    if not client_id or not client_secret:
        return None
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def _symbol(module: Any, name: str) -> Any:
    if module is None:
        return None
    return getattr(module, name, None)


def _save_token(path: Path, credentials: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(credentials.to_json(), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError as exc:
        logger.warning("Could not restrict permissions on %s: %s", path, exc)


def load_credentials(instance_path: str | Path | None) -> Any:
    """Load a saved token, refresh it, or run one desktop browser consent."""
    credentials_cls = _symbol(_CREDENTIALS_MODULE, "Credentials")
    request_cls = _symbol(_REQUESTS_MODULE, "Request")
    flow_cls = _symbol(_FLOW_MODULE, "InstalledAppFlow")
    if credentials_cls is None or request_cls is None or flow_cls is None:
        raise GoogleLibrariesMissingError(MISSING_LIBRARIES_MESSAGE)

    path = token_path(instance_path)
    with _AUTH_LOCK:
        credentials = None
        if path.is_file():
            try:
                credentials = credentials_cls.from_authorized_user_file(str(path), SCOPES)
            except Exception as exc:
                logger.warning("Failed to read Google token %s: %s", path, type(exc).__name__)
                raise GoogleAuthError(
                    f"Saved Google token at {path} could not be read. Delete that file and consent again."
                ) from exc
            if not credentials.has_scopes(SCOPES):
                logger.info("Saved Google token is missing scopes; requesting consent again")
                credentials = None

        if credentials is not None and credentials.valid:
            return credentials

        if credentials is not None and credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(request_cls())
            except Exception as exc:
                logger.warning("Google token refresh failed: %s", type(exc).__name__)
                raise GoogleAuthError(
                    f"Saved Google token at {path} could not be refreshed. Delete that file and consent again."
                ) from exc
            _save_token(path, credentials)
            return credentials

        client_config = client_config_from_env()
        if client_config is None:
            raise GoogleNotConfiguredError(MISSING_CLIENT_MESSAGE)

        logger.info("Opening a browser for Google OAuth consent; token will be saved at %s", path)
        flow = flow_cls.from_client_config(client_config, SCOPES)
        try:
            credentials = flow.run_local_server(port=0, open_browser=True, prompt="consent")
        except Exception as exc:
            logger.warning("Google browser consent failed: %s", type(exc).__name__)
            raise GoogleAuthError(
                "Google browser consent did not finish. Allow a desktop browser to open, then try the tool again."
            ) from exc
        if credentials is None:
            raise GoogleAuthError("Google browser consent did not return credentials.")
        _save_token(path, credentials)
        return credentials


def build_service(api: str, version: str, instance_path: str | Path | None) -> Any:
    """Build a Google API client for the desktop OAuth token."""
    discovery_build = _symbol(_DISCOVERY_MODULE, "build")
    if discovery_build is None:
        raise GoogleLibrariesMissingError(MISSING_LIBRARIES_MESSAGE)
    return discovery_build(api, version, credentials=load_credentials(instance_path), cache_discovery=False)


def _public_error(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    if len(text) > 500:
        text = text[:500] + "…[truncated]"
    if "insufficient" in text.lower() and "scope" in text.lower():
        text += " Delete the saved Google token and consent again so Calendar and Gmail scopes are granted."
    return text


async def call_google(
    instance_path: str | Path | None,
    api: str,
    version: str,
    operation: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    """Run one Google API operation off the event loop, returning an error dict on failure."""
    if not libraries_installed():
        logger.warning("%s", MISSING_LIBRARIES_MESSAGE)
        return {"error": MISSING_LIBRARIES_MESSAGE}

    def _run() -> dict[str, Any]:
        service = build_service(api, version, instance_path)
        return operation(service)

    try:
        return await asyncio.to_thread(_run)
    except GoogleLibrariesMissingError as exc:
        logger.warning("%s", exc)
        return {"error": str(exc)}
    except GoogleNotConfiguredError as exc:
        logger.warning("%s", exc)
        return {"error": str(exc)}
    except GoogleAuthError as exc:
        logger.warning("Google OAuth failed: %s", exc)
        return {"error": str(exc)}
    except Exception as exc:
        logger.warning("Google API call failed: %s", _public_error(exc))
        return {"error": _public_error(exc)}
