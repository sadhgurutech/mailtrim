"""Tests for the doctor command diagnostics and error message translation."""

from __future__ import annotations

# ── CheckResult dataclass ────────────────────────────────────────────────────


def test_check_result_fields():
    import mailtrim.core.diagnostics as diag

    r = diag.CheckResult(name="Test", ok=True, message="All good")
    assert r.ok is True
    assert r.fix == ""
    assert r.optional is False


def test_check_result_failure_with_fix():
    import mailtrim.core.diagnostics as diag

    r = diag.CheckResult(name="Auth", ok=False, message="Token missing", fix="mailtrim auth")
    assert r.ok is False
    assert "mailtrim auth" in r.fix


def test_check_result_optional():
    import mailtrim.core.diagnostics as diag

    r = diag.CheckResult(name="AI", ok=False, message="Not reachable", optional=True)
    assert r.optional is True


# ── Token checks ─────────────────────────────────────────────────────────────


def test_check_token_exists_missing(tmp_path, monkeypatch):
    """Returns failure when TOKEN_PATH does not exist."""
    monkeypatch.setattr("mailtrim.config.TOKEN_PATH", tmp_path / "token.json")
    import importlib

    import mailtrim.core.diagnostics as diag

    importlib.reload(diag)
    result = diag.check_token_exists()
    assert result.ok is False
    assert "mailtrim auth" in result.fix


def test_check_token_exists_present(tmp_path, monkeypatch):
    token = tmp_path / "token.json"
    token.write_text("{}")
    monkeypatch.setattr("mailtrim.config.TOKEN_PATH", token)
    import importlib

    import mailtrim.core.diagnostics as diag

    importlib.reload(diag)
    result = diag.check_token_exists()
    assert result.ok is True


# ── Data dir / config checks ─────────────────────────────────────────────────


def test_check_data_dir_writable(tmp_path, monkeypatch):
    monkeypatch.setattr("mailtrim.config.DATA_DIR", tmp_path)
    import importlib

    import mailtrim.core.diagnostics as diag

    importlib.reload(diag)
    result = diag.check_data_dir()
    assert result.ok is True


def test_check_data_dir_unwritable(tmp_path, monkeypatch):
    """Simulate unwritable directory by making it read-only."""
    import os

    ro_dir = tmp_path / "ro"
    ro_dir.mkdir()
    os.chmod(ro_dir, 0o444)
    monkeypatch.setattr("mailtrim.config.DATA_DIR", ro_dir)
    import importlib

    import mailtrim.core.diagnostics as diag

    importlib.reload(diag)
    result = diag.check_data_dir()
    # restore permissions so tmp cleanup works
    os.chmod(ro_dir, 0o755)
    assert result.ok is False
    assert result.fix != ""


# ── Dependencies check ────────────────────────────────────────────────────────


def test_check_dependencies_all_present():
    import mailtrim.core.diagnostics as diag

    result = diag.check_dependencies()
    assert result.ok is True
    assert "All required" in result.message


# ── AI endpoint check ─────────────────────────────────────────────────────────


def test_check_ai_endpoint_unreachable():
    import mailtrim.core.diagnostics as diag

    result = diag.check_ai_endpoint(url="http://127.0.0.1:19999")  # unused port
    assert result.ok is False
    assert result.optional is True


# ── run_all ───────────────────────────────────────────────────────────────────


def test_run_all_returns_list():
    from mailtrim.core.diagnostics import run_all

    results = run_all(include_optional=False)
    assert isinstance(results, list)
    assert len(results) > 0
    for r in results:
        assert hasattr(r, "ok")
        assert hasattr(r, "name")


def test_run_all_optional_included():
    from mailtrim.core.diagnostics import run_all

    with_opt = run_all(include_optional=True)
    without_opt = run_all(include_optional=False)
    assert len(with_opt) >= len(without_opt)


# ── friendly_error ────────────────────────────────────────────────────────────


