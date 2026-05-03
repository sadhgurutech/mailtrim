"""Tests for the AI trust boundary system.

Covers:
- ai_status_line() output for all three modes
- AIModeError formatting in _handle_error
- AI badge visible in stats, quickstart, doctor output
- Cloud AI warning panel shown before triage/bulk/avoid/digest
- Off-mode blocking with actionable message
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

runner = CliRunner()


# ── ai_status_line unit tests ─────────────────────────────────────────────────


class TestAiStatusLine:
    def _s(self, mode: str):
        from mailtrim.core.ai.mode import ai_status_line

        return ai_status_line(mode)

    def test_off_label(self):
        label, _, _ = self._s("off")
        assert label == "OFF"

    def test_off_note_mentions_no_data_leaves(self):
        _, note, _ = self._s("off")
        assert "no data leaves" in note

    def test_off_color_is_green(self):
        _, _, color = self._s("off")
        assert color == "green"

    def test_local_label(self):
        label, _, _ = self._s("local")
        assert label == "LOCAL"

    def test_local_note_mentions_machine(self):
        _, note, _ = self._s("local")
        assert "machine" in note

    def test_local_color_is_cyan(self):
        _, _, color = self._s("local")
        assert color == "cyan"

    def test_cloud_label(self):
        label, _, _ = self._s("cloud")
        assert label == "CLOUD"

    def test_cloud_note_mentions_anthropic(self):
        _, note, _ = self._s("cloud")
        assert "Anthropic" in note

    def test_cloud_color_is_yellow(self):
        _, _, color = self._s("cloud")
        assert color == "yellow"


# ── _handle_error AIModeError formatting ────────────────────────────────────


class TestHandleErrorAIModeError:
    def _invoke_triage_with_off(self):
        """Invoke triage with ai_mode=off so require_cloud raises AIModeError."""
        from mailtrim.cli.main import app

        mock_client = MagicMock()

        with (
            patch("mailtrim.cli.main._get_client", return_value=mock_client),
            patch(
                "mailtrim.cli.main.get_settings",
                return_value=MagicMock(ai_mode="off"),
            ),
        ):
            return runner.invoke(app, ["triage"], catch_exceptions=False)

    def test_off_mode_exits_1(self):
        result = self._invoke_triage_with_off()
        assert result.exit_code == 1

    def test_off_mode_prints_ai_blocked(self):
        result = self._invoke_triage_with_off()
        assert "AI blocked" in result.output

    def test_off_mode_shows_enable_command(self):
        result = self._invoke_triage_with_off()
        assert "mailtrim config ai-mode" in result.output

    def test_multiline_error_shows_secondary_lines(self):
        """Each line of AIModeError.message is printed, not just the first."""
        result = self._invoke_triage_with_off()
        # The full message has 3 lines; check at least one secondary line appears
        flat = result.output.replace("\n", " ")
        assert "ai-mode" in flat.lower()


# ── AI badge in stats output ──────────────────────────────────────────────────


class TestStatsBadge:
    def _invoke_stats(self, mode: str = "off"):
        from mailtrim.cli.main import app

        mock_client = MagicMock()
        mock_client.get_profile.return_value = {
            "emailAddress": "user@gmail.com",
            "messagesTotal": 0,
            "threadsTotal": 0,
        }

        with (
            patch("mailtrim.cli.main._get_provider", return_value=mock_client),
            patch("mailtrim.core.sender_stats.fetch_sender_groups", return_value=[]),
            patch("mailtrim.cli.main._record"),
            patch(
                "mailtrim.cli.main.get_settings",
                return_value=MagicMock(ai_mode=mode),
            ),
        ):
            return runner.invoke(app, ["stats"], catch_exceptions=False)

    def test_stats_shows_ai_badge(self):
        result = self._invoke_stats("off")
        assert result.exit_code == 0
        assert "AI:" in result.output

    def test_stats_off_mode_shows_off_label(self):
        result = self._invoke_stats("off")
        assert "OFF" in result.output

    def test_stats_off_mode_shows_no_data_leaves(self):
        result = self._invoke_stats("off")
        assert "no data leaves" in result.output

    def test_stats_local_mode_shows_local_label(self):
        result = self._invoke_stats("local")
        assert "LOCAL" in result.output

    def test_stats_cloud_mode_shows_cloud_label(self):
        result = self._invoke_stats("cloud")
        assert "CLOUD" in result.output

    def test_stats_cloud_mode_mentions_anthropic(self):
        result = self._invoke_stats("cloud")
        assert "Anthropic" in result.output


# ── AI badge in quickstart output ────────────────────────────────────────────


class TestQuickstartBadge:
    def _invoke_quickstart(self, mode: str = "off"):
        from mailtrim.cli.main import app

        mock_client = MagicMock()
        mock_client.get_profile.return_value = {
            "emailAddress": "user@gmail.com",
            "messagesTotal": 0,
            "threadsTotal": 0,
        }

        with (
            patch("mailtrim.cli.main._get_provider", return_value=mock_client),
            patch("mailtrim.core.sender_stats.fetch_sender_groups", return_value=[]),
            patch(
                "mailtrim.cli.main.get_settings",
                return_value=MagicMock(ai_mode=mode, provider="gmail"),
            ),
        ):
            return runner.invoke(app, ["quickstart"], catch_exceptions=False)

    def test_quickstart_shows_ai_badge(self):
        result = self._invoke_quickstart("off")
        assert result.exit_code == 0
        assert "AI:" in result.output

    def test_quickstart_off_shows_off(self):
        result = self._invoke_quickstart("off")
        assert "OFF" in result.output

    def test_quickstart_local_shows_local(self):
        result = self._invoke_quickstart("local")
        assert "LOCAL" in result.output

    def test_quickstart_cloud_mentions_anthropic(self):
        result = self._invoke_quickstart("cloud")
        assert "Anthropic" in result.output


# ── AI badge in doctor output ────────────────────────────────────────────────


class TestDoctorBadge:
    def _invoke_doctor(self, mode: str = "off"):
        from mailtrim.cli.main import app
        from mailtrim.core.diagnostics import CheckResult

        ok_results = [CheckResult("Auth token valid", ok=True, message="ok")]

        with (
            patch("mailtrim.core.diagnostics.run_all", return_value=ok_results),
            patch(
                "mailtrim.cli.main.get_settings",
                return_value=MagicMock(ai_mode=mode),
            ),
        ):
            return runner.invoke(app, ["doctor"], catch_exceptions=False)

    def test_doctor_shows_ai_badge(self):
        result = self._invoke_doctor("off")
        assert result.exit_code == 0
        assert "AI:" in result.output

    def test_doctor_off_shows_off_label(self):
        result = self._invoke_doctor("off")
        assert "OFF" in result.output

    def test_doctor_cloud_shows_cloud_label(self):
        result = self._invoke_doctor("cloud")
        assert "CLOUD" in result.output


# ── Cloud warning panel for AI commands ────────────────────────────────────


class TestCloudWarning:
    @pytest.fixture(autouse=True)
    def _use_clean_db(self, clean_db):
        """Provide an in-memory DB so bulk/avoid/digest don't fail on missing DB dir."""

    def _invoke_with_cloud_mode(self, command: list[str]):
        """
        Stub out the command's heavy work but let require_cloud + _cloud_ai_warning run.
        """
        from mailtrim.cli.main import app

        mock_client = MagicMock()
        mock_client.get_email_address.return_value = "user@gmail.com"
        mock_client.list_message_ids.return_value = []
        mock_client.get_messages_batch.return_value = []

        with (
            patch("mailtrim.cli.main._get_client", return_value=mock_client),
            patch("mailtrim.cli.main._get_provider", return_value=mock_client),
            patch(
                "mailtrim.cli.main.get_settings",
                return_value=MagicMock(ai_mode="cloud"),
            ),
        ):
            return runner.invoke(app, command, catch_exceptions=False)

    def test_triage_shows_cloud_warning(self):
        result = self._invoke_with_cloud_mode(["triage"])
        assert "Cloud AI is enabled" in result.output

    def test_bulk_shows_cloud_warning(self):
        result = self._invoke_with_cloud_mode(["bulk", "delete old newsletters"])
        assert "Cloud AI is enabled" in result.output

    def test_avoid_shows_cloud_warning(self):
        result = self._invoke_with_cloud_mode(["avoid"])
        assert "Cloud AI is enabled" in result.output

    def test_digest_shows_cloud_warning(self):
        result = self._invoke_with_cloud_mode(["digest"])
        assert "Cloud AI is enabled" in result.output

    def test_triage_warning_mentions_anthropic(self):
        result = self._invoke_with_cloud_mode(["triage"])
        assert "Anthropic" in result.output

    def test_triage_warning_mentions_disable(self):
        result = self._invoke_with_cloud_mode(["triage"])
        assert "mailtrim config ai-mode off" in result.output


