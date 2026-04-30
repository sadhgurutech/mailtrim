"""Abstract email provider interface.

All pipeline code (stats, purge, bulk) targets this interface.
Provider implementations (Gmail, IMAP) are selected at the CLI boundary
and injected — the pipeline never imports a concrete provider directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from mailtrim.core.gmail_client import Message


class EmailProvider(ABC):
    """
    Minimal interface the mailtrim pipeline requires from any mail backend.

    Concrete implementations: GmailProvider, IMAPProvider.
    Adding a new backend requires implementing these eight methods only —
    no changes to scoring, ranking, or CLI output logic.
    """

    # ── Read ──────────────────────────────────────────────────────────────────

    @abstractmethod
    def list_message_ids(
        self,
        query: str = "",
        max_results: int | None = None,
    ) -> list[str]:
        """Return message IDs matching query, up to max_results."""

    @abstractmethod
    def get_messages_batch(self, ids: list[str]) -> list[Message]:
        """Fetch full messages (headers + body) for a list of IDs."""

    @abstractmethod
    def get_messages_metadata(self, ids: list[str]) -> list[Message]:
        """
        Fetch lightweight metadata only (headers + size, no body).

        Used by the stats pipeline where body content is never needed.
        Implementations should make this as cheap as possible — Gmail uses
        format=metadata, IMAP uses BODY.PEEK[HEADER.FIELDS ...] + RFC822.SIZE.
        """

    # ── Write ─────────────────────────────────────────────────────────────────

    @abstractmethod
    def batch_trash(self, ids: list[str]) -> int:
        """Move messages to Trash. Returns count of messages acted on."""

    @abstractmethod
    def batch_delete_permanent(self, ids: list[str]) -> int:
        """Permanently delete messages — no undo at the provider level."""

    @abstractmethod
    def batch_archive(self, ids: list[str]) -> int:
        """Archive messages (remove from inbox, keep accessible)."""

    @abstractmethod
    def batch_label(
        self,
        ids: list[str],
        add: list[str] = (),
        remove: list[str] = (),
    ) -> int:
        """Add/remove labels. Label semantics are provider-specific."""

    # ── Account ───────────────────────────────────────────────────────────────

    @abstractmethod
    def get_profile(self) -> dict:
        """
        Return account metadata as a dict with at minimum:
            emailAddress: str
            messagesTotal: int
            threadsTotal: int  (may be 0 if provider doesn't support threads)
        """

    def get_email_address(self) -> str:
        return self.get_profile()["emailAddress"]
