"""Tests for MockAIEngine — verifies every AI code path without an Anthropic key."""

import pytest

from mailtrim.core.gmail_client import Message, MessageHeader
from mailtrim.core.mock_ai import MockAIEngine, _heuristic_parse, get_ai_engine


def _msg(
    id_: str, subject: str = "Hello", from_: str = "sender@example.com", unsub: str = ""
) -> Message:
    return Message(
        id=id_,
        thread_id="t1",
        label_ids=["INBOX", "UNREAD"],
        snippet="snippet text",
        headers=MessageHeader(
            subject=subject,
            from_=f"Sender <{from_}>",
            list_unsubscribe=unsub,
        ),
    )


# ── classify_emails ──────────────────────────────────────────────────────────


def test_classify_returns_one_per_message():
    ai = MockAIEngine()
    msgs = [_msg(f"id{i}") for i in range(5)]
    results = ai.classify_emails(msgs)
    assert len(results) == 5
    for r in results:
        assert r.category in {
            "action_required",
            "newsletter",
            "notification",
            "receipt",
            "conversation",
            "social",
            "spam",
            "other",
        }
        assert r.priority in {"high", "medium", "low"}
        assert r.suggested_action in {
            "reply",
            "archive",
            "unsubscribe",
            "delete",
            "keep",
            "delegate",
        }


def test_classify_newsletter_via_header():
    ai = MockAIEngine()
    msg = _msg("abc", subject="Weekly digest", unsub="<mailto:unsub@example.com>")
    result = ai.classify_emails([msg])[0]
    assert result.category == "newsletter"
    assert result.priority == "low"
    assert result.suggested_action == "unsubscribe"


def test_classify_urgent_bumped_to_high():
    ai = MockAIEngine()
    # Pick an id that would normally be low priority but has an urgent subject
    msg = _msg("bump_me", subject="URGENT: action required immediately")
    result = ai.classify_emails([msg])[0]
    assert result.priority == "high"
    assert result.requires_reply is True


def test_classify_deterministic():
    """Same message ID → same category every time."""
    ai = MockAIEngine()
    msg = _msg("fixed_id_xyz")
    r1 = ai.classify_emails([msg])[0]
    r2 = ai.classify_emails([msg])[0]
    assert r1.category == r2.category
    assert r1.priority == r2.priority


# ── translate_rule ───────────────────────────────────────────────────────────


def test_translate_rule_archive():
    ai = MockAIEngine()
    rule = ai.translate_rule("archive all LinkedIn notifications older than 7 days")
    assert rule.action == "archive"
    assert "7" in rule.gmail_query or "older_than" in rule.gmail_query
    assert rule.gmail_query  # non-empty


def test_translate_rule_trash():
    ai = MockAIEngine()
    rule = ai.translate_rule("delete everything from spam@badsite.com")
    assert rule.action == "trash"
    assert "spam@badsite.com" in rule.gmail_query


def test_translate_rule_label():
    ai = MockAIEngine()
    rule = ai.translate_rule("label all receipts as receipts")
    assert rule.action == "label"


def test_translate_rule_has_warnings():
    ai = MockAIEngine()
    rule = ai.translate_rule("archive old stuff")
    assert isinstance(rule.warnings, list)
    assert len(rule.warnings) > 0


# ── parse_bulk_intent ────────────────────────────────────────────────────────


def test_parse_bulk_intent_promotions():
    ai = MockAIEngine()
    op = ai.parse_bulk_intent("archive all promotions older than 30 days")
    assert op.action == "archive"
    assert "30" in op.gmail_query or "promotion" in op.gmail_query
    assert 0.0 <= op.confidence <= 1.0


def test_parse_bulk_intent_trash():
    ai = MockAIEngine()
    op = ai.parse_bulk_intent("delete all emails from noreply@github.com")
    assert op.action == "trash"
    assert "noreply@github.com" in op.gmail_query


# ── generate_digest ──────────────────────────────────────────────────────────


def test_generate_digest_structure():
    ai = MockAIEngine()
    summary = ai.generate_digest(
        inbox_summary={"total_in_inbox": 120, "unread": 34},
        follow_ups=[{"to": "boss@work.com", "subject": "Proposal", "sent": "2026-03-30"}],
        avoided_count=5,
        top_senders=[{"sender": "news@sub.com", "count": 42}],
    )
    assert "120" in summary
    assert "34" in summary
    assert "Follow-up" in summary or "follow-up" in summary


def test_generate_digest_empty_inbox():
    ai = MockAIEngine()
    summary = ai.generate_digest(
        inbox_summary={"total_in_inbox": 0, "unread": 0},
        follow_ups=[],
        avoided_count=0,
        top_senders=[],
    )
    assert isinstance(summary, str)
    assert len(summary) > 0


# ── analyze_avoided_email ────────────────────────────────────────────────────


def test_analyze_avoided_email():
    ai = MockAIEngine()
    msg = _msg("avoid1", subject="Re: invoice overdue")
    insight = ai.analyze_avoided_email(msg)
    assert isinstance(insight, str)
    assert len(insight) > 20


# ── heuristic_parse ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected_action",
    [
        ("delete old promotions", "trash"),
        ("label newsletters as reading", "label"),
        ("mark as read all notifications", "mark_read"),
        ("archive LinkedIn emails older than 14 days", "archive"),
        ("unsubscribe from all newsletters", "unsubscribe"),
    ],
)
def test_heuristic_parse_actions(text, expected_action):
    _, action, _ = _heuristic_parse(text)
    assert action == expected_action


def test_heuristic_parse_age():
    query, _, _ = _heuristic_parse("archive emails older than 3 months")
    assert "older_than:90d" in query


def test_heuristic_parse_from():
    query, _, _ = _heuristic_parse("delete emails from spam@example.com")
    assert "spam@example.com" in query


# ── get_ai_engine factory ─────────────────────────────────────────────────────


def test_get_ai_engine_returns_mock_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    engine = get_ai_engine()
    assert isinstance(engine, MockAIEngine)


def test_get_ai_engine_returns_real_with_key(monkeypatch):
    from mailtrim.core.ai_engine import AIEngine

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-key-for-testing")
    engine = get_ai_engine()
    assert isinstance(engine, AIEngine)
