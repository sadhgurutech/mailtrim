"""Tests for `mailtrim privacy` command output."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture(autouse=True)
def reset_config(tmp_path, monkeypatch):
    """
    Patch config module paths and reset the Settings singleton before each test.
    DATA_DIR and _STATS_PATH are module-level constants computed at import time,
    so env-var monkeypatching alone is not enough — we must patch attributes directly.
    """
    import mailtrim.config as cfg
    import mailtrim.core.usage_stats as us

    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "mailtrim.db")
    monkeypatch.setattr(cfg, "CREDENTIALS_PATH", tmp_path / "credentials.json")
    monkeypatch.setattr(cfg, "TOKEN_PATH", tmp_path / "token.json")
    monkeypatch.setattr(cfg, "UNDO_LOG_DIR", tmp_path / "undo_logs")
    monkeypatch.setattr(cfg, "_settings", None)  # reset singleton
    monkeypatch.setattr(us, "_STATS_PATH", tmp_path / "usage.json")


def _invoke(ai_mode: str = "off"):
    from mailtrim.cli.main import app

    return runner.invoke(
        app, ["privacy"], env={"MAILTRIM_AI_MODE": ai_mode}, catch_exceptions=False
    )


# ── AI mode messaging ─────────────────────────────────────────────────────────


def test_ai_mode_off():
    result = _invoke("off")
    assert result.exit_code == 0
    assert "No email data leaves your machine" in result.output


def test_ai_mode_local():
    result = _invoke("local")
    assert result.exit_code == 0
    assert "Processed locally" in result.output
    assert "nothing sent externally" in result.output


def test_ai_mode_cloud():
    result = _invoke("cloud")
    assert result.exit_code == 0
    assert "May send email subjects" in result.output
    assert "Anthropic" in result.output


# ── Data locations ────────────────────────────────────────────────────────────


def test_shows_data_dir_paths(tmp_path):
    result = _invoke()
    # Rich may wrap long paths across lines — join output before checking
    flat = result.output.replace("\n", "")
    assert str(tmp_path) in flat


def test_shows_all_stored_items():
    result = _invoke()
    assert "OAuth token" in result.output
    assert "Email database" in result.output
    assert "Undo logs" in result.output
    assert "Usage stats" in result.output
    assert "Config / env" in result.output


def test_file_exists_vs_not_created(tmp_path):
    # token does not exist → "not created yet"
    result = _invoke()
    assert "not created yet" in result.output

    # create token file → "exists"
    (tmp_path / "token.json").write_text("{}")
    result2 = _invoke()
    assert "exists" in result2.output


# ── Usage stats section ───────────────────────────────────────────────────────


def test_usage_stats_not_yet_created():
    result = _invoke()
    assert "Not yet created" in result.output


def test_usage_stats_shown_when_present(tmp_path):
    (tmp_path / "usage.json").write_text(
        json.dumps(
            {
                "total_runs": 7,
                "emails_trashed": 42,
                "first_run": "2026-05-01",
                "command_counts": {},
                "emails_restored": 0,
                "undo_count": 0,
                "version_first_seen": {},
            }
        )
    )
    result = _invoke()
    assert result.exit_code == 0
    assert "7" in result.output
    assert "42" in result.output


# ── Trust guarantees ──────────────────────────────────────────────────────────


def test_shows_trust_guarantees():
    result = _invoke()
    assert "No telemetry" in result.output
    assert "No email body" in result.output
    assert "No account data" in result.output


def test_shows_config_ai_mode_hint():
    result = _invoke()
    assert "mailtrim config ai-mode" in result.output
