"""Provider factory — select and construct the right EmailProvider from CLI config.

This is the only place that imports both GmailProvider and IMAPProvider.
All other code depends only on the EmailProvider interface.
"""

from __future__ import annotations

from mailtrim.core.providers.base import EmailProvider


def get_provider(
    provider: str = "gmail",
    *,
    # IMAP-specific
    imap_server: str = "",
    imap_user: str = "",
    imap_password: str = "",
    imap_port: int = 993,
    imap_folder: str = "INBOX",
) -> EmailProvider:
    """
    Construct and return the appropriate EmailProvider.

    Args:
        provider:      "gmail" or "imap"
        imap_server:   Required when provider="imap"
        imap_user:     Required when provider="imap"
        imap_password: Required when provider="imap"
        imap_port:     IMAP SSL port (default 993)
        imap_folder:   Default folder to operate on (default INBOX)

    Raises:
        ValueError: if provider is unknown or IMAP credentials are missing
    """
    if provider == "gmail":
        from mailtrim.core.providers.gmail import GmailProvider

        return GmailProvider()

    if provider == "imap":
        if not imap_server or not imap_user or not imap_password:
            raise ValueError(
                "IMAP provider requires --imap-server, --imap-user, and --imap-password. "
                "Use an app password, not your account password."
            )
        from mailtrim.core.providers.imap import IMAPProvider

        return IMAPProvider(
            server=imap_server,
            user=imap_user,
            password=imap_password,
            port=imap_port,
            folder=imap_folder,
        )

    raise ValueError(f"Unknown provider '{provider}'. Valid options: gmail, imap")
