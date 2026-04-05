"""
MockAIEngine — drop-in replacement for AIEngine when ANTHROPIC_API_KEY is absent.

Produces realistic-looking fake responses so every command can be tested
end-to-end without spending any API credits.

Activated automatically when ANTHROPIC_API_KEY is not set,
or explicitly via:  MAILTRIM_MOCK_AI=true
"""

from __future__ import annotations

import hashlib
import re

from mailtrim.core.ai_engine import (
    AIEngine,
    BulkOperation,
    ClassifiedEmail,
    NLRule,
)
from mailtrim.core.gmail_client import Message

# Deterministic bucketing so the same email always gets the same category
_CATEGORIES = [
    ("action_required", "high", "reply", True, "Sender is a person asking a direct question."),
    (
        "newsletter",
        "low",
        "unsubscribe",
        False,
        "Regular mass mailing with List-Unsubscribe header.",
    ),
    ("notification", "low", "archive", False, "Automated system alert — no reply needed."),
    ("receipt", "low", "archive", False, "Transaction confirmation — keep for records."),
    ("conversation", "medium", "keep", False, "Ongoing thread with a known contact."),
    ("social", "low", "archive", False, "Social network notification."),
    ("spam", "low", "delete", False, "Unsolicited bulk mail."),
]


def _bucket(gmail_id: str) -> int:
    """Deterministic 0–6 bucket from message ID."""
    h = int(hashlib.md5(gmail_id.encode(), usedforsecurity=False).hexdigest(), 16)
    return h % len(_CATEGORIES)


class MockAIEngine:
    """
    Mirrors the AIEngine public interface but never calls Anthropic.
    All outputs are deterministic based on the input data.
    """

    # ── Classification ───────────────────────────────────────────────────────

    def classify_emails(self, messages: list[Message]) -> list[ClassifiedEmail]:
        results = []
        for msg in messages:
            cat, pri, action, needs_reply, explanation = _CATEGORIES[_bucket(msg.id)]

            # Nudge priority up if subject contains urgent-sounding words
            subject_lower = msg.headers.subject.lower()
            if any(w in subject_lower for w in ("urgent", "action required", "important", "asap")):
                pri = "high"
                needs_reply = True

            # Detect newsletters by List-Unsubscribe header
            if msg.headers.list_unsubscribe:
                cat, pri, action, needs_reply = "newsletter", "low", "unsubscribe", False
                explanation = "[mock] Has List-Unsubscribe header — classified as newsletter."

            results.append(
                ClassifiedEmail(
                    gmail_id=msg.id,
                    category=cat,
                    priority=pri,
                    explanation=f"[mock] {explanation}",
                    suggested_action=action,
                    requires_reply=needs_reply,
                    deadline_hint="",
                )
            )
        return results

    # ── NL → rule ────────────────────────────────────────────────────────────

    def translate_rule(self, natural_language: str) -> NLRule:
        query, action, params = _heuristic_parse(natural_language)
        return NLRule(
            natural_language=natural_language,
            gmail_query=query,
            action=action,
            action_params=params,
            explanation=f'[mock] Auto-derived from: "{natural_language}"',
            warnings=["[mock] Review the Gmail query before enabling on production data."],
        )

    # ── Bulk intent ──────────────────────────────────────────────────────────

    def parse_bulk_intent(self, instruction: str) -> BulkOperation:
        query, action, params = _heuristic_parse(instruction)
        return BulkOperation(
            gmail_query=query,
            action=action,
            action_params=params,
            explanation=f"[mock] Would {action} emails matching: {query}",
            estimated_count_hint="unknown (mock mode)",
            confidence=0.7,
        )

    # ── Digest ───────────────────────────────────────────────────────────────

    def generate_digest(self, inbox_summary, follow_ups, avoided_count, top_senders) -> str:
        lines = [
            "[mock digest — no Anthropic key]",
            "",
            f"Inbox: {inbox_summary.get('total_in_inbox', '?')} messages, "
            f"{inbox_summary.get('unread', '?')} unread.",
        ]
        if follow_ups:
            lines.append(f"Follow-ups due: {len(follow_ups)} thread(s) awaiting reply.")
        if avoided_count:
            lines.append(f"Avoided: {avoided_count} email(s) you keep seeing but not acting on.")
        if top_senders:
            top = top_senders[0]
            lines.append(
                f"Top sender: {top['sender']} ({top['count']} emails). Consider unsubscribing."
            )
        return "\n".join(lines)

    # ── Avoidance insight ────────────────────────────────────────────────────

    def analyze_avoided_email(self, msg: Message) -> str:
        return (
            f'[mock] You may be avoiding "{msg.headers.subject[:60]}" '
            "because it requires a decision or uncomfortable reply. "
            "Suggested: draft a 2-sentence response now, or archive if no longer relevant."
        )


