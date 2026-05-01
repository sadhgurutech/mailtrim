"""SQLite storage layer — all state lives locally, nothing leaves your machine."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

import mailtrim.config as _cfg
from mailtrim.config import get_settings

# ── Base ─────────────────────────────────────────────────────────────────────


class Base(DeclarativeBase):
    pass


# ── Models ───────────────────────────────────────────────────────────────────


class Account(Base):
    """A connected Gmail account."""

    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String, unique=True, nullable=False)
    display_name = Column(String, default="")
    added_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_synced_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)


class EmailRecord(Base):
    """Cached metadata for an email — avoids re-fetching from Gmail API."""

    __tablename__ = "emails"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_email = Column(String, nullable=False)
    gmail_id = Column(String, nullable=False, unique=True)
    thread_id = Column(String, nullable=False)
    subject = Column(String, default="")
    sender_email = Column(String, default="")
    sender_name = Column(String, default="")
    snippet = Column(Text, default="")
    label_ids_json = Column(Text, default="[]")  # JSON list of label IDs
    internal_date = Column(Integer, default=0)  # ms since epoch
    size_estimate = Column(Integer, default=0)
    is_unread = Column(Boolean, default=True)
    is_inbox = Column(Boolean, default=True)
    has_attachment = Column(Boolean, default=False)
    list_unsubscribe = Column(Text, default="")
    ai_category = Column(String, default="")  # AI-assigned category
    ai_explanation = Column(Text, default="")  # Why AI categorized it this way
    view_count = Column(Integer, default=0)  # Times surfaced but not acted on
    last_viewed_at = Column(DateTime, nullable=True)
    is_acted_on = Column(Boolean, default=False)  # Archived/replied/deleted
    synced_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    @property
    def label_ids(self) -> list[str]:
        return json.loads(self.label_ids_json or "[]")

    @label_ids.setter
    def label_ids(self, value: list[str]) -> None:
        self.label_ids_json = json.dumps(value)


class FollowUp(Base):
    """Sent emails being tracked for follow-up."""

    __tablename__ = "follow_ups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_email = Column(String, nullable=False)
    sent_message_id = Column(String, nullable=False)
    thread_id = Column(String, nullable=False)
    to_email = Column(String, nullable=False)
    subject = Column(String, default="")
    sent_at = Column(DateTime, nullable=False)
    remind_at = Column(DateTime, nullable=False)  # When to surface the reminder
    remind_only_if_no_reply = Column(Boolean, default=True)
    replied = Column(Boolean, default=False)  # A reply was detected
    replied_at = Column(DateTime, nullable=True)
    snoozed_until = Column(DateTime, nullable=True)
    dismissed = Column(Boolean, default=False)
    note = Column(Text, default="")


class UndoLogEntry(Base):
    """A reversible bulk operation — kept for undo_window_days."""

    __tablename__ = "undo_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_email = Column(String, nullable=False)
    operation = Column(String, nullable=False)  # "archive", "trash", "label", "unlabel"
    description = Column(Text, default="")  # Human-readable summary
    message_ids_json = Column(Text, nullable=False)  # JSON list of affected IDs
    metadata_json = Column(Text, default="{}")  # Extra data (label names etc.)
    executed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    undone_at = Column(DateTime, nullable=True)
    is_undone = Column(Boolean, default=False)
    expires_at = Column(DateTime, nullable=False)  # Auto-purge after this

    @property
    def message_ids(self) -> list[str]:
        return json.loads(self.message_ids_json)

    @message_ids.setter
    def message_ids(self, value: list[str]) -> None:
        self.message_ids_json = json.dumps(value)

    @property
    def op_metadata(self) -> dict:
        return json.loads(self.metadata_json or "{}")

    @op_metadata.setter
    def op_metadata(self, value: dict) -> None:
        self.metadata_json = json.dumps(value)


class RuleDefinition(Base):
    """A user-defined rule (either NL-defined or manually created)."""

    __tablename__ = "rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_email = Column(String, nullable=False)
    name = Column(String, nullable=False)
    natural_language = Column(Text, default="")  # Original NL input
    gmail_query = Column(Text, nullable=False)  # Translated Gmail search query
    action = Column(String, nullable=False)  # "archive", "trash", "label", "mark_read"
    action_params_json = Column(Text, default="{}")  # e.g. {"label": "newsletters"}
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_run_at = Column(DateTime, nullable=True)
    run_count = Column(Integer, default=0)
    ai_explanation = Column(Text, default="")

    @property
    def action_params(self) -> dict:
        return json.loads(self.action_params_json or "{}")

    @action_params.setter
    def action_params(self, value: dict) -> None:
        self.action_params_json = json.dumps(value)


class UnsubscribeRecord(Base):
    """Track unsubscribe attempts and their outcomes."""

    __tablename__ = "unsubscribes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_email = Column(String, nullable=False)
    sender_email = Column(String, nullable=False)
    sender_domain = Column(String, nullable=False)
    method = Column(String, default="")  # "header_mailto", "header_url", "headless"
    status = Column(String, default="pending")  # "pending", "success", "failed", "bounced"
    attempted_at = Column(DateTime, nullable=True)
    confirmed_at = Column(DateTime, nullable=True)
    last_received_at = Column(DateTime, nullable=True)  # Last email from this sender post-unsub


class SenderBlocklist(Base):
    """Senders the user has protected from future purge operations."""

    __tablename__ = "sender_blocklist"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_email = Column(String, nullable=False)
    sender_email = Column(String, nullable=False)
    sender_domain = Column(String, nullable=False)
    reason = Column(String, default="user_protected")  # "user_protected" | "undo_feedback"
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


# ── Engine / session factory ─────────────────────────────────────────────────


_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            f"sqlite:///{_cfg.DB_PATH}",
            connect_args={"check_same_thread": False},
            echo=False,
        )
        Base.metadata.create_all(_engine)
    return _engine


def get_session() -> Session:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionLocal()


# ── Repository helpers ───────────────────────────────────────────────────────


class EmailRepo:
    """CRUD for EmailRecord."""

    def __init__(self, session: Session):
        self.s = session

    def upsert(self, record: EmailRecord) -> None:
        existing = self.s.query(EmailRecord).filter_by(gmail_id=record.gmail_id).first()
        if existing:
            for col in EmailRecord.__table__.columns:
                if col.name not in ("id",):
                    setattr(existing, col.name, getattr(record, col.name))
        else:
            self.s.add(record)
        self.s.commit()

    def upsert_many(self, records: list[EmailRecord]) -> None:
        for r in records:
            self.upsert(r)

    def get(self, gmail_id: str) -> EmailRecord | None:
        return self.s.query(EmailRecord).filter_by(gmail_id=gmail_id).first()

    def get_inbox(self, account_email: str, limit: int = 100) -> list[EmailRecord]:
        return (
            self.s.query(EmailRecord)
            .filter_by(account_email=account_email, is_inbox=True)
            .order_by(EmailRecord.internal_date.desc())
            .limit(limit)
            .all()
        )

    def find_avoided(self, account_email: str, threshold: int | None = None) -> list[EmailRecord]:
        """Emails viewed >= threshold times but not acted on."""
        t = threshold or get_settings().avoidance_view_threshold
        return (
            self.s.query(EmailRecord)
            .filter(
                EmailRecord.account_email == account_email,
                EmailRecord.is_inbox.is_(True),
                EmailRecord.is_acted_on.is_(False),
                EmailRecord.view_count >= t,
            )
            .order_by(EmailRecord.view_count.desc())
            .all()
        )

    def increment_view(self, gmail_id: str) -> None:
        rec = self.get(gmail_id)
        if rec:
            rec.view_count += 1
            rec.last_viewed_at = datetime.now(timezone.utc)
            self.s.commit()

    def mark_acted_on(self, gmail_id: str) -> None:
        rec = self.get(gmail_id)
        if rec:
            rec.is_acted_on = True
            self.s.commit()


class FollowUpRepo:
    def __init__(self, session: Session):
        self.s = session

    def create(self, fu: FollowUp) -> FollowUp:
        self.s.add(fu)
        self.s.commit()
        return fu

    def get_due(self, account_email: str) -> list[FollowUp]:
        now = datetime.now(timezone.utc)
        return (
            self.s.query(FollowUp)
            .filter(
                FollowUp.account_email == account_email,
                FollowUp.remind_at <= now,
                FollowUp.replied.is_(False),
                FollowUp.dismissed.is_(False),
                FollowUp.snoozed_until.is_(None),
            )
            .all()
        )

    def mark_replied(self, thread_id: str) -> None:
        now = datetime.now(timezone.utc)
        rows = self.s.query(FollowUp).filter_by(thread_id=thread_id, replied=False).all()
        for r in rows:
            r.replied = True
            r.replied_at = now
        self.s.commit()

    def dismiss(self, follow_up_id: int) -> None:
        fu = self.s.get(FollowUp, follow_up_id)
        if fu:
            fu.dismissed = True
            self.s.commit()

    def snooze(self, follow_up_id: int, until: datetime) -> None:
        fu = self.s.get(FollowUp, follow_up_id)
        if fu:
            fu.snoozed_until = until
            self.s.commit()


class UndoLogRepo:
    def __init__(self, session: Session):
        self.s = session

    def record(
        self,
        account_email: str,
        operation: str,
        message_ids: list[str],
        description: str = "",
        metadata: dict | None = None,
    ) -> UndoLogEntry:
        from datetime import timedelta

        settings = get_settings()
        entry = UndoLogEntry(
            account_email=account_email,
            operation=operation,
            description=description,
            message_ids_json=json.dumps(message_ids),
            metadata_json=json.dumps(metadata or {}),
            expires_at=datetime.now(timezone.utc) + timedelta(days=settings.undo_window_days),
        )
        self.s.add(entry)
        self.s.commit()
        return entry

    def get(self, entry_id: int) -> UndoLogEntry | None:
        return self.s.get(UndoLogEntry, entry_id)

    def list_recent(self, account_email: str, limit: int = 20) -> list[UndoLogEntry]:
        return (
            self.s.query(UndoLogEntry)
            .filter_by(account_email=account_email, is_undone=False)
            .order_by(UndoLogEntry.executed_at.desc())
            .limit(limit)
            .all()
        )

    def mark_undone(self, entry_id: int) -> None:
        entry = self.get(entry_id)
        if entry:
            entry.is_undone = True
            entry.undone_at = datetime.now(timezone.utc)
            self.s.commit()

    def purge_expired(self) -> int:
        now = datetime.now(timezone.utc)
        count = self.s.query(UndoLogEntry).filter(UndoLogEntry.expires_at < now).delete()
        self.s.commit()
        return count


class RuleRepo:
    def __init__(self, session: Session):
        self.s = session

    def create(self, rule: RuleDefinition) -> RuleDefinition:
        self.s.add(rule)
        self.s.commit()
        return rule

    def list_active(self, account_email: str) -> list[RuleDefinition]:
        return (
            self.s.query(RuleDefinition)
            .filter_by(account_email=account_email, is_active=True)
            .all()
        )

    def deactivate(self, rule_id: int) -> None:
        r = self.s.get(RuleDefinition, rule_id)
        if r:
            r.is_active = False
            self.s.commit()

    def record_run(self, rule_id: int) -> None:
        r = self.s.get(RuleDefinition, rule_id)
        if r:
            r.last_run_at = datetime.now(timezone.utc)
            r.run_count += 1
            self.s.commit()


class BlocklistRepo:
    """CRUD for SenderBlocklist — senders protected from purge operations."""

    def __init__(self, session: Session):
        self.s = session

    def add(
        self,
        account_email: str,
        sender_email: str,
        reason: str = "user_protected",
    ) -> SenderBlocklist:
        """Add a sender to the blocklist. Idempotent — returns existing entry if already present."""
        domain = (
            sender_email.split("@")[-1].lower() if "@" in sender_email else sender_email.lower()
        )
        existing = (
            self.s.query(SenderBlocklist)
            .filter_by(account_email=account_email, sender_email=sender_email)
            .first()
        )
        if existing:
            return existing
        entry = SenderBlocklist(
            account_email=account_email,
            sender_email=sender_email,
            sender_domain=domain,
            reason=reason,
        )
        self.s.add(entry)
        self.s.commit()
        return entry

    def remove(self, account_email: str, sender_email: str) -> bool:
        """Remove a sender from the blocklist. Returns True if it existed."""
        entry = (
            self.s.query(SenderBlocklist)
            .filter_by(account_email=account_email, sender_email=sender_email)
            .first()
        )
        if not entry:
            return False
        self.s.delete(entry)
        self.s.commit()
        return True

    def list_all(self, account_email: str) -> list[SenderBlocklist]:
        return (
            self.s.query(SenderBlocklist)
            .filter_by(account_email=account_email)
            .order_by(SenderBlocklist.created_at.desc())
            .all()
        )

    def blocked_emails(self, account_email: str) -> set[str]:
        """Return the set of blocked sender email addresses for fast membership tests."""
        rows = (
            self.s.query(SenderBlocklist.sender_email).filter_by(account_email=account_email).all()
        )
        return {r.sender_email for r in rows}
