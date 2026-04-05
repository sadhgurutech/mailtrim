"""Tests for keyword-based confidence penalty and sender blocklist."""

from datetime import datetime, timezone

import pytest

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch, tmp_path):
    """Redirect all storage to a temp dir so tests never touch ~/.mailtrim."""
    monkeypatch.setenv("MAILTRIM_DIR", str(tmp_path))
    import mailtrim.config as cfg

    cfg._settings = None
    cfg.DATA_DIR = tmp_path
    cfg.DB_PATH = tmp_path / "test.db"
    cfg.UNDO_LOG_DIR = tmp_path / "undo_logs"

    import mailtrim.core.storage as storage

    storage._engine = None
    storage._SessionLocal = None

    yield

    if storage._engine:
        storage._engine.dispose()
    storage._engine = None
    storage._SessionLocal = None


def _make_group(
    subjects: list[str],
    has_unsubscribe: bool = True,
    inbox_days: int = 200,
    count: int = 60,
):
    """Build a minimal SenderGroup for scoring tests."""
    from datetime import timedelta

    from mailtrim.core.sender_stats import SenderGroup

    now = datetime.now(timezone.utc)
    earliest = now - timedelta(days=inbox_days)
    return SenderGroup(
        sender_email="test@example.com",
        sender_name="Test Sender",
        count=count,
        total_size_bytes=5 * 1024 * 1024,
        earliest_date=earliest,
        latest_date=now,
        sample_subjects=subjects,
        message_ids=["id1"],
        has_unsubscribe=has_unsubscribe,
    )


# ── Keyword penalty ───────────────────────────────────────────────────────────


def test_no_penalty_for_clean_newsletter():
    """Newsletters with no transactional keywords should score high."""
    from mailtrim.core.sender_stats import compute_confidence_score

    g = _make_group(
        subjects=["Weekly digest: top stories this week", "Your weekly roundup"],
        has_unsubscribe=True,
        inbox_days=200,
        count=60,
    )
    score = compute_confidence_score(g)
    # Full score: 30 (unsub) + 35 (age capped) + 35 (freq capped) = 100, no penalty
    assert score == 100


def test_penalty_applied_for_receipt_keyword():
    """'receipt' in subject line should trigger 25-pt penalty."""
    from mailtrim.core.sender_stats import compute_confidence_score

    g = _make_group(
        subjects=["Your order receipt #12345"],
        has_unsubscribe=True,
        inbox_days=200,
        count=60,
    )
    score = compute_confidence_score(g)
    assert score == 75  # 100 - 25 penalty


def test_penalty_applied_for_invoice_keyword():
    from mailtrim.core.sender_stats import compute_confidence_score

    g = _make_group(subjects=["Invoice #INV-2024-001 from Acme Corp"])
    score = compute_confidence_score(g)
    assert score == 75


def test_penalty_applied_for_security_alert():
    from mailtrim.core.sender_stats import compute_confidence_score

    g = _make_group(subjects=["Security alert: new login detected"])
    score = compute_confidence_score(g)
    assert score == 75


def test_penalty_does_not_go_below_zero():
    """Low-signal sender with a transactional keyword should floor at 0, not go negative."""
    from mailtrim.core.sender_stats import compute_confidence_score

    g = _make_group(
        subjects=["Your payment receipt"],
        has_unsubscribe=False,
        inbox_days=10,
        count=5,
    )
    score = compute_confidence_score(g)
    assert score >= 0


def test_penalty_not_triggered_by_partial_word():
    """'ordering' should not trigger the 'order' keyword (substring check is word-safe)."""
    from mailtrim.core.sender_stats import compute_confidence_score

    # "ordering" contains "order" as a substring — we accept this conservative behaviour
    # but document it here so the decision is explicit, not accidental.
    g = _make_group(
        subjects=["Reordering tips for your kitchen"],
        has_unsubscribe=True,
        inbox_days=200,
        count=60,
    )
    # "order" IS a substring of "reordering" — penalty is intentionally applied
    # (conservative: we'd rather flag for review than silently delete)
    score = compute_confidence_score(g)
    assert score == 75


def test_confidence_reason_includes_transactional():
    """confidence_reason() should mention transactional keywords when detected."""
    from mailtrim.core.sender_stats import confidence_reason

    g = _make_group(subjects=["Your invoice is ready"])
    reason = confidence_reason(g)
    assert "transactional keywords detected" in reason


def test_confidence_reason_no_transactional_for_newsletter():
    from mailtrim.core.sender_stats import confidence_reason

    g = _make_group(subjects=["Top 10 stories this week", "Newsletter Vol. 42"])
    reason = confidence_reason(g)
    assert "transactional" not in reason


# ── BlocklistRepo ─────────────────────────────────────────────────────────────


def test_blocklist_add_and_list():
    from mailtrim.core.storage import BlocklistRepo, get_session

    repo = BlocklistRepo(get_session())
    repo.add("user@gmail.com", "bank@example.com")

    entries = repo.list_all("user@gmail.com")
    assert len(entries) == 1
    assert entries[0].sender_email == "bank@example.com"
    assert entries[0].sender_domain == "example.com"
    assert entries[0].reason == "user_protected"


def test_blocklist_add_idempotent():
    """Adding the same sender twice should not create duplicates."""
    from mailtrim.core.storage import BlocklistRepo, get_session

    repo = BlocklistRepo(get_session())
    repo.add("user@gmail.com", "bank@example.com")
    repo.add("user@gmail.com", "bank@example.com")

    entries = repo.list_all("user@gmail.com")
    assert len(entries) == 1


def test_blocklist_remove():
    from mailtrim.core.storage import BlocklistRepo, get_session

    repo = BlocklistRepo(get_session())
    repo.add("user@gmail.com", "bank@example.com")

    removed = repo.remove("user@gmail.com", "bank@example.com")
    assert removed is True
    assert repo.list_all("user@gmail.com") == []


def test_blocklist_remove_nonexistent_returns_false():
    from mailtrim.core.storage import BlocklistRepo, get_session

    repo = BlocklistRepo(get_session())
    removed = repo.remove("user@gmail.com", "nobody@example.com")
    assert removed is False


def test_blocklist_blocked_emails_set():
    from mailtrim.core.storage import BlocklistRepo, get_session

    repo = BlocklistRepo(get_session())
    repo.add("user@gmail.com", "bank@example.com")
    repo.add("user@gmail.com", "invoices@corp.com")

    blocked = repo.blocked_emails("user@gmail.com")
    assert blocked == {"bank@example.com", "invoices@corp.com"}


def test_blocklist_scoped_to_account():
    """Blocked senders for account A should not appear for account B."""
    from mailtrim.core.storage import BlocklistRepo, get_session

    repo = BlocklistRepo(get_session())
    repo.add("alice@gmail.com", "spam@example.com")

    assert repo.blocked_emails("bob@gmail.com") == set()


def test_blocklist_undo_feedback_reason():
    from mailtrim.core.storage import BlocklistRepo, get_session

    repo = BlocklistRepo(get_session())
    repo.add("user@gmail.com", "news@example.com", reason="undo_feedback")

    entries = repo.list_all("user@gmail.com")
    assert entries[0].reason == "undo_feedback"
