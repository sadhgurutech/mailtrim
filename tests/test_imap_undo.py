"""Tests for IMAP-compatible undo and IMAPProvider.batch_untrash."""

from __future__ import annotations

import imaplib
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture(autouse=True)
def _use_clean_db(clean_db):
    pass


# ── batch_untrash unit tests ──────────────────────────────────────────────────


class TestIMAPBatchUntrash:
    def _make_provider(self):
        from mailtrim.core.providers.imap import IMAPProvider

        p = IMAPProvider.__new__(IMAPProvider)
        p._server = "imap.example.com"
        p._user = "user@example.com"
        p._password = "secret"
        p._port = 993
        p._default_folder = "INBOX"
        p._conn = None
        p._selected_folder = None
        p._trash_folder = None  # cache starts empty
        return p

    def test_empty_ids_returns_zero(self):
        p = self._make_provider()
        assert p.batch_untrash([]) == 0

    def test_no_trash_folder_returns_zero(self):
        p = self._make_provider()
        with patch.object(p, "_get_trash_folder", return_value=None):
            with patch.object(p, "_ensure_connected"):
                assert p.batch_untrash(["uid1", "uid2"]) == 0

    def test_move_succeeds_returns_count(self):
        p = self._make_provider()
        mock_conn = MagicMock()
        mock_conn.select.return_value = ("OK", [b"10"])
        mock_conn.uid.return_value = ("OK", [b""])

        with patch.object(p, "_get_trash_folder", return_value="Trash"):
            with patch.object(p, "_ensure_connected", return_value=mock_conn):
                p._conn = mock_conn
                count = p.batch_untrash(["uid1", "uid2"])

        assert count == 2
        # MOVE should be attempted
        mock_conn.uid.assert_any_call("MOVE", "uid1,uid2", "INBOX")

    def test_move_not_supported_falls_back_to_copy_delete(self):
        p = self._make_provider()
        mock_conn = MagicMock()
        mock_conn.select.return_value = ("OK", [b"10"])

        # MOVE raises IMAP4.error (not supported), COPY succeeds
        def uid_side_effect(cmd, *args):
            if cmd == "MOVE":
                raise imaplib.IMAP4.error("MOVE not supported")
            if cmd == "COPY":
                return ("OK", [b""])
            return ("OK", [b""])

        mock_conn.uid.side_effect = uid_side_effect

        with patch.object(p, "_get_trash_folder", return_value="Trash"):
            with patch.object(p, "_ensure_connected", return_value=mock_conn):
                p._conn = mock_conn
                count = p.batch_untrash(["uid1"])

        assert count == 1

    def test_select_failure_returns_zero(self):
        p = self._make_provider()
        mock_conn = MagicMock()
        mock_conn.select.return_value = ("NO", [b"Permission denied"])

        with patch.object(p, "_get_trash_folder", return_value="Trash"):
            with patch.object(p, "_ensure_connected", return_value=mock_conn):
                p._conn = mock_conn
                count = p.batch_untrash(["uid1"])

        assert count == 0

    def test_both_move_and_copy_fail_returns_zero(self):
        p = self._make_provider()
        mock_conn = MagicMock()
        mock_conn.select.return_value = ("OK", [b"10"])
        mock_conn.uid.side_effect = Exception("network error")

        with patch.object(p, "_get_trash_folder", return_value="Trash"):
            with patch.object(p, "_ensure_connected", return_value=mock_conn):
                p._conn = mock_conn
                count = p.batch_untrash(["uid1"])

        assert count == 0


# ── GmailProvider.batch_untrash ───────────────────────────────────────────────


def test_gmail_batch_untrash_calls_untrash_per_id():
    from mailtrim.core.providers.gmail import GmailProvider

    mock_client = MagicMock()
    provider = GmailProvider(client=mock_client)

    count = provider.batch_untrash(["id1", "id2", "id3"])

    assert count == 3
    assert mock_client.untrash.call_count == 3
    mock_client.untrash.assert_any_call("id1")
    mock_client.untrash.assert_any_call("id2")
    mock_client.untrash.assert_any_call("id3")


def test_gmail_batch_untrash_empty_returns_zero():
    from mailtrim.core.providers.gmail import GmailProvider

    provider = GmailProvider(client=MagicMock())
    assert provider.batch_untrash([]) == 0


# ── undo CLI — IMAP path ─────────────────────────────────────────────────────


def _seed_undo_log(account_email: str, message_ids: list[str]) -> int:
    """Insert a trash undo log entry and return its ID."""
    from mailtrim.core.storage import UndoLogRepo, get_session

    repo = UndoLogRepo(get_session())
    entry = repo.record(
        account_email=account_email,
        operation="trash",
        message_ids=message_ids,
        description="Test purge",
        metadata={"senders": ["news@example.com"]},
    )
    return entry.id


