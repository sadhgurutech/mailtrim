"""Tests for `_resolve_imap_settings`, provider isolation, and provider indicator output.

Key invariants:
- Gmail mode: IMAP settings always zeroed (no stale bleed-through)
- IMAP mode: IMAP settings resolved from CLI > persisted config
- Fallback: empty provider setting → "gmail"
- Provider switches (IMAP → Gmail and Gmail → IMAP) work cleanly
- Provider indicator line is printed at the start of stats, quickstart, purge
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock, patch

from rich.console import Console

# ── _resolve_imap_settings unit tests ─────────────────────────────────────────


def _resolve(
    provider="",
    imap_server="",
    imap_user="",
    imap_port=993,
    imap_folder="INBOX",
):
    from mailtrim.cli.main import _resolve_imap_settings

    return _resolve_imap_settings(provider, imap_server, imap_user, imap_port, imap_folder)


class TestProviderFallback:
    """Provider defaults to 'gmail' when nothing is explicitly set."""

    def test_empty_cli_and_empty_settings_returns_gmail(self, monkeypatch):
        monkeypatch.setenv("MAILTRIM_PROVIDER", "")
        import mailtrim.config as config

        config._settings = None
        p, *_ = _resolve()
        assert p == "gmail"

    def test_cli_flag_wins_over_settings(self, monkeypatch):
        monkeypatch.setenv("MAILTRIM_PROVIDER", "gmail")
        import mailtrim.config as config

        config._settings = None
        p, *_ = _resolve(provider="imap")
        assert p == "imap"

    def test_persisted_gmail_returns_gmail(self):
        # conftest already sets MAILTRIM_PROVIDER=gmail
        p, *_ = _resolve()
        assert p == "gmail"

    def test_persisted_imap_returns_imap(self, monkeypatch):
        monkeypatch.setenv("MAILTRIM_PROVIDER", "imap")
        import mailtrim.config as config

        config._settings = None
        p, *_ = _resolve()
        assert p == "imap"


class TestGmailIsolation:
    """When the resolved provider is Gmail, IMAP settings must be zeroed."""

    def test_stale_imap_user_zeroed_for_gmail(self, monkeypatch):
        monkeypatch.setenv("MAILTRIM_PROVIDER", "gmail")
        monkeypatch.setenv("MAILTRIM_IMAP_USER", "old@example.com")
        monkeypatch.setenv("MAILTRIM_IMAP_SERVER", "imap.example.com")
        import mailtrim.config as config

        config._settings = None
        _, server, user, port, folder = _resolve()
        assert user == ""
        assert server == ""
        assert port == 993
        assert folder == "INBOX"

    def test_imap_cli_flags_ignored_when_gmail_provider(self):
        # CLI flags for IMAP should be ignored when provider resolves to gmail
        _, server, user, port, folder = _resolve(
            provider="gmail",
            imap_server="imap.example.com",
            imap_user="me@example.com",
            imap_port=993,
            imap_folder="INBOX",
        )
        assert server == ""
        assert user == ""

    def test_no_imap_password_prompt_possible_when_gmail(self, monkeypatch):
        """The IMAP password prompt guard relies on imap_user being empty in Gmail mode."""
        monkeypatch.setenv("MAILTRIM_PROVIDER", "gmail")
        monkeypatch.setenv("MAILTRIM_IMAP_USER", "user@example.com")
        import mailtrim.config as config

        config._settings = None
        _, _, imap_user, _, _ = _resolve()
        # Prompt condition: `provider == "imap" and imap_user and not imap_password`
        # With provider="gmail" and imap_user="" the condition is always False
        assert imap_user == ""


class TestImapResolution:
    """When provider is IMAP, settings flow through correctly."""

    def test_imap_server_from_settings(self, monkeypatch):
        monkeypatch.setenv("MAILTRIM_PROVIDER", "imap")
        monkeypatch.setenv("MAILTRIM_IMAP_SERVER", "imap.example.com")
        monkeypatch.setenv("MAILTRIM_IMAP_USER", "user@example.com")
        import mailtrim.config as config

        config._settings = None
        _, server, user, _, _ = _resolve()
        assert server == "imap.example.com"
        assert user == "user@example.com"

    def test_cli_flag_overrides_persisted_server(self, monkeypatch):
        monkeypatch.setenv("MAILTRIM_PROVIDER", "imap")
        monkeypatch.setenv("MAILTRIM_IMAP_SERVER", "imap.old.com")
        import mailtrim.config as config

        config._settings = None
        _, server, _, _, _ = _resolve(provider="imap", imap_server="imap.new.com")
        assert server == "imap.new.com"

    def test_custom_port_from_settings(self, monkeypatch):
        monkeypatch.setenv("MAILTRIM_PROVIDER", "imap")
        monkeypatch.setenv("MAILTRIM_IMAP_PORT", "1993")
        import mailtrim.config as config

        config._settings = None
        _, _, _, port, _ = _resolve()
        assert port == 1993

    def test_cli_port_overrides_settings_when_nondefault(self, monkeypatch):
        monkeypatch.setenv("MAILTRIM_PROVIDER", "imap")
        monkeypatch.setenv("MAILTRIM_IMAP_PORT", "1993")
        import mailtrim.config as config

        config._settings = None
        _, _, _, port, _ = _resolve(provider="imap", imap_port=2993)
        assert port == 2993

    def test_custom_folder_from_settings(self, monkeypatch):
        monkeypatch.setenv("MAILTRIM_PROVIDER", "imap")
        monkeypatch.setenv("MAILTRIM_IMAP_FOLDER", "Archive")
        import mailtrim.config as config

        config._settings = None
        _, _, _, _, folder = _resolve()
        assert folder == "Archive"


class TestProviderSwitching:
    """Switching providers via setup must not leave cross-provider state."""

    def test_switch_imap_to_gmail_clears_imap_user(self, monkeypatch):
        """After switching from IMAP to Gmail, imap_user must be empty."""
        # Simulate: was IMAP, user ran `setup` and chose Gmail → .env now has MAILTRIM_PROVIDER=gmail
        # and MAILTRIM_IMAP_* cleared. In tests we just set the env accordingly.
        monkeypatch.setenv("MAILTRIM_PROVIDER", "gmail")
        monkeypatch.setenv("MAILTRIM_IMAP_USER", "")
        monkeypatch.setenv("MAILTRIM_IMAP_SERVER", "")
        import mailtrim.config as config

        config._settings = None
        _, server, user, _, _ = _resolve()
        assert user == ""
        assert server == ""

    def test_switch_gmail_to_imap_returns_imap(self, monkeypatch):
        """After switching from Gmail to IMAP, provider must be 'imap'."""
        monkeypatch.setenv("MAILTRIM_PROVIDER", "imap")
        monkeypatch.setenv("MAILTRIM_IMAP_SERVER", "imap.example.com")
        monkeypatch.setenv("MAILTRIM_IMAP_USER", "user@example.com")
        import mailtrim.config as config

        config._settings = None
        p, server, user, _, _ = _resolve()
        assert p == "imap"
        assert server == "imap.example.com"
        assert user == "user@example.com"


# ── _print_provider_line output tests ─────────────────────────────────────────


def _capture_provider_line(provider: str, imap_server: str = "") -> str:
    """Call _print_provider_line and return the rendered text (markup stripped)."""
    from mailtrim.cli.main import _print_provider_line

    buf = StringIO()
    cap = Console(file=buf, highlight=False, no_color=True)
    with patch("mailtrim.cli.main.console", cap):
        _print_provider_line(provider, imap_server)
    return buf.getvalue().strip()


class TestProviderIndicatorOutput:
    def test_gmail_shows_provider_gmail(self):
        out = _capture_provider_line("gmail")
        assert "Provider: Gmail" in out

    def test_gmail_no_imap_detail(self):
        out = _capture_provider_line("gmail")
        assert "server:" not in out
        assert "imap" not in out.lower()

    def test_imap_shows_provider_imap(self):
        out = _capture_provider_line("imap", "imap.example.com")
        assert "Provider: IMAP" in out

    def test_imap_shows_server_name(self):
        out = _capture_provider_line("imap", "imap.example.com")
        assert "imap.example.com" in out

    def test_imap_no_server_omits_server_detail(self):
        out = _capture_provider_line("imap", "")
        assert "Provider: IMAP" in out
        assert "server:" not in out

    def test_output_is_single_line(self):
        gmail_out = _capture_provider_line("gmail")
        imap_out = _capture_provider_line("imap", "imap.example.com")
        assert "\n" not in gmail_out
        assert "\n" not in imap_out


class TestProviderIndicatorInCommands:
    """Smoke tests: provider line appears in stats, quickstart, purge output."""

    def _mock_gmail_client(self):
        c = MagicMock()
        c.get_profile.return_value = {
            "emailAddress": "user@gmail.com",
            "messagesTotal": 100,
            "threadsTotal": 80,
        }
        c.get_email_address.return_value = "user@gmail.com"
        return c

    def test_stats_shows_gmail_provider_line(self, monkeypatch):
        from typer.testing import CliRunner

        from mailtrim.cli.main import app

        monkeypatch.setenv("MAILTRIM_PROVIDER", "gmail")
        import mailtrim.config as config

        config._settings = None

        client = self._mock_gmail_client()
        with (
            patch("mailtrim.cli.main._get_provider", return_value=client),
            patch("mailtrim.core.sender_stats.fetch_sender_groups", return_value=[]),
        ):
            result = CliRunner().invoke(app, ["stats"], catch_exceptions=False)

        assert "Provider: Gmail" in result.output

    def test_stats_shows_imap_provider_line(self, monkeypatch):
        from typer.testing import CliRunner

        from mailtrim.cli.main import app

        monkeypatch.setenv("MAILTRIM_PROVIDER", "imap")
        monkeypatch.setenv("MAILTRIM_IMAP_SERVER", "imap.example.com")
        monkeypatch.setenv("MAILTRIM_IMAP_USER", "user@example.com")
        monkeypatch.setenv("MAILTRIM_IMAP_PASSWORD", "secret")
        import mailtrim.config as config

        config._settings = None

        client = self._mock_gmail_client()
        with (
            patch("mailtrim.cli.main._get_provider", return_value=client),
            patch("mailtrim.core.sender_stats.fetch_sender_groups", return_value=[]),
        ):
            result = CliRunner().invoke(app, ["stats"], catch_exceptions=False)

        assert "Provider: IMAP" in result.output
        assert "imap.example.com" in result.output

    def test_purge_shows_gmail_provider_line(self, monkeypatch):
        from typer.testing import CliRunner

        from mailtrim.cli.main import app

        monkeypatch.setenv("MAILTRIM_PROVIDER", "gmail")
        import mailtrim.config as config

        config._settings = None

        client = self._mock_gmail_client()
        with (
            patch("mailtrim.cli.main._get_provider", return_value=client),
            patch("mailtrim.cli.main._get_account_email", return_value="user@gmail.com"),
            patch("mailtrim.core.sender_stats.fetch_sender_groups", return_value=[]),
        ):
            result = CliRunner().invoke(app, ["purge"], input="q\n", catch_exceptions=False)

        assert "Provider: Gmail" in result.output

    def test_quickstart_shows_gmail_provider_line(self, monkeypatch):
        from typer.testing import CliRunner

        from mailtrim.cli.main import app

        monkeypatch.setenv("MAILTRIM_PROVIDER", "gmail")
        import mailtrim.config as config

        config._settings = None

        client = self._mock_gmail_client()
        with (
            patch("mailtrim.cli.main._get_provider", return_value=client),
            patch("mailtrim.core.sender_stats.fetch_sender_groups", return_value=[]),
        ):
            result = CliRunner().invoke(app, ["quickstart"], catch_exceptions=False)

        assert "Provider: Gmail" in result.output
