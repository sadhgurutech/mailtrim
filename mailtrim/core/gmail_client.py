"""Gmail API client — OAuth2, message CRUD, labels, search, batch operations.

Rate limits (free Gmail):
  - 250 quota units / user / second
  - messages.get = 5 units, messages.list = 5 units, messages.delete = 5 units
  - Batch requests share the same quota but reduce HTTP overhead significantly

This module adds automatic retry with exponential backoff on 429 / 5xx responses.
"""

from __future__ import annotations

import base64
import functools
import logging
import time
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from mailtrim.config import CREDENTIALS_PATH, TOKEN_PATH, get_settings

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

# ── Retry decorator ──────────────────────────────────────────────────────────

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _with_retry(max_attempts: int = 4, base_delay: float = 1.0) -> Callable[[F], F]:
    """
    Decorator: retry on transient Gmail API errors (429, 5xx) with
    exponential backoff. Raises on non-retryable errors immediately.
    """

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            delay = base_delay
            for attempt in range(max_attempts):
                try:
                    return fn(*args, **kwargs)
                except HttpError as exc:
                    if exc.status_code not in _RETRYABLE_STATUS or attempt == max_attempts - 1:
                        raise
                    logger.warning(
                        "Gmail API %s on attempt %d/%d — retrying in %.1fs",
                        exc.status_code,
                        attempt + 1,
                        max_attempts,
                        delay,
                    )
                    time.sleep(delay)
                    delay *= 2

        return wrapper  # type: ignore[return-value]

    return decorator


# ── Data models ──────────────────────────────────────────────────────────────


@dataclass
class MessageHeader:
    subject: str = ""
    from_: str = ""
    to: str = ""
    cc: str = ""
    date: str = ""
    message_id: str = ""
    list_unsubscribe: str = ""
    list_unsubscribe_post: str = ""


@dataclass
class Message:
    id: str
    thread_id: str
    label_ids: list[str]
    snippet: str
    headers: MessageHeader
    body_text: str = ""
    body_html: str = ""
    size_estimate: int = 0
    internal_date: int = 0  # milliseconds since epoch
    raw_payload: dict = field(default_factory=dict)

    @property
    def timestamp(self) -> float:
        return self.internal_date / 1000

    @property
    def sender_email(self) -> str:
        addr = self.headers.from_
        if "<" in addr and ">" in addr:
            return addr[addr.index("<") + 1 : addr.index(">")].strip().lower()
        return addr.strip().lower()

    @property
    def sender_name(self) -> str:
        addr = self.headers.from_
        if "<" in addr:
            return addr[: addr.index("<")].strip().strip('"')
        return addr.strip()

    @property
    def is_unread(self) -> bool:
        return "UNREAD" in self.label_ids

    @property
    def is_inbox(self) -> bool:
        return "INBOX" in self.label_ids


@dataclass
class Thread:
    id: str
    messages: list[Message]
    snippet: str = ""

    @property
    def latest(self) -> Message | None:
        return self.messages[-1] if self.messages else None


# ── OAuth helpers ────────────────────────────────────────────────────────────


def authenticate(
    credentials_path: Path = CREDENTIALS_PATH,
    token_path: Path = TOKEN_PATH,
) -> Credentials:
    """
    Run OAuth2 flow (opens browser on first run) and return valid credentials.
    The token file is written with mode 0o600 (owner read/write only).
    """
    settings = get_settings()
    scopes = settings.gmail_scopes
    creds: Credentials | None = None

    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), scopes)
        except Exception as exc:
            # Corrupted or unreadable token — delete and re-authenticate.
            logger.warning("Token file is corrupted (%s), re-authenticating.", exc)
            token_path.unlink(missing_ok=True)
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:
                logger.warning("Token refresh failed (%s), re-authenticating.", exc)
                token_path.unlink(missing_ok=True)
                creds = None

        if not creds:
            if not credentials_path.exists():
                raise FileNotFoundError(
                    f"OAuth credentials file not found at {credentials_path}.\n"
                    "Download it from Google Cloud Console → APIs & Services → Credentials.\n"
                    "See README.md for step-by-step setup."
                )
            # Restrict the credentials file too — it contains the OAuth client secret.
            # Do this before reading so the secret is protected even on first run.
            try:
                credentials_path.chmod(0o600)
            except OSError:
                logger.warning(
                    "Could not set permissions on %s — ensure only you can read it.",
                    credentials_path,
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), scopes)
            creds = flow.run_local_server(port=0)

        token_path.write_text(creds.to_json())
        # Restrict permissions: owner read/write only — tokens are sensitive
        token_path.chmod(0o600)

    return creds


# ── Main client ──────────────────────────────────────────────────────────────


