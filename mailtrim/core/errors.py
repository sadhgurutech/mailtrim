"""
Human-readable error translation for common failure modes.

Usage:
    from mailtrim.core.errors import friendly_error
    try:
        ...
    except Exception as exc:
        msg, fix = friendly_error(exc)
        console.print(f"[red]{msg}[/red]")
        if fix:
            console.print(f"  [dim]Fix: {fix}[/dim]")
        raise typer.Exit(1)
"""

from __future__ import annotations

import re


def friendly_error(exc: BaseException) -> tuple[str, str]:
    """
    Map an exception to a (human message, fix hint) tuple.

    Returns plain strings — caller decides how to display them.
    The message is always set; fix may be empty.
    """
    msg = str(exc)
    msg_lower = msg.lower()

    # ── Auth / OAuth ──────────────────────────────────────────────────────────
    if "invalid_grant" in msg_lower or (
        "401" in msg and ("gmail" in msg_lower or "google" in msg_lower)
    ):
        return (
            "Your Gmail connection expired.",
            "Run: mailtrim auth",
        )

    if "credentials" in msg_lower and "not found" in msg_lower:
        return (
            "OAuth credentials file not found.",
            "Download credentials.json from Google Cloud Console — see README for setup steps.",
        )

    if "token" in msg_lower and ("expired" in msg_lower or "invalid" in msg_lower):
        return (
            "Gmail token is no longer valid.",
            "Run: mailtrim auth",
        )

    if "access_denied" in msg_lower or "403" in msg:
        return (
            "Gmail access denied — insufficient permissions.",
            "Re-run: mailtrim auth  (grant all requested scopes)",
        )

    # ── Network ───────────────────────────────────────────────────────────────
    if any(
        kw in msg_lower for kw in ("timed out", "timeout", "connection refused", "connection reset")
    ):
        return (
            "Could not reach Gmail — check your internet connection and try again.",
            "",
        )

    if "name or service not known" in msg_lower or "nodename nor servname" in msg_lower:
        return (
            "DNS lookup failed — no internet connection or Gmail is unreachable.",
            "Check your network, then retry.",
        )

    if "ssl" in msg_lower and ("cert" in msg_lower or "handshake" in msg_lower):
        return (
            "SSL/TLS error connecting to Gmail.",
            "Check that your system certificates are up to date.",
        )

    # ── File system ───────────────────────────────────────────────────────────
    if isinstance(exc, PermissionError) or "permission denied" in msg_lower:
        path_match = re.search(r"'([^']+)'", msg)
        path_hint = f" ({path_match.group(1)})" if path_match else ""
        return (
            f"mailtrim cannot write to your local config files{path_hint}.",
            "Check folder permissions on ~/.mailtrim/",
        )

    if isinstance(exc, FileNotFoundError):
        return (
            f"Required file not found: {msg[:100]}",
            "Check the path or run: mailtrim auth",
        )

    if isinstance(exc, OSError) and "no space left" in msg_lower:
        return (
            "No disk space left — mailtrim cannot save state.",
            "Free up disk space, then retry.",
        )

    # ── Gmail API ────────────────────────────────────────────────────────────
    if "quotaExceeded" in msg or "rateLimitExceeded" in msg or "429" in msg:
        return (
            "Gmail API rate limit hit — you've sent too many requests.",
            "Wait 60 seconds and try again.",
        )

    if "HttpError 500" in msg or "HttpError 502" in msg or "HttpError 503" in msg:
        return (
            "Gmail API is temporarily unavailable (server error).",
            "Wait a few minutes and retry.",
        )

    if "userRateLimitExceeded" in msg:
        return (
            "You've hit Gmail's per-user rate limit.",
            "Wait a minute, then retry with a smaller --max-scan value.",
        )

    # ── Database ─────────────────────────────────────────────────────────────
    if "database" in msg_lower or "sqlite" in msg_lower or "no such table" in msg_lower:
        return (
            "Local database error — undo history may be unavailable.",
            "If this persists, delete ~/.mailtrim/mailtrim.db and retry.",
        )

    if "disk image is malformed" in msg_lower or "database disk image" in msg_lower:
        return (
            "Local database is corrupted.",
            "Delete ~/.mailtrim/mailtrim.db — your Gmail is unaffected, but undo history will be lost.",
        )

    # ── Fallback ─────────────────────────────────────────────────────────────
    return (
        f"Unexpected error: {msg[:120]}",
        "If this persists, report it at github.com/sadhgurutech/mailtrim/issues",
    )
