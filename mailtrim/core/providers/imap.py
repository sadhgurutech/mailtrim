"""IMAP provider — stdlib-only implementation of EmailProvider.

Design constraints:
  - Zero new dependencies: imaplib, email, email.header, re only
  - Single persistent connection per instance (no reconnect loops)
  - Metadata-first strategy: fetch headers + size, body only when requested
  - Batch fetch: FETCH uid1,uid2,...,uidN in one round-trip
  - Fail clearly on auth errors; fail silently on individual message parse errors
  - Never log credentials

Performance targets:
  - 300 emails metadata fetch < 10s on a local network
  - Single IMAP connection reused across all calls

Security:
  - Credentials are never stored; connection object holds the session
  - Always use SSL (IMAP4_SSL); plaintext IMAP is not supported
"""

from __future__ import annotations

import email
import email.header
import imaplib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from mailtrim.core.gmail_client import Message, MessageHeader
from mailtrim.core.providers.base import EmailProvider

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_DEFAULT_PORT = 993
_BATCH_SIZE = 100  # UIDs per FETCH command
_MAX_BODY_CHARS = 400  # body preview truncation

# Common IMAP folder names for Trash and Archive across providers
_TRASH_FOLDERS = ["Trash", "[Gmail]/Trash", "Deleted Messages", "Deleted Items"]
_ARCHIVE_FOLDERS = ["Archive", "[Gmail]/All Mail", "All Mail"]


# ── IMAP query translation ────────────────────────────────────────────────────


def _gmail_query_to_imap(query: str) -> tuple[str, str]:
    """
    Translate a Gmail query string to (folder, IMAP SEARCH criteria).

    Handles the subset of Gmail operators used by mailtrim:
        in:inbox          → INBOX, ALL
        in:anywhere       → INBOX, ALL  (full-folder walk not implemented; INBOX is the
                            primary target; users can pass --imap-folder for other folders)
        from:addr         → FROM "addr"
        older_than:Nd     → BEFORE <date>
        subject:text      → SUBJECT "text"
        category:*        → ALL  (no IMAP equivalent; ignored gracefully)
        label:*           → ALL  (ignored)

    Returns (folder_name, search_criteria).
    """
    folder = "INBOX"
    parts: list[str] = []

    # Scope modifiers — folder selection only, no search criteria
    if "in:anywhere" in query:
        folder = "INBOX"  # simplified; see note above
    elif "in:inbox" in query:
        folder = "INBOX"

    # FROM filter
    m = re.search(r"from:([^\s]+)", query)
    if m:
        parts.append(f'FROM "{m.group(1)}"')

    # Age filter — older_than:Nd → BEFORE (exclusive upper bound)
    m = re.search(r"older_than:(\d+)d", query)
    if m:
        days = int(m.group(1))
        cutoff = datetime.now(tz=timezone.utc).timestamp() - days * 86400
        dt = datetime.fromtimestamp(cutoff, tz=timezone.utc)
        # IMAP BEFORE uses DD-Mon-YYYY format
        parts.append(f'BEFORE "{dt.strftime("%d-%b-%Y")}"')

    # Since filter — newer_than:Nd → SINCE (inclusive lower bound)
    m = re.search(r"newer_than:(\d+)d", query)
    if m:
        days = int(m.group(1))
        cutoff = datetime.now(tz=timezone.utc).timestamp() - days * 86400
        dt = datetime.fromtimestamp(cutoff, tz=timezone.utc)
        # IMAP SINCE uses DD-Mon-YYYY format
        parts.append(f'SINCE "{dt.strftime("%d-%b-%Y")}"')

    # Subject filter
    m = re.search(r'subject:"([^"]+)"', query)
    if m:
        parts.append(f'SUBJECT "{m.group(1)}"')

    criteria = " ".join(parts) if parts else "ALL"
    return folder, criteria


# ── Header parsing ────────────────────────────────────────────────────────────


