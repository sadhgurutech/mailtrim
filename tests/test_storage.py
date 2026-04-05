"""Tests for the storage layer."""

import json
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch, tmp_path):
    """Point all storage to a temp dir so tests don't touch ~/.mailtrim."""
    monkeypatch.setenv("MAILTRIM_DIR", str(tmp_path))
    # Reset module-level singletons
    import mailtrim.config as cfg

    cfg._settings = None
    cfg.DATA_DIR = tmp_path
    cfg.DB_PATH = tmp_path / "test.db"
    cfg.UNDO_LOG_DIR = tmp_path / "undo_logs"

    import mailtrim.core.storage as storage

    storage._engine = None
    storage._SessionLocal = None

    yield

    # Cleanup
    import mailtrim.core.storage as storage

    if storage._engine:
        storage._engine.dispose()
    storage._engine = None
    storage._SessionLocal = None


def test_email_record_upsert():
    from mailtrim.core.storage import EmailRecord, EmailRepo, get_session

    session = get_session()
    repo = EmailRepo(session)

    rec = EmailRecord(
        account_email="test@gmail.com",
        gmail_id="abc123",
        thread_id="thread1",
        subject="Test Subject",
        sender_email="sender@example.com",
        sender_name="Sender",
        snippet="Hello world",
        label_ids_json=json.dumps(["INBOX", "UNREAD"]),
        internal_date=1700000000000,
        is_unread=True,
        is_inbox=True,
    )
    repo.upsert(rec)

    fetched = repo.get("abc123")
    assert fetched is not None
    assert fetched.subject == "Test Subject"
    assert fetched.is_inbox is True

    # Upsert again with update
    rec.subject = "Updated Subject"
    repo.upsert(rec)
    fetched2 = repo.get("abc123")
    assert fetched2.subject == "Updated Subject"


def test_follow_up_lifecycle():
    from mailtrim.core.storage import FollowUp, FollowUpRepo, get_session

    session = get_session()
    repo = FollowUpRepo(session)

    now = datetime.now(timezone.utc)
    fu = FollowUp(
        account_email="test@gmail.com",
        sent_message_id="msg1",
        thread_id="thread1",
        to_email="recipient@example.com",
        subject="Follow up on proposal",
        sent_at=now,
        remind_at=now - timedelta(seconds=1),  # already due
        remind_only_if_no_reply=True,
    )
    repo.create(fu)

    due = repo.get_due("test@gmail.com")
    assert len(due) == 1
    assert due[0].subject == "Follow up on proposal"

    repo.mark_replied("thread1")
    # get_due filters replied=False, so replied threads no longer appear
    due_after = repo.get_due("test@gmail.com")
    assert len(due_after) == 0
    # But the record itself is updated
    record = session.query(FollowUp).filter_by(thread_id="thread1").first()
    assert record.replied is True


def test_undo_log_record_and_undo():
    from mailtrim.core.storage import UndoLogRepo, get_session

    session = get_session()
    repo = UndoLogRepo(session)

    entry = repo.record(
        account_email="test@gmail.com",
        operation="archive",
        message_ids=["id1", "id2", "id3"],
        description="Archived newsletters",
        metadata={"gmail_query": "label:newsletters"},
    )

    assert entry.id is not None
    assert entry.message_ids == ["id1", "id2", "id3"]
    assert entry.is_undone is False

    recent = repo.list_recent("test@gmail.com")
    assert len(recent) == 1

    repo.mark_undone(entry.id)
    entry_after = repo.get(entry.id)
    assert entry_after.is_undone is True
    assert entry_after.undone_at is not None


def test_rule_create_and_deactivate():
    from mailtrim.core.storage import RuleDefinition, RuleRepo, get_session

    session = get_session()
    repo = RuleRepo(session)

    rule = RuleDefinition(
        account_email="test@gmail.com",
        name="Archive old LinkedIn",
        natural_language="archive LinkedIn notifications older than 7 days",
        gmail_query="from:linkedin.com older_than:7d",
        action="archive",
    )
    rule.action_params = {}
    created = repo.create(rule)
    assert created.id is not None

    active = repo.list_active("test@gmail.com")
    assert len(active) == 1

    repo.deactivate(created.id)
    active_after = repo.list_active("test@gmail.com")
    assert len(active_after) == 0


def test_avoidance_view_tracking():
    from mailtrim.config import get_settings
    from mailtrim.core.storage import EmailRecord, EmailRepo, get_session

    session = get_session()
    repo = EmailRepo(session)

    rec = EmailRecord(
        account_email="test@gmail.com",
        gmail_id="avoid1",
        thread_id="t1",
        subject="That email I keep ignoring",
        sender_email="boss@company.com",
        is_inbox=True,
        is_acted_on=False,
        view_count=0,
        label_ids_json="[]",
    )
    repo.upsert(rec)

    threshold = get_settings().avoidance_view_threshold
    for _ in range(threshold):
        repo.increment_view("avoid1")

    avoided = repo.find_avoided("test@gmail.com")
    assert len(avoided) == 1
    assert avoided[0].view_count == threshold

    repo.mark_acted_on("avoid1")
    avoided_after = repo.find_avoided("test@gmail.com")
    assert len(avoided_after) == 0
