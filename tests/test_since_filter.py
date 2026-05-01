"""Tests for --since <Nd> time-based filtering."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

runner = CliRunner()


# ── validate_since unit tests ─────────────────────────────────────────────────


class TestValidateSince:
    def _v(self, value: str) -> int:
        from mailtrim.core.validation import validate_since

        return validate_since(value)

    def test_valid_30d(self):
        assert self._v("30d") == 30

    def test_valid_7d(self):
        assert self._v("7d") == 7

    def test_valid_1d(self):
        assert self._v("1d") == 1

    def test_valid_365d(self):
        assert self._v("365d") == 365

    def test_valid_with_whitespace(self):
        assert self._v("  30d  ") == 30

    def test_missing_d_suffix(self):
        with pytest.raises(typer.BadParameter, match="format"):
            self._v("30")

    def test_zero_days(self):
        with pytest.raises(typer.BadParameter, match="at least 1"):
            self._v("0d")

    def test_non_numeric(self):
        with pytest.raises(typer.BadParameter, match="format"):
            self._v("abcd")

    def test_empty_string(self):
        with pytest.raises(typer.BadParameter):
            self._v("")

    def test_negative_not_accepted(self):
        # "-30d" doesn't match the regex (no leading minus allowed)
        with pytest.raises(typer.BadParameter):
            self._v("-30d")

    def test_over_100_years_rejected(self):
        with pytest.raises(typer.BadParameter, match="100 years"):
            self._v("36501d")

    def test_exactly_100_years_ok(self):
        assert self._v("36500d") == 36500


# ── IMAP query translation ────────────────────────────────────────────────────


class TestImapQueryTranslation:
    def _translate(self, query: str):
        from mailtrim.core.providers.imap import _gmail_query_to_imap

        return _gmail_query_to_imap(query)

    def test_newer_than_produces_since(self):
        _, criteria = self._translate("in:inbox newer_than:30d")
        assert "SINCE" in criteria

    def test_newer_than_date_format(self):
        """IMAP SINCE uses DD-Mon-YYYY format."""
        _, criteria = self._translate("in:inbox newer_than:7d")
        import re

        assert re.search(r"SINCE \"\d{2}-[A-Z][a-z]{2}-\d{4}\"", criteria)

    def test_newer_than_date_is_recent(self):
        """The SINCE date should be within the last N+1 days."""
        _, criteria = self._translate("in:inbox newer_than:30d")
        import re
        from datetime import datetime

        m = re.search(r'SINCE "(\d{2}-[A-Z][a-z]{2}-\d{4})"', criteria)
        assert m, f"No SINCE date found in: {criteria}"
        dt = datetime.strptime(m.group(1), "%d-%b-%Y")
        days_ago = (datetime.now() - dt).days
        assert 29 <= days_ago <= 31  # ±1 day tolerance for day boundaries

    def test_older_than_and_newer_than_coexist(self):
        """Both BEFORE and SINCE can appear in one query (date range)."""
        _, criteria = self._translate("in:inbox older_than:90d newer_than:30d")
        assert "BEFORE" in criteria
        assert "SINCE" in criteria

    def test_no_newer_than_produces_no_since(self):
        _, criteria = self._translate("in:inbox")
        assert "SINCE" not in criteria

    def test_inbox_scope_preserved(self):
        folder, _ = self._translate("in:inbox newer_than:7d")
        assert folder == "INBOX"


# ── Gmail query construction ──────────────────────────────────────────────────


class TestGmailQueryConstruction:
    """Verify that --since injects newer_than into the query string."""

    def _make_client(self, groups=None):
        from mailtrim.core.sender_stats import SenderGroup

        if groups is None:
            now = datetime.now(timezone.utc)
            groups = [
                SenderGroup(
                    sender_email="news@example.com",
                    sender_name="Example",
                    count=30,
                    total_size_bytes=5 * 1024 * 1024,
                    earliest_date=now - timedelta(days=20),
                    latest_date=now,
                    sample_subjects=["Newsletter"],
                    message_ids=[f"id{i}" for i in range(30)],
                    has_unsubscribe=True,
                    impact_score=70,
                )
            ]

        mock = MagicMock()
        mock.get_profile.return_value = {
            "emailAddress": "user@gmail.com",
            "messagesTotal": 3000,
            "threadsTotal": 2000,
        }
        mock.list_message_ids.return_value = [g.message_ids[0] for g in groups]
        return mock, groups

    def test_stats_since_appends_newer_than_to_query(self):
        from mailtrim.cli.main import app

        mock_client, groups = self._make_client()
        captured_queries: list[str] = []

        def fake_fetch(client, query, **kwargs):
            captured_queries.append(query)
            return groups

        with (
            patch("mailtrim.cli.main._get_provider", return_value=mock_client),
            patch("mailtrim.core.sender_stats.fetch_sender_groups", side_effect=fake_fetch),
        ):
            result = runner.invoke(app, ["stats", "--since", "30d"], catch_exceptions=False)

        assert result.exit_code == 0
        assert any("newer_than:30d" in q for q in captured_queries)

    def test_stats_no_since_no_newer_than(self):
        from mailtrim.cli.main import app

        mock_client, groups = self._make_client()
        captured_queries: list[str] = []

        def fake_fetch(client, query, **kwargs):
            captured_queries.append(query)
            return groups

        with (
            patch("mailtrim.cli.main._get_provider", return_value=mock_client),
            patch("mailtrim.core.sender_stats.fetch_sender_groups", side_effect=fake_fetch),
        ):
            result = runner.invoke(app, ["stats"], catch_exceptions=False)

        assert result.exit_code == 0
        assert all("newer_than" not in q for q in captured_queries)

    def test_stats_since_with_scope_anywhere(self):
        from mailtrim.cli.main import app

        mock_client, groups = self._make_client()
        captured_queries: list[str] = []

        def fake_fetch(client, query, **kwargs):
            captured_queries.append(query)
            return groups

        with (
            patch("mailtrim.cli.main._get_provider", return_value=mock_client),
            patch("mailtrim.core.sender_stats.fetch_sender_groups", side_effect=fake_fetch),
        ):
            result = runner.invoke(
                app, ["stats", "--since", "7d", "--scope", "anywhere"], catch_exceptions=False
            )

        assert result.exit_code == 0
        assert any("newer_than:7d" in q and "in:anywhere" in q for q in captured_queries)

    def test_purge_since_appends_newer_than_to_query(self):
        from mailtrim.cli.main import app

        mock_client, groups = self._make_client()
        captured_queries: list[str] = []

        def fake_fetch(client, query, **kwargs):
            captured_queries.append(query)
            return groups

        with (
            patch("mailtrim.cli.main._get_provider", return_value=mock_client),
            patch("mailtrim.cli.main._get_account_email", return_value="user@gmail.com"),
            patch("mailtrim.core.sender_stats.fetch_sender_groups", side_effect=fake_fetch),
            patch("mailtrim.core.storage.BlocklistRepo") as mock_bl,
        ):
            mock_bl.return_value.blocked_emails.return_value = set()
            result = runner.invoke(
                app, ["purge", "--since", "30d", "--json"], catch_exceptions=False
            )

        assert result.exit_code == 0
        assert any("newer_than:30d" in q for q in captured_queries)

    def test_purge_domain_and_since(self):
        from mailtrim.cli.main import app

        mock_client, groups = self._make_client()
        captured_queries: list[str] = []

        def fake_fetch(client, query, **kwargs):
            captured_queries.append(query)
            return groups

        with (
            patch("mailtrim.cli.main._get_provider", return_value=mock_client),
            patch("mailtrim.cli.main._get_account_email", return_value="user@gmail.com"),
            patch("mailtrim.core.sender_stats.fetch_sender_groups", side_effect=fake_fetch),
            patch("mailtrim.core.storage.BlocklistRepo") as mock_bl,
        ):
            mock_bl.return_value.blocked_emails.return_value = set()
            result = runner.invoke(
                app,
                ["purge", "--domain", "example.com", "--since", "30d", "--json"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert any("newer_than:30d" in q and "from:example.com" in q for q in captured_queries)


# ── CLI validation errors ──────────────────────────────────────────────────────


class TestCLIValidationErrors:
    def _mock_provider(self):
        mock = MagicMock()
        mock.get_profile.return_value = {
            "emailAddress": "user@gmail.com",
            "messagesTotal": 0,
            "threadsTotal": 0,
        }
        return mock

    def test_stats_invalid_since_exits_1(self):
        from mailtrim.cli.main import app

        with patch("mailtrim.cli.main._get_provider", return_value=self._mock_provider()):
            result = runner.invoke(app, ["stats", "--since", "badvalue"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "Error" in result.output

    def test_stats_since_zero_exits_1(self):
        from mailtrim.cli.main import app

        with patch("mailtrim.cli.main._get_provider", return_value=self._mock_provider()):
            result = runner.invoke(app, ["stats", "--since", "0d"], catch_exceptions=False)
        assert result.exit_code == 1

    def test_stats_since_no_d_suffix_exits_1(self):
        from mailtrim.cli.main import app

        with patch("mailtrim.cli.main._get_provider", return_value=self._mock_provider()):
            result = runner.invoke(app, ["stats", "--since", "30"], catch_exceptions=False)
        assert result.exit_code == 1

    def test_purge_invalid_since_exits_1(self):
        from mailtrim.cli.main import app

        with patch("mailtrim.cli.main._get_provider", return_value=self._mock_provider()):
            result = runner.invoke(app, ["purge", "--since", "xyz"], catch_exceptions=False)
        assert result.exit_code == 1

    def test_stats_scope_label_includes_since(self):
        """When --since is used the scan label should mention the time window."""
        from mailtrim.cli.main import app

        mock_client = self._mock_provider()

        with (
            patch("mailtrim.cli.main._get_provider", return_value=mock_client),
            patch("mailtrim.core.sender_stats.fetch_sender_groups", return_value=[]),
        ):
            result = runner.invoke(app, ["stats", "--since", "7d"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "7d" in result.output
