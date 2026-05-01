"""Tests for `mailtrim quickstart` command."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

runner = CliRunner()


# ── Fixtures ──────────────────────────────────────────────────────────────────


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
    from datetime import timedelta

    earliest = now - timedelta(days=inbox_days)
    return SenderGroup(
        sender_email=email,
        sender_name=name,
        count=count,
        total_size_bytes=size_bytes,
        earliest_date=earliest,
        latest_date=now,
        sample_subjects=["Weekly digest", "Top stories this week"],
        message_ids=[f"id{i}" for i in range(count)],
        has_unsubscribe=has_unsubscribe,
        impact_score=80,
    )


def _invoke(auth_ok: bool = True, groups: list | None = None):
    from mailtrim.cli.main import app

    mock_client = MagicMock()
    mock_client.list_message_ids.return_value = []

    if groups is None:
        groups = [_make_sender()]

    with (
        patch("mailtrim.cli.main._get_client", return_value=mock_client),
        patch(
            "mailtrim.cli.main._get_account_email",
            side_effect=Exception("no auth") if not auth_ok else lambda _: "user@gmail.com",
        ),
        patch("mailtrim.core.sender_stats.fetch_sender_groups", return_value=groups),
    ):
        return runner.invoke(app, ["quickstart"], catch_exceptions=False)


# ── Auth ──────────────────────────────────────────────────────────────────────


def test_auth_failure_shows_hint():
    result = _invoke(auth_ok=False)
    assert result.exit_code == 1
    assert "mailtrim auth" in result.output


def test_auth_success_shows_account():
    result = _invoke()
    assert "user@gmail.com" in result.output


# ── Scan summary ──────────────────────────────────────────────────────────────


def test_shows_email_count():
    sender = _make_sender(count=60)
    result = _invoke(groups=[sender])
    assert "60" in result.output


def test_shows_safe_candidate_count():
    result = _invoke(groups=[_make_sender(has_unsubscribe=True, inbox_days=200, count=60)])
    # Fixture sender is safe (newsletter keyword) + high confidence → ≥1 candidate
    assert "safe senders to clean" in result.output


def test_shows_reclaimable_mb_when_nonzero():
    result = _invoke(groups=[_make_sender(size_bytes=15 * 1024 * 1024)])
    assert "MB" in result.output


# ── Best action ───────────────────────────────────────────────────────────────


def test_shows_best_action_command():
    result = _invoke(groups=[_make_sender(email="news@example.com")])
    assert "mailtrim purge" in result.output
    assert "example.com" in result.output


def test_shows_sender_name_in_best_action():
    result = _invoke(groups=[_make_sender(name="Example Newsletter")])
    assert "Example Newsletter" in result.output


def test_shows_email_count_in_best_action():
    result = _invoke(groups=[_make_sender(count=60)])
    assert "60" in result.output


# ── Undo hint ─────────────────────────────────────────────────────────────────


def test_shows_undo_hint():
    result = _invoke()
    assert "mailtrim undo" in result.output


# ── Output length ─────────────────────────────────────────────────────────────


def test_output_under_12_lines():
    result = _invoke()
    # Strip blank lines and leading/trailing whitespace for a fair count
    non_blank = [ln for ln in result.output.splitlines() if ln.strip()]
    assert len(non_blank) <= 12, f"Got {len(non_blank)} lines:\n{result.output}"


# ── Clean inbox ───────────────────────────────────────────────────────────────


def test_clean_inbox_message():
    result = _invoke(groups=[])
    assert result.exit_code == 0
    assert "clean" in result.output.lower() or "nothing" in result.output.lower()


# ── Sensitive sender filtered ─────────────────────────────────────────────────


def test_sensitive_sender_not_suggested():
    """Banks should not appear as the best first action."""
    bank = _make_sender(email="alerts@bankofamerica.com", name="Bank of America")
    result = _invoke(groups=[bank])
    # Should not show purge command for a sensitive sender
    assert "mailtrim purge --domain bankofamerica.com --yes" not in result.output