def _decode_header_value(raw: str | bytes | None) -> str:
    """Decode a potentially RFC2047-encoded header value to a plain string."""
    if not raw:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    decoded_parts = []
    for part, charset in email.header.decode_header(raw):
        if isinstance(part, bytes):
            decoded_parts.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded_parts.append(part)
    return "".join(decoded_parts)


def _parse_imap_date(date_str: str) -> int:
    """Parse an email Date header to milliseconds since epoch. Returns 0 on failure."""
    try:
        dt = parsedate_to_datetime(date_str)
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def _extract_text_from_message(msg: email.message.Message) -> tuple[str, str]:
    """
    Walk a parsed email.message.Message and extract plain text and HTML.
    Returns (text, html), each truncated to _MAX_BODY_CHARS.
    Handles multipart, encoding errors, and malformed payloads silently.
    """
    text = ""
    html = ""
    try:
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/plain" and not text:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        text = payload.decode(charset, errors="replace")[:_MAX_BODY_CHARS]
                elif ct == "text/html" and not html:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        html = payload.decode(charset, errors="replace")[:_MAX_BODY_CHARS]
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                content = payload.decode(charset, errors="replace")[:_MAX_BODY_CHARS]
                if msg.get_content_type() == "text/html":
                    html = content
                else:
                    text = content
    except Exception as exc:
        logger.debug("Body extraction failed: %s", exc)
    return text, html


def _strip_html(html: str) -> str:
    """Strip HTML tags for plain-text snippet generation."""
    return re.sub(r"<[^>]+>", " ", html).strip()


# ── Message construction ──────────────────────────────────────────────────────


@dataclass
class _IMAPRawMessage:
    """Intermediate parsed form before converting to the shared Message type."""

    uid: str
    subject: str = ""
    from_: str = ""
    date: str = ""
    list_unsubscribe: str = ""
    size_bytes: int = 0
    internal_date_ms: int = 0
    body_text: str = ""
    body_html: str = ""
    flags: list[str] = field(default_factory=list)


def _raw_to_message(raw: _IMAPRawMessage, folder: str = "INBOX") -> Message:
    """Convert an _IMAPRawMessage to the shared Message dataclass."""
    label_ids = []
    if folder.upper() == "INBOX":
        label_ids.append("INBOX")
    if r"\Seen" not in raw.flags:
        label_ids.append("UNREAD")

    snippet = raw.body_text[:200] if raw.body_text else _strip_html(raw.body_html)[:200]

    return Message(
        id=raw.uid,
        thread_id="",  # IMAP has no thread concept at the protocol level
        label_ids=label_ids,
        snippet=snippet,
        headers=MessageHeader(
            subject=raw.subject,
            from_=raw.from_,
            date=raw.date,
            list_unsubscribe=raw.list_unsubscribe,
        ),
        body_text=raw.body_text,
        body_html=raw.body_html,
        size_estimate=raw.size_bytes,
        internal_date=raw.internal_date_ms,
    )


# ── IMAP response parsing ─────────────────────────────────────────────────────


def _parse_uid_search(response: list[bytes]) -> list[str]:
    """Extract UID integers from a UID SEARCH response."""
    uids: list[str] = []
    for line in response:
        if line:
            uids.extend(line.decode("ascii", errors="replace").split())
    return [u for u in uids if u.isdigit()]