def test_friendly_error_invalid_grant():
    from mailtrim.core.errors import friendly_error

    exc = Exception("invalid_grant: Token has been expired or revoked.")
    msg, fix = friendly_error(exc)
    assert "expired" in msg.lower()
    assert "mailtrim auth" in fix


def test_friendly_error_timeout():
    from mailtrim.core.errors import friendly_error

    exc = Exception("Connection timed out trying to reach Gmail API")
    msg, fix = friendly_error(exc)
    assert "internet" in msg.lower() or "reach" in msg.lower()


def test_friendly_error_permission_denied():
    from mailtrim.core.errors import friendly_error

    exc = PermissionError("Permission denied: '/Users/x/.mailtrim/token.json'")
    msg, fix = friendly_error(exc)
    assert "permission" in msg.lower() or "write" in msg.lower()
    assert "~/.mailtrim" in fix


def test_friendly_error_credentials_missing():
    from mailtrim.core.errors import friendly_error

    exc = FileNotFoundError("OAuth credentials file not found at /path/credentials.json.")
    msg, fix = friendly_error(exc)
    assert "not found" in msg.lower() or "credential" in msg.lower()


def test_friendly_error_rate_limit():
    from mailtrim.core.errors import friendly_error

    exc = Exception("HttpError 429 when requesting... rateLimitExceeded")
    msg, fix = friendly_error(exc)
    assert "rate limit" in msg.lower()
    assert "60 seconds" in fix or "wait" in fix.lower()


def test_friendly_error_403():
    from mailtrim.core.errors import friendly_error

    exc = Exception("HttpError 403 when requesting... 403")
    msg, fix = friendly_error(exc)
    assert "denied" in msg.lower() or "permission" in msg.lower()


def test_friendly_error_database_corrupt():
    from mailtrim.core.errors import friendly_error

    exc = Exception("disk image is malformed")
    msg, fix = friendly_error(exc)
    assert "corrupt" in msg.lower() or "database" in msg.lower()
    assert "mailtrim.db" in fix


def test_friendly_error_unknown_falls_back():
    from mailtrim.core.errors import friendly_error

    exc = RuntimeError("something completely unexpected happened xyz123")
    msg, fix = friendly_error(exc)
    assert "xyz123" in msg or "unexpected" in msg.lower()
    assert "github" in fix


# ── usage stats ───────────────────────────────────────────────────────────────


def test_usage_stats_record_run(tmp_path, monkeypatch):
    monkeypatch.setattr("mailtrim.core.usage_stats._STATS_PATH", tmp_path / "usage.json")
    from mailtrim.core.usage_stats import get_stats, record_run

    record_run("stats")
    record_run("stats")
    record_run("purge")
    data = get_stats()
    assert data["command_counts"]["stats"] == 2
    assert data["command_counts"]["purge"] == 1
    assert data["total_runs"] == 3
    assert data["first_run"] is not None


def test_usage_stats_emails_trashed(tmp_path, monkeypatch):
    monkeypatch.setattr("mailtrim.core.usage_stats._STATS_PATH", tmp_path / "usage.json")
    from mailtrim.core.usage_stats import get_stats, record_emails_trashed

    record_emails_trashed(50)
    record_emails_trashed(25)
    assert get_stats()["emails_trashed"] == 75


def test_usage_stats_undo(tmp_path, monkeypatch):
    monkeypatch.setattr("mailtrim.core.usage_stats._STATS_PATH", tmp_path / "usage.json")
    from mailtrim.core.usage_stats import get_stats, record_undo

    record_undo(restored=30)
    data = get_stats()
    assert data["undo_count"] == 1
    assert data["emails_restored"] == 30


def test_usage_stats_format_summary(tmp_path, monkeypatch):
    monkeypatch.setattr("mailtrim.core.usage_stats._STATS_PATH", tmp_path / "usage.json")
    from mailtrim.core.usage_stats import format_summary, record_emails_trashed, record_run

    record_run("purge")
    record_emails_trashed(100)
    summary = format_summary()
    assert "100" in summary
    assert "1 run" in summary or "runs" in summary