# ── Heuristic NL parser (no AI needed) ───────────────────────────────────────


def _heuristic_parse(text: str) -> tuple[str, str, dict]:
    """
    Very basic keyword extraction to produce a plausible Gmail query + action.
    Good enough for testing the full pipeline without Anthropic.
    """
    t = text.lower()

    # Action — check more specific phrases first
    if any(w in t for w in ("delete", "trash", "remove")):
        action = "trash"
    elif any(w in t for w in ("mark as read", "mark read")):
        action = "mark_read"
    elif any(w in t for w in ("label", "tag", "categorize")):
        action = "label"
    elif "unsubscribe" in t:
        action = "unsubscribe"
    else:
        action = "archive"

    # Label name (for label action)
    params: dict = {}
    label_match = re.search(r"(?:label|tag|categorize)[^\w]*['\"]?(\w[\w\s-]{1,30})['\"]?", t)
    if action == "label" and label_match:
        params["label_name"] = label_match.group(1).strip()

    # From: sender
    from_match = re.search(r"from[:\s]+([^\s,]+)", t)
    from_clause = f"from:{from_match.group(1)}" if from_match else ""

    # Age clause
    age_match = re.search(r"(\d+)\s*(day|week|month|year)s?", t)
    age_clause = ""
    if age_match:
        n, unit = age_match.group(1), age_match.group(2)
        multiplier = {"day": 1, "week": 7, "month": 30, "year": 365}
        days = int(n) * multiplier[unit]
        age_clause = f"older_than:{days}d"

    # Category / label hints
    category_clause = ""
    if "newsletter" in t or "newsletter" in t:
        category_clause = "label:newsletters"
    elif "promotion" in t:
        category_clause = "category:promotions"
    elif "social" in t:
        category_clause = "category:social"
    elif "notification" in t:
        category_clause = "label:notifications"

    parts = [p for p in [from_clause, category_clause, age_clause] if p]
    query = " ".join(parts) if parts else "in:inbox"

    return query, action, params


# ── Factory ───────────────────────────────────────────────────────────────────


def get_ai_engine() -> "AIEngine | MockAIEngine":
    """
    Return a real AIEngine if ANTHROPIC_API_KEY is set,
    otherwise fall back to MockAIEngine with a visible warning.

    Privacy note: when a real key is used, email subjects and snippets
    (never full body content) are sent to the Anthropic API for classification.
    See PRIVACY.md for full details.
    """
    import os

    from rich.console import Console

    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        Console().print(
            "[yellow]No ANTHROPIC_API_KEY — running in mock AI mode.[/yellow] "
            "[dim]Set ANTHROPIC_API_KEY for real classifications.[/dim]"
        )
        return MockAIEngine()

    Console().print(
        "[bold yellow][AI][/bold yellow] Real Anthropic API key detected — "
        "email subjects and snippets (≤300 chars, no full body) will be sent to Anthropic. "
        "[dim]See PRIVACY.md for full data flow.[/dim]"
    )
    return AIEngine(api_key=key)