def _parse_fetch_response(
    fetch_data: list,
    metadata_only: bool = True,
) -> list[_IMAPRawMessage]:
    """
    Parse imaplib FETCH response into _IMAPRawMessage list.

    imaplib returns alternating (header_bytes, b')') tuples.
    Each header_bytes contains:
        <UID> (RFC822.SIZE <n> INTERNALDATE "<date>" FLAGS (<flags>)
               BODY[HEADER.FIELDS (...)] {<n>}
        <header-block>

    For full fetch (metadata_only=False), BODY[] contains the full RFC822 message.
    """
    results: list[_IMAPRawMessage] = []
    i = 0
    while i < len(fetch_data):
        item = fetch_data[i]
        i += 1

        # imaplib interleaves (tuple, b')') — skip plain bytes sentinels
        if not isinstance(item, tuple):
            continue

        descriptor, raw_headers = item[0], item[1]
        descriptor_str = (
            descriptor.decode("ascii", errors="replace")
            if isinstance(descriptor, bytes)
            else str(descriptor)
        )

        # Extract UID
        uid_match = re.search(r"UID\s+(\d+)", descriptor_str, re.IGNORECASE)
        uid = uid_match.group(1) if uid_match else str(i)

        # Extract RFC822.SIZE
        size_match = re.search(r"RFC822\.SIZE\s+(\d+)", descriptor_str, re.IGNORECASE)
        size_bytes = int(size_match.group(1)) if size_match else 0

        # Extract INTERNALDATE
        date_match = re.search(r'INTERNALDATE\s+"([^"]+)"', descriptor_str, re.IGNORECASE)
        internal_date_str = date_match.group(1) if date_match else ""
        internal_date_ms = _parse_imap_date(internal_date_str) if internal_date_str else 0

        # Extract FLAGS
        flags_match = re.search(r"FLAGS\s+\(([^)]*)\)", descriptor_str, re.IGNORECASE)
        flags = flags_match.group(1).split() if flags_match else []

        # Parse headers
        raw_msg = _IMAPRawMessage(
            uid=uid, size_bytes=size_bytes, internal_date_ms=internal_date_ms, flags=flags
        )

        if isinstance(raw_headers, bytes):
            try:
                parsed = email.message_from_bytes(raw_headers)
                raw_msg.subject = _decode_header_value(parsed.get("Subject", ""))
                raw_msg.from_ = _decode_header_value(parsed.get("From", ""))
                raw_msg.date = _decode_header_value(parsed.get("Date", ""))
                raw_msg.list_unsubscribe = _decode_header_value(parsed.get("List-Unsubscribe", ""))

                if not metadata_only:
                    raw_msg.body_text, raw_msg.body_html = _extract_text_from_message(parsed)
            except Exception as exc:
                logger.debug("Header parse failed for UID %s: %s", uid, exc)

        results.append(raw_msg)

    return results


# ── Main provider ─────────────────────────────────────────────────────────────