class TestUndoIMAP:
    def _invoke(self, log_id: int | None, restore_count: int = 3) -> object:
        from mailtrim.cli.main import app

        mock_provider = MagicMock()
        mock_provider.batch_untrash.return_value = restore_count

        args = [
            "undo",
            "--provider",
            "imap",
            "--imap-server",
            "imap.example.com",
            "--imap-user",
            "user@example.com",
            "--yes",
        ]
        if log_id is not None:
            args.insert(1, str(log_id))

        with (
            patch("mailtrim.cli.main._get_provider", return_value=mock_provider),
            patch("mailtrim.cli.main._record"),
        ):
            return runner.invoke(
                app,
                args,
                catch_exceptions=False,
                env={"MAILTRIM_IMAP_PASSWORD": "testpass"},
            )

    def test_list_shows_recent_operations(self):
        _seed_undo_log("user@example.com", ["id1", "id2"])
        result = self._invoke(log_id=None)
        assert result.exit_code == 0
        # Table should show the operation type and message count
        assert "trash" in result.output
        assert "2" in result.output

    def test_restore_calls_batch_untrash(self):
        from mailtrim.cli.main import app

        ids = ["uid1", "uid2", "uid3"]
        log_id = _seed_undo_log("user@example.com", ids)

        mock_provider = MagicMock()
        mock_provider.batch_untrash.return_value = 3

        with (
            patch("mailtrim.cli.main._get_provider", return_value=mock_provider),
            patch("mailtrim.cli.main._record"),
        ):
            result = runner.invoke(
                app,
                [
                    "undo",
                    str(log_id),
                    "--provider",
                    "imap",
                    "--imap-server",
                    "imap.example.com",
                    "--imap-user",
                    "user@example.com",
                    "--yes",
                ],
                catch_exceptions=False,
                env={"MAILTRIM_IMAP_PASSWORD": "testpass"},
            )

        assert result.exit_code == 0
        mock_provider.batch_untrash.assert_called_once_with(ids)

    def test_restore_shows_success_message(self):
        log_id = _seed_undo_log("user@example.com", ["id1"])
        result = self._invoke(log_id=log_id, restore_count=1)
        assert result.exit_code == 0
        assert "Restored" in result.output

    def test_partial_restore_shows_warning(self):
        log_id = _seed_undo_log("user@example.com", ["id1", "id2", "id3"])
        result = self._invoke(log_id=log_id, restore_count=1)  # only 1 of 3 restored
        assert result.exit_code == 0
        assert "Partial restore" in result.output or "1 of 3" in result.output

    def test_unsupported_operation_exits_with_message(self):
        from mailtrim.core.storage import UndoLogRepo, get_session

        repo = UndoLogRepo(get_session())
        entry = repo.record(
            account_email="user@example.com",
            operation="archive",  # not supported for IMAP
            message_ids=["id1"],
            description="Archive test",
            metadata={},
        )

        result = self._invoke(log_id=entry.id)
        assert result.exit_code == 1
        assert "archive" in result.output.lower() or "not supported" in result.output.lower()

    def test_missing_imap_user_exits(self):
        from mailtrim.cli.main import app

        result = runner.invoke(
            app,
            ["undo", "--provider", "imap", "--imap-server", "imap.example.com", "--yes"],
            catch_exceptions=False,
            env={"MAILTRIM_IMAP_PASSWORD": "testpass"},
        )
        assert result.exit_code == 1
        assert "imap-user" in result.output.lower()


# ── purge domain mode creates undo log ────────────────────────────────────────


def test_purge_domain_mode_creates_undo_log():
    """Domain-mode purge must record an undo log so the user can restore."""
    from datetime import datetime, timedelta, timezone

    from mailtrim.cli.main import app
    from mailtrim.core.sender_stats import SenderGroup
    from mailtrim.core.storage import UndoLogRepo, get_session

    now = datetime.now(timezone.utc)
    group = SenderGroup(
        sender_email="news@example.com",
        sender_name="Newsletter",
        count=5,
        total_size_bytes=1024 * 1024,
        earliest_date=now - timedelta(days=60),
        latest_date=now,
        sample_subjects=["Test"],
        message_ids=["id1", "id2", "id3", "id4", "id5"],
        has_unsubscribe=True,
        impact_score=80,
    )

    mock_client = MagicMock()
    mock_client.get_profile.return_value = {
        "emailAddress": "user@gmail.com",
        "messagesTotal": 100,
        "threadsTotal": 50,
    }
    mock_client.get_email_address.return_value = "user@gmail.com"
    mock_client.batch_trash.return_value = 5

    with (
        patch("mailtrim.cli.main._get_provider", return_value=mock_client),
        patch("mailtrim.core.sender_stats.fetch_sender_groups", return_value=[group]),
        patch("mailtrim.cli.main._record"),
    ):
        result = runner.invoke(
            app,
            ["purge", "--domain", "example.com", "--yes"],
            catch_exceptions=False,
        )

    assert result.exit_code == 0

    # Verify undo log was created
    repo = UndoLogRepo(get_session())
    entries = repo.list_recent("user@gmail.com")
    assert len(entries) == 1
    assert entries[0].operation == "trash"
    assert "example.com" in entries[0].description

    # Verify undo ID is shown in output
    assert "mailtrim undo" in result.output
