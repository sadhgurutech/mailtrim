"""Gmail provider — wraps GmailClient to implement EmailProvider.

All existing GmailClient behaviour is preserved exactly.
This file is the only place that knows about Gmail internals;
the rest of the pipeline sees only EmailProvider.
"""

from __future__ import annotations

from mailtrim.config import get_settings
from mailtrim.core.gmail_client import GmailClient, Message, _chunks
from mailtrim.core.providers.base import EmailProvider


class GmailProvider(EmailProvider):
    """
    Thin adapter: EmailProvider interface → GmailClient implementation.

    Constructed by the CLI and injected into the pipeline.  Nothing in
    stats / bulk / scoring imports this class directly.
    """

    def __init__(self, client: GmailClient | None = None) -> None:
        self._client = client or GmailClient()

    # ── Read ──────────────────────────────────────────────────────────────────

    def list_message_ids(
        self,
        query: str = "",
        max_results: int | None = None,
    ) -> list[str]:
        return self._client.list_message_ids(query=query, max_results=max_results)

    def get_messages_batch(self, ids: list[str]) -> list[Message]:
        return self._client.get_messages_batch(ids)

    def get_messages_metadata(self, ids: list[str]) -> list[Message]:
        """Metadata-only fetch using Gmail's format=metadata (no body download)."""
        settings = get_settings()
        results: list[Message] = []
        for chunk in _chunks(ids, settings.gmail_batch_size):
            results.extend(self._client._fetch_batch(chunk, format="metadata"))
        return results

    # ── Write ─────────────────────────────────────────────────────────────────

    def batch_trash(self, ids: list[str]) -> int:
        return self._client.batch_trash(ids)

    def batch_delete_permanent(self, ids: list[str]) -> int:
        return self._client.batch_delete_permanent(ids)

    def batch_archive(self, ids: list[str]) -> int:
        return self._client.batch_archive(ids)

    def batch_label(
        self,
        ids: list[str],
        add: list[str] = (),
        remove: list[str] = (),
    ) -> int:
        return self._client.batch_label(ids, add=list(add), remove=list(remove))

    # ── Account ───────────────────────────────────────────────────────────────

    def get_profile(self) -> dict:
        return self._client.get_profile()

    def get_email_address(self) -> str:
        return self._client.get_email_address()

    # ── Pass-through for Gmail-only features ──────────────────────────────────
    # Code that requires Gmail-specific capabilities (labels, OAuth, unsubscribe)
    # accesses the underlying client directly. The provider abstraction is only
    # for the stats/purge pipeline.

    @property
    def gmail_client(self) -> GmailClient:
        """Direct access for Gmail-specific operations (labels, drafts, etc.)."""
        return self._client