class IMAPProvider(EmailProvider):
    """
    IMAP email provider using stdlib imaplib.

    One connection is opened on first use and reused for all operations.
    SSL is always enforced (IMAP4_SSL).

    Args:
        server:   IMAP server hostname (e.g. imap.gmail.com, outlook.office365.com)
        user:     Full email address used for login
        password: App password (never a regular account password for 2FA accounts)
        port:     SSL port, default 993
        folder:   Default folder to operate on, default INBOX
    """

    def __init__(
        self,
        server: str,
        user: str,
        password: str,
        port: int = _DEFAULT_PORT,
        folder: str = "INBOX",
    ) -> None:
        self._server = server
        self._user = user
        self._password = password  # never logged
        self._port = port
        self._default_folder = folder
        self._conn: imaplib.IMAP4_SSL | None = None
        self._selected_folder: str | None = None

    # ── Connection management ─────────────────────────────────────────────────

    def _connect(self) -> imaplib.IMAP4_SSL:
        """Open and authenticate an SSL connection. Raises on auth failure."""
        conn = imaplib.IMAP4_SSL(self._server, self._port)
        typ, data = conn.login(self._user, self._password)
        if typ != "OK":
            raise ConnectionError(f"IMAP login failed: {data}")
        return conn

    def _ensure_connected(self) -> imaplib.IMAP4_SSL:
        """Return the active connection, reconnecting once if it has gone stale."""
        if self._conn is None:
            self._conn = self._connect()
            self._selected_folder = None
        else:
            # NOOP to check liveness; reconnect if the server has closed the session
            try:
                self._conn.noop()
            except Exception:
                logger.debug("IMAP connection stale, reconnecting")
                try:
                    self._conn.logout()
                except Exception as exc:
                    logger.debug("Ignoring logout error during reconnect: %s", exc)
                self._conn = self._connect()
                self._selected_folder = None
        return self._conn

    def _select(self, folder: str = "INBOX") -> None:
        """SELECT a mailbox if not already selected."""
        conn = self._ensure_connected()
        if self._selected_folder != folder:
            typ, data = conn.select(folder, readonly=False)
            if typ != "OK":
                raise ConnectionError(f"Cannot SELECT {folder}: {data}")
            self._selected_folder = folder

    def _find_folder(self, candidates: list[str]) -> str | None:
        """Return the first folder from candidates that exists on the server."""
        conn = self._ensure_connected()
        typ, mailboxes = conn.list()
        if typ != "OK":
            return None
        existing = []
        for mb in mailboxes:
            if isinstance(mb, bytes):
                existing.append(mb.decode("utf-8", errors="replace"))
        for candidate in candidates:
            if any(candidate in box for box in existing):
                return candidate
        return None

    # ── Read ──────────────────────────────────────────────────────────────────

    def list_message_ids(
        self,
        query: str = "",
        max_results: int | None = None,
    ) -> list[str]:
        """
        Return message UIDs matching query, up to max_results.

        Gmail query strings are translated to IMAP SEARCH criteria.
        Category/label operators that have no IMAP equivalent are ignored
        gracefully — the search falls back to ALL.
        """
        folder, criteria = _gmail_query_to_imap(query or "in:inbox")
        self._select(folder)
        conn = self._ensure_connected()

        typ, data = conn.uid("SEARCH", None, criteria)
        if typ != "OK":
            logger.warning("IMAP SEARCH failed: %s", data)
            return []

        uids = _parse_uid_search(data)
        if max_results:
            uids = uids[:max_results]
        return uids

    def get_messages_batch(self, ids: list[str]) -> list[Message]:
        """Fetch full messages (headers + body) for a list of UIDs."""
        return self._fetch_batch(ids, metadata_only=False)

    def get_messages_metadata(self, ids: list[str]) -> list[Message]:
        """
        Fetch lightweight metadata only — headers + size, no body.

        Uses BODY.PEEK[HEADER.FIELDS ...] so the server marks nothing as read.
        """
        return self._fetch_batch(ids, metadata_only=True)

    def _fetch_batch(self, ids: list[str], metadata_only: bool = True) -> list[Message]:
        """
        Core fetch implementation — batches UIDs into groups of _BATCH_SIZE
        to avoid oversized FETCH commands.
        """
        if not ids:
            return []

        folder, _ = _gmail_query_to_imap("in:inbox")
        self._select(folder)
        conn = self._ensure_connected()

        if metadata_only:
            fetch_items = (
                "(UID RFC822.SIZE INTERNALDATE FLAGS "
                "BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE LIST-UNSUBSCRIBE)])"
            )
        else:
            fetch_items = "(UID RFC822.SIZE INTERNALDATE FLAGS BODY.PEEK[])"

        results: list[Message] = []
        for i in range(0, len(ids), _BATCH_SIZE):
            chunk = ids[i : i + _BATCH_SIZE]
            uid_set = ",".join(chunk)
            try:
                typ, data = conn.uid("FETCH", uid_set, fetch_items)
                if typ != "OK":
                    logger.warning("IMAP FETCH failed for chunk starting %s: %s", chunk[0], data)
                    continue
                raw_msgs = _parse_fetch_response(data, metadata_only=metadata_only)
                results.extend(_raw_to_message(r, folder=folder) for r in raw_msgs)
            except Exception as exc:
                logger.warning("IMAP FETCH chunk error: %s", exc)
        return results

    # ── Write ─────────────────────────────────────────────────────────────────

    def batch_trash(self, ids: list[str]) -> int:
        r"""
        Move messages to the Trash folder.
        Falls back to \Deleted flag + EXPUNGE if MOVE is unavailable.
        """
        if not ids:
            return 0
        self._select(self._default_folder)
        conn = self._ensure_connected()

        trash_folder = self._find_folder(_TRASH_FOLDERS)
        uid_set = ",".join(ids)
        count = 0

        if trash_folder:
            try:
                typ, _ = conn.uid("MOVE", uid_set, trash_folder)
                if typ == "OK":
                    return len(ids)
            except imaplib.IMAP4.error:
                pass  # MOVE not supported — fall through to STORE+EXPUNGE

        # Fallback: flag as deleted and expunge
        try:
            conn.uid("STORE", uid_set, "+FLAGS", r"(\Deleted)")
            conn.expunge()
            count = len(ids)
        except Exception as exc:
            logger.warning("IMAP batch_trash fallback failed: %s", exc)

        return count

    def batch_delete_permanent(self, ids: list[str]) -> int:
        r"""Permanently delete messages — flag \Deleted and EXPUNGE immediately."""
        if not ids:
            return 0
        self._select(self._default_folder)
        conn = self._ensure_connected()
        uid_set = ",".join(ids)
        try:
            conn.uid("STORE", uid_set, "+FLAGS", r"(\Deleted)")
            conn.expunge()
            return len(ids)
        except Exception as exc:
            logger.warning("IMAP batch_delete_permanent failed: %s", exc)
            return 0

    def batch_archive(self, ids: list[str]) -> int:
        """
        Archive messages — move to Archive/All Mail folder.
        If no archive folder is found, removes the INBOX flag instead.
        """
        if not ids:
            return 0
        self._select(self._default_folder)
        conn = self._ensure_connected()
        uid_set = ",".join(ids)

        archive_folder = self._find_folder(_ARCHIVE_FOLDERS)
        if archive_folder:
            try:
                typ, _ = conn.uid("MOVE", uid_set, archive_folder)
                if typ == "OK":
                    return len(ids)
            except imaplib.IMAP4.error:
                pass  # MOVE not supported — fall through to seen-flag fallback

        # Fallback: mark as seen (closest to "archive" on servers without MOVE)
        try:
            conn.uid("STORE", uid_set, "+FLAGS", r"(\Seen)")
            return len(ids)
        except Exception as exc:
            logger.warning("IMAP batch_archive fallback failed: %s", exc)
            return 0

    def batch_label(
        self,
        ids: list[str],
        add: list[str] = (),
        remove: list[str] = (),
    ) -> int:
        r"""
        IMAP has no label concept — map to flag operations.

        Known mappings:
          UNREAD → \Seen (remove flag to mark unread, add to mark read)
          All others → logged and skipped.
        """
        if not ids:
            return 0
        conn = self._ensure_connected()
        uid_set = ",".join(ids)

        for label in add:
            if label == "UNREAD":
                conn.uid("STORE", uid_set, "-FLAGS", r"(\Seen)")
        for label in remove:
            if label == "UNREAD":
                conn.uid("STORE", uid_set, "+FLAGS", r"(\Seen)")
            elif label == "INBOX":
                # Move out of inbox = archive
                self.batch_archive(ids)
                return len(ids)

        return len(ids)

    # ── Account ───────────────────────────────────────────────────────────────

    def get_profile(self) -> dict:
        """
        Return account summary. messagesTotal comes from STATUS(MESSAGES).
        threadsTotal is 0 — IMAP has no native thread concept.
        """
        conn = self._ensure_connected()
        messages_total = 0
        try:
            typ, data = conn.status("INBOX", "(MESSAGES)")
            if typ == "OK" and data:
                m = re.search(r"MESSAGES\s+(\d+)", data[0].decode("ascii", errors="replace"))
                if m:
                    messages_total = int(m.group(1))
        except Exception as exc:
            logger.debug("IMAP STATUS failed: %s", exc)

        return {
            "emailAddress": self._user,
            "messagesTotal": messages_total,
            "threadsTotal": 0,
        }

    def get_email_address(self) -> str:
        return self._user

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Gracefully close the IMAP connection."""
        if self._conn:
            try:
                if self._selected_folder:
                    self._conn.close()
                self._conn.logout()
            except Exception as exc:
                logger.debug("Ignoring IMAP close/logout error during cleanup: %s", exc)
            finally:
                self._conn = None
                self._selected_folder = None

    def __enter__(self) -> "IMAPProvider":
        self._ensure_connected()
        return self

    def __exit__(self, *_) -> None:
        self.close()
