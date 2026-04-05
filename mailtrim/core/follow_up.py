"""Conditional follow-up tracker — the "remind me only if they haven't replied" engine."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from mailtrim.config import get_settings
from mailtrim.core.gmail_client import GmailClient, Message
from mailtrim.core.storage import FollowUp, FollowUpRepo, get_session


class FollowUpTracker:
    """
    Tracks sent emails and surfaces follow-up reminders only when a reply
    has NOT arrived — solving the #1 gap in every existing email tool.
    """

    def __init__(self, client: GmailClient, account_email: str):
        self.client = client
        self.account_email = account_email
        self.session = get_session()
        self.repo = FollowUpRepo(self.session)

    def track(
        self,
        message: Message,
        remind_in_days: int | None = None,
        remind_only_if_no_reply: bool = True,
        note: str = "",
    ) -> FollowUp:
        """
        Start tracking a sent (or inbox) message for follow-up.

        Args:
            message: The sent Message to track.
            remind_in_days: Days until reminder fires. Defaults to settings value.
            remind_only_if_no_reply: Only remind if no reply has arrived by remind_at.
            note: Optional note to surface with the reminder.
        """
        settings = get_settings()
        days = remind_in_days or settings.follow_up_default_days
        sent_at = datetime.fromtimestamp(message.timestamp, tz=timezone.utc)
        remind_at = datetime.now(timezone.utc) + timedelta(days=days)

        fu = FollowUp(
            account_email=self.account_email,
            sent_message_id=message.id,
            thread_id=message.thread_id,
            to_email=message.headers.to,
            subject=message.headers.subject,
            sent_at=sent_at,
            remind_at=remind_at,
            remind_only_if_no_reply=remind_only_if_no_reply,
            note=note,
        )
        return self.repo.create(fu)

    def sync_replies(self) -> int:
        """
        Check if any tracked threads have received a reply.
        Updates FollowUp.replied for any that have.
        Returns count of newly-detected replies.
        """
        pending = (
            self.session.query(FollowUp)
            .filter_by(account_email=self.account_email, replied=False, dismissed=False)
            .all()
        )

        # Deduplicate by thread_id to minimize API calls
        thread_ids = list({fu.thread_id for fu in pending})
        detected = 0

        for thread_id in thread_ids:
            try:
                thread = self.client.get_thread(thread_id)
            except Exception:
                continue

            # A reply has arrived if the thread has more than 1 message
            # AND one of the later messages is NOT from our account
            if len(thread.messages) <= 1:
                continue

            for msg in thread.messages[1:]:
                reply_from = msg.headers.from_
                # A reply is any message NOT sent by the original sender
                if self.account_email.lower() not in reply_from.lower():
                    self.repo.mark_replied(thread_id)
                    detected += 1
                    break

        return detected

    def get_due_follow_ups(self) -> list[FollowUp]:
        """
        Return follow-ups that are due AND (if remind_only_if_no_reply) haven't been replied to.
        This is the key differentiator — we only surface what actually needs attention.
        """
        due = self.repo.get_due(self.account_email)

        result = []
        for fu in due:
            if fu.remind_only_if_no_reply and fu.replied:
                # They replied — no need to bother the user
                continue
            result.append(fu)

        return result

    def dismiss(self, follow_up_id: int) -> None:
        self.repo.dismiss(follow_up_id)

    def snooze(self, follow_up_id: int, days: int = 2) -> None:
        until = datetime.now(timezone.utc) + timedelta(days=days)
        self.repo.snooze(follow_up_id, until)

    def get_stats(self) -> dict:
        all_fus = (
            self.session.query(FollowUp)
            .filter_by(account_email=self.account_email)
            .all()
        )
        return {
            "total_tracked": len(all_fus),
            "replied": sum(1 for f in all_fus if f.replied),
            "pending": sum(1 for f in all_fus if not f.replied and not f.dismissed),
            "dismissed": sum(1 for f in all_fus if f.dismissed),
            "due_now": len(self.get_due_follow_ups()),
        }