# ── Off-mode blocking ─────────────────────────────────────────────────────────


class TestOffModeBlocking:
    def _invoke_cloud_cmd_with_off(self, command: list[str]):
        from mailtrim.cli.main import app

        mock_client = MagicMock()

        with (
            patch("mailtrim.cli.main._get_client", return_value=mock_client),
            patch("mailtrim.cli.main._get_provider", return_value=mock_client),
            patch(
                "mailtrim.cli.main.get_settings",
                return_value=MagicMock(ai_mode="off"),
            ),
        ):
            return runner.invoke(app, command, catch_exceptions=False)

    def test_triage_off_exits_1(self):
        result = self._invoke_cloud_cmd_with_off(["triage"])
        assert result.exit_code == 1

    def test_bulk_off_exits_1(self):
        result = self._invoke_cloud_cmd_with_off(["bulk", "delete old mail"])
        assert result.exit_code == 1

    def test_avoid_off_exits_1(self):
        result = self._invoke_cloud_cmd_with_off(["avoid"])
        assert result.exit_code == 1

    def test_digest_off_exits_1(self):
        result = self._invoke_cloud_cmd_with_off(["digest"])
        assert result.exit_code == 1

    def test_triage_off_shows_enable_hint(self):
        result = self._invoke_cloud_cmd_with_off(["triage"])
        assert "mailtrim config ai-mode" in result.output

    def test_bulk_off_no_cloud_warning(self):
        """_cloud_ai_warning must NOT fire when mode is off (blocked before reaching it)."""
        result = self._invoke_cloud_cmd_with_off(["bulk", "delete old mail"])
        assert "Cloud AI is enabled" not in result.output
