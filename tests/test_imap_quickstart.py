"""Tests for `mailtrim quickstart --provider imap`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture(autouse=True)
def _use_clean_db(clean_db):
    pass


# ── Helpers ─────────────────────���────────────────────────────────────���────────


def _make_sender(
    email: str = "news@example.com",
    name: str = "Example Newsletter",
    count: int = 60,
    size_bytes: int = 10 * 1024 * 1024,
    inbox_days: int = 200,
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


def _invoke_imap(auth_ok: bool = True, groups: list | None = None):
    from mailtrim.cli.main import app

    mock_provider = MagicMock()
    if auth_ok:
        mock_provider.get_email_address.return_value = "user@imap.example.com"
    else:
        mock_provider.get_email_address.side_effect = ConnectionError("login failed")

    if groups is None:
        groups = [_make_sender()]

    with (
        patch("mailtrim.cli.main._get_provider", return_value=mock_provider),
        patch(
            "mailtrim.cli.main._get_account_email",
            side_effect=ConnectionError("login failed")
            if not auth_ok
            else lambda _: "user@imap.example.com",
        ),
        patch("mailtrim.core.sender_stats.fetch_sender_groups", return_value=groups),
    ):
        return runner.invoke(
            app,
            [
                "quickstart",
                "--provider",
                "imap",
                "--imap-server",
                "imap.example.com",
                "--imap-user",
                "user@imap.example.com",
            ],
            catch_exceptions=False,
            env={"MAILTRIM_IMAP_PASSWORD": "testpass"},
        )


# ── Auth ──────────────────────────────────────────────────────────────────────


def test_imap_auth_failure_shows_setup_hint():
    result = _invoke_imap(auth_ok=False)
    assert result.exit_code == 1
    assert "mailtrim setup" in result.output


def test_imap_auth_failure_does_not_show_auth_hint():
    """IMAP users should not be told to run 'mailtrim auth' (that's Gmail-specific)."""
    result = _invoke_imap(auth_ok=False)
    assert "mailtrim auth" not in result.output


def test_imap_auth_success_shows_account():
    result = _invoke_imap()
    assert "user@imap.example.com" in result.output


# ── Scan results ───────────────────────────────────────────────────────────���──


def test_imap_shows_email_count():
    result = _invoke_imap(groups=[_make_sender(count=45)])
    assert "45" in result.output


def test_imap_shows_best_action():
    result = _invoke_imap(groups=[_make_sender()])
    assert "mailtrim purge" in result.output


def test_imap_shows_undo_hint():
    result = _invoke_imap()
    assert "mailtrim undo" in result.output


def test_imap_clean_inbox():
    result = _invoke_imap(groups=[])
    assert result.exit_code == 0
    assert "clean" in result.output.lower() or "nothing" in result.output.lower()


# ── Provider abstraction ────────────────────────────────���─────────────────────


def test_get_provider_called_not_get_client():
    """quickstart must use _get_provider(), never _get_client()."""
    from mailtrim.cli.main import app

    mock_provider = MagicMock()
    with (
        patch("mailtrim.cli.main._get_provider", return_value=mock_provider) as mock_gp,
        patch("mailtrim.cli.main._get_client") as mock_gc,
        patch("mailtrim.cli.main._get_account_email", return_value="u@example.com"),
        patch("mailtrim.core.sender_stats.fetch_sender_groups", return_value=[]),
    ):
        runner.invoke(app, ["quickstart"], catch_exceptions=False)
        assert mock_gp.called
        assert not mock_gc.called


# ── Capability: supports() ────────────────────────────────────────────────────


def test_imap_provider_supports_returns_false():
    from mailtrim.core.providers.imap import IMAPProvider

    # IMAPProvider.supports() is False for all capabilities
    provider = IMAPProvider.__new__(IMAPProvider)
    assert provider.supports("labels") is False
    assert provider.supports("threads") is False
    assert provider.supports("unsubscribe") is False
    assert provider.supports("rules") is False


def test_gmail_provider_supports_returns_true():
    from unittest.mock import MagicMock

    from mailtrim.core.providers.gmail import GmailProvider

    provider = GmailProvider(client=MagicMock())
    assert provider.supports("labels") is True
    assert provider.supports("threads") is True
    assert provider.supports("unsubscribe") is True
    assert provider.supports("rules") is True
