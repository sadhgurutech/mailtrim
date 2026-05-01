"""Tests for `mailtrim setup` command."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

runner = CliRunner()

# ── Helpers ───────────────────────────────────────────────────────────────────

_GOOD_PROFILE = {"emailAddress": "user@gmail.com", "messagesTotal": 3000, "threadsTotal": 2000}


def _make_sender(
    email: str = "news@newsletter.co",
    name: str = "Newsletter",
    count: int = 70,
    size_bytes: int = 12 * 1024 * 1024,
    inbox_days: int = 180,
    has_unsubscribe: bool = True,
):
    from mailtrim.core.sender_stats import SenderGroup

    now = datetime.now(timezone.utc)
    return SenderGroup(
        sender_email=email,
        sender_name=name,
        count=count,
        total_size_bytes=size_bytes,
        earliest_date=now - timedelta(days=inbox_days),
        latest_date=now,
        sample_subjects=["Weekly digest"],
        message_ids=[f"id{i}" for i in range(count)],
        has_unsubscribe=has_unsubscribe,
        impact_score=80,
    )


def _mock_checks_ok():
    """Return a patch context that makes all doctor checks pass."""
    from mailtrim.core.diagnostics import CheckResult

    ok_results = [
        CheckResult("Required packages", ok=True, message="ok"),
        CheckResult("Config", ok=True, message="ok"),
        CheckResult("Data directory", ok=True, message="ok"),
        CheckResult("Undo storage", ok=True, message="ok"),
        CheckResult("Auth token file", ok=True, message="ok"),
        CheckResult("Auth token valid", ok=True, message="ok"),
        CheckResult("Gmail connection", ok=True, message="ok"),
        CheckResult("Trash access", ok=True, message="ok"),
    ]
    return patch("mailtrim.core.diagnostics.run_all", return_value=ok_results)


def _invoke_gmail(groups=None, auth_ok=True, checks_ok=True, credentials_exist=True):
    """Simulate user typing 'G' for Gmail, happy path by default."""
    from mailtrim.cli.main import app
    from mailtrim.config import CREDENTIALS_PATH

    if groups is None:
        groups = [_make_sender()]

    mock_creds = MagicMock()
    mock_client = MagicMock()
    mock_client.get_email_address.return_value = "user@gmail.com"
    mock_client.get_profile.return_value = _GOOD_PROFILE

    from mailtrim.core.diagnostics import CheckResult

    if checks_ok:
        check_results = [
            CheckResult("Required packages", ok=True, message="ok"),
            CheckResult("Config", ok=True, message="ok"),
            CheckResult("Data directory", ok=True, message="ok"),
            CheckResult("Undo storage", ok=True, message="ok"),
            CheckResult("Auth token file", ok=True, message="ok"),
            CheckResult("Auth token valid", ok=True, message="ok"),
            CheckResult("Gmail connection", ok=True, message="ok"),
            CheckResult("Trash access", ok=True, message="ok"),
        ]
    else:
        check_results = [
            CheckResult("Required packages", ok=True, message="ok"),
            CheckResult(
                "Gmail connection",
                ok=False,
                message="Session expired",
                fix="mailtrim auth",
            ),
        ]

    with (
        patch(
            "mailtrim.cli.main.CREDENTIALS_PATH",
            CREDENTIALS_PATH if not credentials_exist else CREDENTIALS_PATH,
        ),
        patch("mailtrim.config.CREDENTIALS_PATH", CREDENTIALS_PATH),
        patch("pathlib.Path.exists", return_value=credentials_exist),
        patch("mailtrim.core.gmail_client.authenticate", return_value=mock_creds),
        patch(
            "mailtrim.cli.main._get_client",
            side_effect=Exception("bad auth") if not auth_ok else lambda: mock_client,
        ),
        patch(
            "mailtrim.core.gmail_client.GmailClient",
            return_value=mock_client if auth_ok else (_ for _ in ()).throw(Exception("bad auth")),
        ),
        patch("mailtrim.core.diagnostics.run_all", return_value=check_results),
        patch("mailtrim.core.sender_stats.fetch_sender_groups", return_value=groups),
    ):
        return runner.invoke(app, ["setup"], input="G\n", catch_exceptions=False)


def _invoke_imap(groups=None, imap_ok=True):
    """Simulate user typing 'I' for IMAP."""
    from mailtrim.cli.main import app

    if groups is None:
        groups = [_make_sender()]

    mock_provider = MagicMock()
    mock_provider.get_profile.return_value = _GOOD_PROFILE
    if not imap_ok:
        mock_provider.get_profile.side_effect = Exception("auth failed")

    from mailtrim.core.diagnostics import CheckResult

    ok = CheckResult("ok", ok=True, message="ok")
    check_results = [
        CheckResult("Required packages", ok=True, message="ok"),
        CheckResult("Config", ok=True, message="ok"),
        CheckResult("Data directory", ok=True, message="ok"),
        CheckResult("Undo storage", ok=True, message="ok"),
    ]
    imap_input = "I\nimap.example.com\nuser@example.com\nsecret\n993\n"
    with (
        patch("pathlib.Path.exists", return_value=True),
        patch("mailtrim.core.providers.factory.get_provider", return_value=mock_provider),
        patch("mailtrim.core.diagnostics.run_all", return_value=check_results),
        patch("mailtrim.core.diagnostics.check_dependencies", return_value=ok),
        patch("mailtrim.core.diagnostics.check_config", return_value=ok),
        patch("mailtrim.core.diagnostics.check_data_dir", return_value=ok),
        patch("mailtrim.core.diagnostics.check_undo_storage", return_value=ok),
        patch("mailtrim.core.sender_stats.fetch_sender_groups", return_value=groups),
    ):
        return runner.invoke(app, ["setup"], input=imap_input, catch_exceptions=False)


# ── Welcome message ───────────────────────────────────────────────────────────


def test_welcome_shows_time_estimate():
    result = _invoke_gmail()
    assert "1–2 minutes" in result.output


def test_welcome_shows_safety_note():
    result = _invoke_gmail()
    assert "deleted without your explicit command" in result.output


# ── Provider selection ────────────────────────────────────────────────────────


def test_shows_gmail_option():
    result = _invoke_gmail()
    assert "Gmail" in result.output


def test_shows_imap_option():
    result = _invoke_gmail()
    assert "IMAP" in result.output


# ── Gmail auth ────────────────────────────────────────────────────────────────


def test_gmail_happy_path_exits_0():
    result = _invoke_gmail()
    assert result.exit_code == 0


def test_gmail_shows_authenticated_account():
    result = _invoke_gmail()
    assert "user@gmail.com" in result.output


def test_missing_credentials_exits_1():
    result = _invoke_gmail(credentials_exist=False)
    assert result.exit_code == 1


def test_missing_credentials_shows_steps():
    result = _invoke_gmail(credentials_exist=False)
    assert "console.cloud.google.com" in result.output or "cloud.google.com" in result.output


def test_missing_credentials_shows_retry_hint():
    result = _invoke_gmail(credentials_exist=False)
    assert "mailtrim setup" in result.output


# ── Doctor checks ─────────────────────────────────────────────────────────────


def test_shows_step_3_health_check():
    result = _invoke_gmail()
    assert "Step 3" in result.output


def test_shows_check_icons():
    result = _invoke_gmail()
    assert "✓" in result.output


def test_doctor_failure_exits_1():
    result = _invoke_gmail(checks_ok=False)
    assert result.exit_code == 1


def test_doctor_failure_shows_fix():
    result = _invoke_gmail(checks_ok=False)
    assert "mailtrim auth" in result.output or "Fix" in result.output


def test_doctor_failure_shows_retry_hint():
    result = _invoke_gmail(checks_ok=False)
    assert "mailtrim setup" in result.output


# ── Quickstart scan ───────────────────────────────────────────────────────────


def test_shows_emails_scanned():
    result = _invoke_gmail()
    assert "emails" in result.output


def test_shows_safe_sender_count():
    result = _invoke_gmail(groups=[_make_sender()])
    assert "safe senders to clean" in result.output


def test_shows_best_action_command():
    result = _invoke_gmail(groups=[_make_sender()])
    assert "mailtrim purge" in result.output


def test_shows_undo_hint():
    result = _invoke_gmail()
    assert "mailtrim undo" in result.output


def test_clean_inbox_message():
    result = _invoke_gmail(groups=[])
    assert result.exit_code == 0
    assert "clean" in result.output.lower() or "nothing" in result.output.lower()


# ── Done / setup complete ─────────────────────────────────────────────────────


def test_shows_setup_complete():
    result = _invoke_gmail()
    assert "Setup complete" in result.output or "all set" in result.output.lower()


def test_shows_next_commands():
    result = _invoke_gmail()
    assert "mailtrim quickstart" in result.output
    assert "mailtrim stats" in result.output


# ── IMAP path ─────────────────────────────────────────────────────────────────


def test_imap_happy_path_exits_0():
    result = _invoke_imap()
    assert result.exit_code == 0


def test_imap_failure_exits_1():
    result = _invoke_imap(imap_ok=False)
    assert result.exit_code == 1


def test_imap_failure_shows_retry_hint():
    result = _invoke_imap(imap_ok=False)
    assert "mailtrim setup" in result.output