class GmailClient:
    """High-level Gmail API wrapper with batching, retry, and pagination."""

    def __init__(self, creds: Credentials | None = None):
        if creds is None:
            creds = authenticate()
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self._user = "me"

    # ── Message retrieval ────────────────────────────────────────────────────

    @_with_retry()
    def list_message_ids(
        self,
        query: str = "",
        label_ids: list[str] | None = None,
        max_results: int | None = None,
    ) -> list[str]:
        """Return message IDs matching query/labels. Paginates automatically."""
        ids: list[str] = []
        params: dict[str, Any] = {"userId": self._user, "maxResults": 500}
        if query:
            params["q"] = query
        if label_ids:
            params["labelIds"] = label_ids

        while True:
            resp = self._service.users().messages().list(**params).execute()
            for msg in resp.get("messages", []):
                ids.append(msg["id"])
                if max_results and len(ids) >= max_results:
                    return ids
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
            params["pageToken"] = page_token

        return ids

    @_with_retry()
    def get_message(self, message_id: str, format: str = "full") -> Message:
        """Fetch a single message and parse it into a Message dataclass."""
        raw = (
            self._service.users()
            .messages()
            .get(userId=self._user, id=message_id, format=format)
            .execute()
        )
        return self._parse_message(raw)

    def get_messages_batch(self, message_ids: list[str]) -> list[Message]:
        """Fetch multiple messages efficiently using batch requests."""
        settings = get_settings()
        results: list[Message] = []

        for chunk in _chunks(message_ids, settings.gmail_batch_size):
            results.extend(self._fetch_batch(chunk, format="full"))

        return results

    def _fetch_batch(
        self,
        message_ids: list[str],
        format: str = "full",
        metadata_headers: list[str] | None = None,
    ) -> list[Message]:
        """
        Execute a single batch request for a list of message IDs.

        Args:
            format:           "full" or "metadata" (metadata is faster — no body)
            metadata_headers: When format="metadata", which headers to include.
                              Defaults to the standard set for sender aggregation.

        Uses a factory function (_make_callback) to capture the accumulator list
        by reference once, not per-iteration — avoids the closure-over-loop-variable bug.
        """
        if format == "metadata" and metadata_headers is None:
            metadata_headers = ["From", "Subject", "Date", "List-Unsubscribe"]

        batch = self._service.new_batch_http_request()
        batch_results: list[Message] = []

        def _make_callback(acc: list) -> Callable:
            def _cb(request_id: str, response: dict, exception: Any) -> None:
                if exception:
                    logger.debug("Batch item error: %s", exception)
                    return
                if response:
                    acc.append(self._parse_message(response))

            return _cb

        callback = _make_callback(batch_results)
        for mid in message_ids:
            kwargs: dict[str, Any] = {"userId": self._user, "id": mid, "format": format}
            if format == "metadata" and metadata_headers:
                kwargs["metadataHeaders"] = metadata_headers
            batch.add(
                self._service.users().messages().get(**kwargs),
                callback=callback,
            )
        batch.execute()
        return batch_results

    @_with_retry()
    def get_thread(self, thread_id: str) -> Thread:
        raw = self._service.users().threads().get(userId=self._user, id=thread_id).execute()
        messages = [self._parse_message(m) for m in raw.get("messages", [])]
        return Thread(id=thread_id, messages=messages, snippet=raw.get("snippet", ""))

    # ── Mutations ────────────────────────────────────────────────────────────

    @_with_retry()
    def archive(self, message_id: str) -> None:
        """Remove INBOX label (archive)."""
        self._modify_labels(message_id, remove=["INBOX"])

    @_with_retry()
    def trash(self, message_id: str) -> None:
        self._service.users().messages().trash(userId=self._user, id=message_id).execute()

    @_with_retry()
    def untrash(self, message_id: str) -> None:
        self._service.users().messages().untrash(userId=self._user, id=message_id).execute()

    def mark_read(self, message_id: str) -> None:
        self._modify_labels(message_id, remove=["UNREAD"])

    def mark_unread(self, message_id: str) -> None:
        self._modify_labels(message_id, add=["UNREAD"])

    def add_label(self, message_id: str, label_id: str) -> None:
        self._modify_labels(message_id, add=[label_id])

    def remove_label(self, message_id: str, label_id: str) -> None:
        self._modify_labels(message_id, remove=[label_id])

    @_with_retry()
    def _modify_labels(
        self,
        message_id: str,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> None:
        body: dict = {}
        if add:
            body["addLabelIds"] = add
        if remove:
            body["removeLabelIds"] = remove
        self._service.users().messages().modify(
            userId=self._user, id=message_id, body=body
        ).execute()

    def batch_archive(self, message_ids: list[str]) -> int:
        """Archive many messages; returns count of successful operations."""
        return self._batch_modify(message_ids, remove=["INBOX"])

    def batch_trash(self, message_ids: list[str]) -> int:
        count = 0
        settings = get_settings()
        for chunk in _chunks(message_ids, settings.gmail_batch_size):
            batch = self._service.new_batch_http_request()
            for mid in chunk:
                batch.add(self._service.users().messages().trash(userId=self._user, id=mid))
            batch.execute()
            count += len(chunk)
        return count

    def batch_delete_permanent(self, message_ids: list[str]) -> int:
        """
        Permanently delete messages — bypasses Trash, CANNOT BE UNDONE.
        Uses messages.delete (not trash) per Gmail API.
        """
        count = 0
        settings = get_settings()
        for chunk in _chunks(message_ids, settings.gmail_batch_size):
            batch = self._service.new_batch_http_request()
            for mid in chunk:
                batch.add(self._service.users().messages().delete(userId=self._user, id=mid))
            batch.execute()
            count += len(chunk)
        return count

    def batch_label(
        self, message_ids: list[str], add: list[str] = (), remove: list[str] = ()
    ) -> int:
        return self._batch_modify(message_ids, add=list(add), remove=list(remove))

    def _batch_modify(
        self,
        message_ids: list[str],
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> int:
        count = 0
        settings = get_settings()
        body: dict = {}
        if add:
            body["addLabelIds"] = add
        if remove:
            body["removeLabelIds"] = remove

        for chunk in _chunks(message_ids, settings.gmail_batch_size):
            batch = self._service.new_batch_http_request()
            for mid in chunk:
                batch.add(
                    self._service.users().messages().modify(userId=self._user, id=mid, body=body)
                )
            batch.execute()
            count += len(chunk)

        return count

    # ── Send / reply ─────────────────────────────────────────────────────────

    @_with_retry()
    def send(self, to: str, subject: str, body: str, thread_id: str | None = None) -> str:
        """Send a plain-text email. Returns the sent message ID."""
        msg = MIMEText(body)
        msg["to"] = to
        msg["subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        payload: dict = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id
        result = self._service.users().messages().send(userId=self._user, body=payload).execute()
        return result["id"]

    # ── Labels ───────────────────────────────────────────────────────────────

    @_with_retry()
    def list_labels(self) -> list[dict]:
        resp = self._service.users().labels().list(userId=self._user).execute()
        return resp.get("labels", [])

    def get_or_create_label(self, name: str, color: dict | None = None) -> str:
        """Return label ID, creating label if it doesn't exist."""
        for lbl in self.list_labels():
            if lbl["name"].lower() == name.lower():
                return lbl["id"]
        body: dict = {
            "name": name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        }
        if color:
            body["color"] = color
        created = self._service.users().labels().create(userId=self._user, body=body).execute()
        return created["id"]

    # ── Profile ──────────────────────────────────────────────────────────────

    @_with_retry()
    def get_profile(self) -> dict:
        return self._service.users().getProfile(userId=self._user).execute()

    def get_email_address(self) -> str:
        return self.get_profile()["emailAddress"]

    def get_storage_used_bytes(self) -> int:
        """Return total mailbox size in bytes from the profile API."""
        profile = self.get_profile()
        # historyId is always present; threadsTotal / messagesTotal are too
        return profile.get("messagesTotal", 0)  # not size — see note below

    # ── Drafts ───────────────────────────────────────────────────────────────

    @_with_retry()
    def create_draft(self, to: str, subject: str, body: str, thread_id: str | None = None) -> str:
        msg = MIMEText(body)
        msg["to"] = to
        msg["subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        message_payload: dict = {"raw": raw}
        if thread_id:
            message_payload["threadId"] = thread_id
        result = (
            self._service.users()
            .drafts()
            .create(userId=self._user, body={"message": message_payload})
            .execute()
        )
        return result["id"]

    # ── Parsing ──────────────────────────────────────────────────────────────

    def _parse_message(self, raw: dict) -> Message:
        headers = _parse_headers(raw.get("payload", {}).get("headers", []))
        body_text, body_html = _extract_body(raw.get("payload", {}))
        return Message(
            id=raw["id"],
            thread_id=raw.get("threadId", ""),
            label_ids=raw.get("labelIds", []),
            snippet=raw.get("snippet", ""),
            headers=headers,
            body_text=body_text,
            body_html=body_html,
            size_estimate=raw.get("sizeEstimate", 0),
            internal_date=int(raw.get("internalDate", 0) or 0),
            raw_payload=raw.get("payload", {}),
        )


# ── Module-level helpers ─────────────────────────────────────────────────────


def _parse_headers(header_list: list[dict]) -> MessageHeader:
    h = {item["name"].lower(): item["value"] for item in header_list}
    return MessageHeader(
        subject=h.get("subject", ""),
        from_=h.get("from", ""),
        to=h.get("to", ""),
        cc=h.get("cc", ""),
        date=h.get("date", ""),
        message_id=h.get("message-id", ""),
        list_unsubscribe=h.get("list-unsubscribe", ""),
        list_unsubscribe_post=h.get("list-unsubscribe-post", ""),
    )


def _extract_body(payload: dict) -> tuple[str, str]:
    """Recursively extract text/plain and text/html from a MIME payload."""
    text = ""
    html = ""
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            text = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    elif mime_type == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            html = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    elif mime_type.startswith("multipart/"):
        for part in payload.get("parts", []):
            t, h = _extract_body(part)
            text = text or t
            html = html or h

    return text, html


def _chunks(lst: list, n: int) -> Iterator[list]:
    for i in range(0, len(lst), n):
        yield lst[i : i + n]
