"""
Input validation helpers — centralise all user-supplied value checks.

Every value that flows from the CLI into a Gmail API query or file path goes
through one of these validators before use.  Validators raise ``typer.BadParameter``
so errors surface as friendly CLI messages rather than raw exceptions.
"""

from __future__ import annotations

import re

import typer

# ── Domain names ──────────────────────────────────────────────────────────────
# RFC 1123 label: starts/ends with alnum, hyphens allowed in the middle.
# We also allow leading wildcard (*.) for informational display, but strip it
# before embedding in queries.
_LABEL = r"[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?"
_DOMAIN_RE = re.compile(rf"^({_LABEL}\.)+{_LABEL}$")


def validate_domain(value: str) -> str:
    """
    Ensure *value* is a well-formed domain name before embedding it in a Gmail
    query (prevents query injection via crafted domain strings).

    Accepts: ``example.com``, ``mail.example.co.uk``
    Rejects: ``example.com OR from:other@bad.com``, ``-in:inbox``, ``../../..``

    Returns the lowercased domain on success.
    Raises typer.BadParameter on failure.
    """
    cleaned = value.strip().lower()
    if not _DOMAIN_RE.match(cleaned):
        raise typer.BadParameter(
            f"'{value}' is not a valid domain name. "
            "Expected format: example.com or mail.example.com"
        )
    return cleaned


# ── Email addresses ───────────────────────────────────────────────────────────
# Deliberately simple — the Gmail API validates the actual deliverability.
# We only need to block obvious injection strings (spaces, query operators).
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}$")


def validate_sender_email(value: str) -> str:
    """
    Ensure *value* looks like an email address before embedding it in a Gmail
    ``from:`` query.

    Accepts: ``user@example.com``
    Rejects: strings with spaces, multiple @, or Gmail query operators.

    Returns the lowercased address on success.
    Raises typer.BadParameter on failure.
    """
    cleaned = value.strip().lower()
    if not _EMAIL_RE.match(cleaned):
        raise typer.BadParameter(
            f"'{value}' is not a valid email address. Expected format: user@example.com"
        )
    return cleaned


# ── Numeric bounds ────────────────────────────────────────────────────────────

_MAX_OLDER_THAN_DAYS = 36_500  # 100 years — anything beyond this is meaningless


def validate_older_than(days: int) -> int:
    """
    Ensure the ``--older-than`` value is a sensible positive integer.

    Gmail silently ignores ``older_than:0d`` and ``older_than:-1d``, which
    produces confusing results.  An absurdly large value (> 100 years) almost
    certainly indicates a mistake.

    Returns the validated int on success.
    Raises typer.BadParameter on failure.
    """
    if days <= 0:
        raise typer.BadParameter(f"--older-than must be a positive number of days (got {days}).")
    if days > _MAX_OLDER_THAN_DAYS:
        raise typer.BadParameter(
            f"--older-than value {days} exceeds 100 years — did you mean something else?"
        )
    return days


# ── Since / date range ────────────────────────────────────────────────────────

_SINCE_RE = re.compile(r"^(\d+)d$")
_MAX_SINCE_DAYS = 36_500  # 100 years


def validate_since(value: str) -> int:
    """
    Parse and validate a ``--since <Nd>`` argument.

    Accepts: ``30d``, ``7d``, ``365d``
    Rejects: ``30``, ``d``, ``0d``, negative values, values > 100 years.

    Returns the number of days as an int on success.
    Raises typer.BadParameter on failure.
    """
    m = _SINCE_RE.match(value.strip())
    if not m:
        raise typer.BadParameter(
            f"'{value}' is not a valid --since value. "
            "Use the format <N>d, e.g. --since 30d or --since 7d."
        )
    days = int(m.group(1))
    if days <= 0:
        raise typer.BadParameter(f"--since must be at least 1 day (got {days}d).")
    if days > _MAX_SINCE_DAYS:
        raise typer.BadParameter(
            f"--since value {days}d exceeds 100 years — did you mean something else?"
        )
    return days
