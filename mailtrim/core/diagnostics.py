"""
Diagnostic checks for mailtrim doctor command.

Each check returns a CheckResult.  All checks are independent — a failure in
one does not prevent the others from running.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class CheckResult:
    name: str
    ok: bool
    message: str
    fix: str = ""  # one-line fix hint shown on failure
    optional: bool = False  # optional checks show ⚠ instead of ✗


# ── Individual checks ─────────────────────────────────────────────────────────


def check_token_exists() -> CheckResult:
    from mailtrim.config import TOKEN_PATH

    if TOKEN_PATH.exists():
        return CheckResult("Auth token file", ok=True, message="Token file present")
    return CheckResult(
        "Auth token file",
        ok=False,
        message="Token file not found",
        fix="mailtrim auth",
    )


def check_token_valid() -> CheckResult:
    from mailtrim.config import TOKEN_PATH

    if not TOKEN_PATH.exists():
        return CheckResult(
            "Auth token valid",
            ok=False,
            message="No token to validate",
            fix="mailtrim auth",
        )
    try:
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))
        if creds.expired and not creds.refresh_token:
            return CheckResult(
                "Auth token valid",
                ok=False,
                message="Token expired, no refresh token",
                fix="mailtrim auth",
            )
        return CheckResult("Auth token valid", ok=True, message="Token looks valid")
    except Exception as exc:
        return CheckResult(
            "Auth token valid",
            ok=False,
            message=f"Token unreadable: {exc}",
            fix="mailtrim auth",
        )


def check_gmail_connection() -> CheckResult:
    try:
        from mailtrim.core.gmail_client import GmailClient, authenticate

        creds = authenticate()
        client = GmailClient(creds)
        profile = client.get_profile()
        email = profile.get("emailAddress", "unknown")
        return CheckResult("Gmail connection", ok=True, message=f"Connected as {email}")
    except FileNotFoundError:
        return CheckResult(
            "Gmail connection",
            ok=False,
            message="OAuth credentials file missing",
            fix="Download credentials.json from Google Cloud Console — see README",
        )
    except Exception as exc:
        _msg = str(exc)
        if "invalid_grant" in _msg or "401" in _msg:
            return CheckResult(
                "Gmail connection",
                ok=False,
                message="Gmail session expired",
                fix="mailtrim auth",
            )
        if "timed out" in _msg.lower() or "connection" in _msg.lower():
            return CheckResult(
                "Gmail connection",
                ok=False,
                message="Could not reach Gmail — check internet connection",
                fix="Check network, then retry",
            )
        return CheckResult(
            "Gmail connection",
            ok=False,
            message=f"Connection failed: {_msg[:80]}",
            fix="mailtrim auth",
        )


def check_trash_access() -> CheckResult:
    try:
        from mailtrim.core.gmail_client import GmailClient, authenticate

        creds = authenticate()
        client = GmailClient(creds)
        # List 1 trashed message — proves we can query Trash without side effects
        client.list_message_ids(query="in:trash", max_results=1)
        return CheckResult("Trash access", ok=True, message="Trash label readable")
    except Exception as exc:
        return CheckResult(
            "Trash access",
            ok=False,
            message=f"Cannot read Trash: {str(exc)[:80]}",
            fix="Check Gmail API scopes — may need to re-run mailtrim auth",
        )


def check_data_dir() -> CheckResult:
    from mailtrim.config import DATA_DIR

    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        test_file = DATA_DIR / ".doctor_check"
        test_file.write_text("ok")
        test_file.unlink()
        return CheckResult("Data directory", ok=True, message=f"Writable at {DATA_DIR}")
    except OSError as exc:
        return CheckResult(
            "Data directory",
            ok=False,
            message=f"Cannot write to {DATA_DIR}: {exc}",
            fix=f"Check permissions on {DATA_DIR.parent}",
        )


def check_undo_storage() -> CheckResult:
    try:
        from mailtrim.core.storage import UndoLogRepo, get_session

        session = get_session()
        repo = UndoLogRepo(session)
        repo.list_recent(limit=1)
        return CheckResult("Undo storage", ok=True, message="Database readable and writable")
    except Exception as exc:
        return CheckResult(
            "Undo storage",
            ok=False,
            message=f"Database error: {str(exc)[:80]}",
            fix="Delete ~/.mailtrim/mailtrim.db and retry (undo history will be lost)",
        )


def check_config() -> CheckResult:
    try:
        from mailtrim.config import get_settings

        settings = get_settings()
        _ = settings.undo_window_days  # access any field
        return CheckResult("Config", ok=True, message="Configuration loaded successfully")
    except Exception as exc:
        return CheckResult(
            "Config",
            ok=False,
            message=f"Config error: {str(exc)[:80]}",
            fix="Check ~/.mailtrim/.env for syntax errors",
        )


def check_ai_endpoint(url: str = "http://localhost:8080") -> CheckResult:
    import urllib.request

    try:
        req = urllib.request.Request(f"{url}/health", method="GET")
        with urllib.request.urlopen(req, timeout=2):  # nosec B310 — localhost health check only
            pass
        return CheckResult(
            "Local AI endpoint",
            ok=True,
            message=f"llama.cpp reachable at {url}",
            optional=True,
        )
    except Exception:
        return CheckResult(
            "Local AI endpoint",
            ok=False,
            message=f"Not reachable at {url} (optional — only needed for --ai flag)",
            optional=True,
        )


def check_dependencies() -> CheckResult:
    missing = []
    packages = [
        ("google.auth", "google-auth"),
        ("googleapiclient", "google-api-python-client"),
        ("rich", "rich"),
        ("typer", "typer"),
        ("sqlalchemy", "sqlalchemy"),
        ("pydantic_settings", "pydantic-settings"),
    ]
    for module, pkg in packages:
        try:
            __import__(module)
        except ImportError:
            missing.append(pkg)

    if missing:
        return CheckResult(
            "Required packages",
            ok=False,
            message=f"Missing: {', '.join(missing)}",
            fix="pip install mailtrim",
        )
    return CheckResult("Required packages", ok=True, message="All required packages present")


# ── Run all checks ────────────────────────────────────────────────────────────

# Ordered list of checks. Gmail-dependent ones come after token/dep checks.
_CHECKS: list[Callable[[], CheckResult]] = [
    check_dependencies,
    check_config,
    check_data_dir,
    check_undo_storage,
    check_token_exists,
    check_token_valid,
    check_gmail_connection,
    check_trash_access,
]

_OPTIONAL_CHECKS: list[Callable[[], CheckResult]] = [
    check_ai_endpoint,
]


def run_all(include_optional: bool = True) -> list[CheckResult]:
    """Run all checks and return results in order."""
    results = []
    for fn in _CHECKS:
        try:
            results.append(fn())
        except Exception as exc:
            results.append(
                CheckResult(
                    fn.__name__,
                    ok=False,
                    message=f"Check crashed: {exc}",
                    fix="Please report this at github.com/sadhgurutech/mailtrim/issues",
                )
            )
    if include_optional:
        for fn in _OPTIONAL_CHECKS:
            try:
                results.append(fn())
            except Exception as exc:
                results.append(
                    CheckResult(
                        fn.__name__,
                        ok=False,
                        message=f"Check crashed: {exc}",
                        optional=True,
                    )
                )
    return results
