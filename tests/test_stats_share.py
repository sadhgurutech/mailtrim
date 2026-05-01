"""Tests for `mailtrim stats --share` and generate_stats_share_text."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

runner = CliRunner()

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_sender(
    email: str = "news@newsletter.co",
    name: str = "Newsletter",
    count: int = 80,
    size_bytes: int = 15 * 1024 * 1024,
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


def _invoke(*args: str, groups=None):
    from mailtrim.cli.main import app

    mock_client = MagicMock()
    mock_client.get_profile.return_value = {
        "emailAddress": "user@gmail.com",
        "messagesTotal": 5000,
        "threadsTotal": 3000,
    }
    mock_client.list_message_ids.return_value = []

    if groups is None:
        groups = [_make_sender()]

    with (
        patch("mailtrim.cli.main._get_provider", return_value=mock_client),
        patch("mailtrim.core.sender_stats.fetch_sender_groups", return_value=groups),
    ):
        return runner.invoke(app, ["stats", *args], catch_exceptions=False)


# ── generate_stats_share_text unit tests ─────────────────────────────────────


class TestGenerateStatsShareText:
    def test_twitter_under_280_chars(self):
        from mailtrim.core.sender_stats import generate_stats_share_text

        text = generate_stats_share_text(
            reclaimable_mb_val=87.5,
            sender_count=3,
            email_count=495,
            top_domains=["linkedin.com", "github.com", "newsletter.co"],
            scan_seconds=8,
            fmt="twitter",
        )
        assert len(text) <= 280

    def test_twitter_contains_emoji(self):
        from mailtrim.core.sender_stats import generate_stats_share_text

        text = generate_stats_share_text(
            reclaimable_mb_val=10.0,
            sender_count=2,
            email_count=100,
            top_domains=["example.com"],
            scan_seconds=3,
            fmt="twitter",
        )
        assert "🧹" in text

    def test_plain_no_emoji(self):
        from mailtrim.core.sender_stats import generate_stats_share_text

        text = generate_stats_share_text(
            reclaimable_mb_val=10.0,
            sender_count=2,
            email_count=100,
            top_domains=["example.com"],
            scan_seconds=3,
            fmt="plain",
        )
        assert "🧹" not in text

    def test_contains_email_count(self):
        from mailtrim.core.sender_stats import generate_stats_share_text

        text = generate_stats_share_text(
            reclaimable_mb_val=50.0,
            sender_count=4,
            email_count=1234,
            top_domains=[],
            scan_seconds=5,
        )
        assert "1,234" in text

    def test_contains_sender_count(self):
        from mailtrim.core.sender_stats import generate_stats_share_text

        text = generate_stats_share_text(
            reclaimable_mb_val=20.0,
            sender_count=5,
            email_count=300,
            top_domains=[],
            scan_seconds=4,
        )
        assert "5" in text
        assert "sender" in text

    def test_contains_mb_when_nonzero(self):
        from mailtrim.core.sender_stats import generate_stats_share_text

        text = generate_stats_share_text(
            reclaimable_mb_val=42.5,
            sender_count=2,
            email_count=200,
            top_domains=[],
            scan_seconds=3,
        )
        assert "42.5 MB" in text

    def test_no_mb_when_zero(self):
        from mailtrim.core.sender_stats import generate_stats_share_text

        text = generate_stats_share_text(
            reclaimable_mb_val=0,
            sender_count=2,
            email_count=50,
            top_domains=[],
            scan_seconds=2,
        )
        assert "MB" not in text

    def test_contains_top_domains(self):
        from mailtrim.core.sender_stats import generate_stats_share_text

        text = generate_stats_share_text(
            reclaimable_mb_val=5.0,
            sender_count=3,
            email_count=100,
            top_domains=["linkedin.com", "github.com"],
            scan_seconds=2,
        )
        assert "linkedin.com" in text
        assert "github.com" in text

    def test_no_personal_data(self):
        """Email addresses must never appear in share text."""
        from mailtrim.core.sender_stats import generate_stats_share_text

        text = generate_stats_share_text(
            reclaimable_mb_val=10.0,
            sender_count=2,
            email_count=100,
            top_domains=["example.com"],
            scan_seconds=3,
        )
        assert "@" not in text

    def test_contains_repo_url(self):
        from mailtrim.core.sender_stats import generate_stats_share_text

        text = generate_stats_share_text(
            reclaimable_mb_val=5.0,
            sender_count=1,
            email_count=50,
            top_domains=[],
            scan_seconds=1,
        )
        assert "github.com/sadhgurutech/mailtrim" in text

    def test_scan_speed_shown(self):
        from mailtrim.core.sender_stats import generate_stats_share_text

        text = generate_stats_share_text(
            reclaimable_mb_val=5.0,
            sender_count=1,
            email_count=50,
            top_domains=[],
            scan_seconds=7,
        )
        assert "7s" in text

    def test_twitter_stays_under_280_with_long_domains(self):
        """Even with many long domain names the output must not exceed 280."""
        from mailtrim.core.sender_stats import generate_stats_share_text

        long_domains = [
            "verylongnewsletterdomain.example.com",
            "another-ridiculously-long-domain-name.io",
            "thirdverylongemail.newsletter.co.uk",
        ]
        text = generate_stats_share_text(
            reclaimable_mb_val=100.0,
            sender_count=10,
            email_count=9999,
            top_domains=long_domains,
            scan_seconds=10,
            fmt="twitter",
        )
        assert len(text) <= 280

    def test_singular_sender_word(self):
        from mailtrim.core.sender_stats import generate_stats_share_text

        text = generate_stats_share_text(
            reclaimable_mb_val=5.0,
            sender_count=1,
            email_count=50,
            top_domains=[],
            scan_seconds=2,
        )
        assert "1 sender" in text
        assert "senders" not in text


# ── CLI integration tests ─────────────────────────────────────────────────────


class TestStatsCLIShare:
    def test_share_exits_without_full_output(self):
        result = _invoke("--share")
        assert result.exit_code == 0
        # Should not show the full stats table
        assert "Top Senders" not in result.output

    def test_share_shows_github_url(self):
        result = _invoke("--share")
        assert "github.com/sadhgurutech/mailtrim" in result.output

    def test_share_shows_copy_ready_section(self):
        result = _invoke("--share")
        assert "copy-ready" in result.output

    def test_share_shows_char_count(self):
        result = _invoke("--share")
        assert "chars" in result.output

    def test_share_twitter_fits_280(self):
        result = _invoke("--share")
        assert "fits Twitter" in result.output

    def test_share_format_plain(self):
        result = _invoke("--share", "--format", "plain")
        assert result.exit_code == 0
        assert "🧹" not in result.output

    def test_share_format_twitter_default(self):
        result = _invoke("--share")
        assert "🧹" in result.output

    def test_share_invalid_format(self):
        result = _invoke("--share", "--format", "markdown")
        assert result.exit_code == 1
        assert "Unknown --format" in result.output

    def test_share_no_email_address_in_output(self):
        """Account email must not leak into share output."""
        result = _invoke("--share")
        # "user@gmail.com" is the mocked account — must not appear
        assert "user@gmail.com" not in result.output

    def test_share_shows_sender_count(self):
        groups = [_make_sender(count=60), _make_sender(email="a@b.com", count=40)]
        result = _invoke("--share", groups=groups)
        assert result.exit_code == 0
        # At least 1 recommendation → shows count
        assert "sender" in result.output
