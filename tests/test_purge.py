"""Tests for the purge command helpers."""

from datetime import datetime, timezone

import pytest

# ── Selection parser ─────────────────────────────────────────────────────────


def _parse(raw: str, max_index: int):
    from mailtrim.cli.main import _parse_selection

    return _parse_selection(raw, max_index)


def test_parse_single():
    assert _parse("3", 10) == [2]


def test_parse_comma_list():
    assert _parse("1, 3, 5", 10) == [0, 2, 4]


def test_parse_range():
    assert _parse("2-5", 10) == [1, 2, 3, 4]


def test_parse_mixed():
    assert _parse("1,3-5,8", 10) == [0, 2, 3, 4, 7]


def test_parse_all():
    assert _parse("all", 5) == [0, 1, 2, 3, 4]


def test_parse_out_of_bounds_ignored():
    # 15 is beyond max=10, should be ignored
    assert _parse("1,15", 10) == [0]


def test_parse_empty_returns_empty():
    assert _parse("", 10) == []


def test_parse_invalid_ignored():
    assert _parse("abc,2", 10) == [1]


# ── SenderGroup accumulation ─────────────────────────────────────────────────


def test_sender_group_size_properties():
    from mailtrim.core.sender_stats import SenderGroup

    group = SenderGroup(
        sender_email="news@example.com",
        sender_name="Example News",
        count=42,
        total_size_bytes=5 * 1024 * 1024,  # 5 MB
        earliest_date=datetime(2022, 1, 1, tzinfo=timezone.utc),
        latest_date=datetime.now(timezone.utc),
        sample_subjects=["Subject A", "Subject B"],
        message_ids=["id1", "id2"],
        has_unsubscribe=True,
    )
    assert group.total_size_mb == 5.0
    assert group.display_name == "Example News"


def test_sender_group_display_name_falls_back_to_email():
    from mailtrim.core.sender_stats import SenderGroup

    group = SenderGroup(
        sender_email="news@example.com",
        sender_name="",
        count=1,
        total_size_bytes=0,
        earliest_date=datetime.now(timezone.utc),
        latest_date=datetime.now(timezone.utc),
        sample_subjects=[],
        message_ids=[],
        has_unsubscribe=False,
    )
    assert group.display_name == "news@example.com"


def test_accumulator_groups_correctly():
    from mailtrim.core.gmail_client import Message, MessageHeader
    from mailtrim.core.sender_stats import _Accumulator

    acc = _Accumulator("news@example.com", "Example News")

    def make_msg(id_: str, subject: str, ts: int, size: int, unsub: str = "") -> Message:
        return Message(
            id=id_,
            thread_id="t1",
            label_ids=["INBOX"],
            snippet="",
            headers=MessageHeader(
                subject=subject,
                from_="Example News <news@example.com>",
                list_unsubscribe=unsub,
            ),
            size_estimate=size,
            internal_date=ts,
        )

    acc.add(make_msg("1", "Weekly digest", 1_000_000, 1024))
    acc.add(make_msg("2", "Special offer", 2_000_000, 2048, unsub="<mailto:unsub@example.com>"))
    acc.add(make_msg("3", "Monthly newsletter", 1_500_000, 512))

    group = acc.to_group()
    assert group.count == 3
    assert group.total_size_bytes == 1024 + 2048 + 512
    assert group.has_unsubscribe is True
    # earliest_date should correspond to ts=1_000_000 ms (the smallest ts)
    assert group.earliest_date.timestamp() == pytest.approx(1_000_000 / 1000, rel=1e-3)
    # latest_date should correspond to ts=2_000_000 ms (the largest ts)
    assert group.latest_date.timestamp() == pytest.approx(2_000_000 / 1000, rel=1e-3)
    assert len(group.sample_subjects) == 3
    assert group.message_ids == ["1", "2", "3"]


def test_sort_by_oldest():
    """Groups sorted by oldest should put the one with the earliest first-email first."""
    from datetime import timedelta

    from mailtrim.core.sender_stats import SenderGroup

    now = datetime.now(timezone.utc)

    def make_group(email, days_ago):
        return SenderGroup(
            sender_email=email,
            sender_name=email,
            count=5,
            total_size_bytes=1024,
            earliest_date=now - timedelta(days=days_ago),
            latest_date=now,
            sample_subjects=[],
            message_ids=[],
            has_unsubscribe=False,
        )

    groups = [
        make_group("new@x.com", 10),
        make_group("old@x.com", 500),
        make_group("mid@x.com", 100),
    ]
    groups.sort(key=lambda g: g.earliest_date)  # same logic as sort_by="oldest"
    assert groups[0].sender_email == "old@x.com"
    assert groups[1].sender_email == "mid@x.com"
    assert groups[2].sender_email == "new@x.com"


def test_inbox_days():
    from datetime import timedelta

    from mailtrim.core.sender_stats import SenderGroup

    now = datetime.now(timezone.utc)
    group = SenderGroup(
        sender_email="x@x.com",
        sender_name="X",
        count=1,
        total_size_bytes=0,
        earliest_date=now - timedelta(days=365),
        latest_date=now,
        sample_subjects=[],
        message_ids=[],
        has_unsubscribe=False,
    )
    assert group.inbox_days == 365


# ── format_age ───────────────────────────────────────────────────────────────


def test_format_age_today():
    from mailtrim.core.sender_stats import format_age

    assert format_age(0) == "today"


def test_format_age_days():
    from mailtrim.core.sender_stats import format_age

    assert format_age(15) == "15d ago"


def test_format_age_months():
    from mailtrim.core.sender_stats import format_age

    assert format_age(60) == "2mo ago"


def test_format_age_years_only():
    from mailtrim.core.sender_stats import format_age

    assert format_age(365) == "1y ago"


def test_format_age_years_and_months():
    from mailtrim.core.sender_stats import format_age

    assert format_age(400) == "1y 1mo ago"


# ── compute_impact_scores ────────────────────────────────────────────────────


def _make_group(email: str, count: int, size_bytes: int, days_ago: int = 30):
    from datetime import timedelta

    from mailtrim.core.sender_stats import SenderGroup

    now = datetime.now(timezone.utc)
    return SenderGroup(
        sender_email=email,
        sender_name=email,
        count=count,
        total_size_bytes=size_bytes,
        earliest_date=now - timedelta(days=days_ago),
        latest_date=now,
        sample_subjects=[],
        message_ids=[],
        has_unsubscribe=False,
    )


def test_impact_score_largest_gets_100():
    from mailtrim.core.sender_stats import compute_impact_scores

    groups = [
        _make_group("big@x.com", 100, 10 * 1024 * 1024),
        _make_group("small@x.com", 10, 1024),
    ]
    compute_impact_scores(groups)
    assert groups[0].impact_score == 100


def test_impact_score_range():
    from mailtrim.core.sender_stats import compute_impact_scores

    groups = [_make_group(f"{i}@x.com", i * 10, i * 1024 * 1024) for i in range(1, 6)]
    compute_impact_scores(groups)
    for g in groups:
        assert 0 <= g.impact_score <= 100


def test_impact_score_empty_list_no_crash():
    from mailtrim.core.sender_stats import compute_impact_scores

    compute_impact_scores([])  # must not raise


# ── group_by_domain ──────────────────────────────────────────────────────────


def test_group_by_domain_merges_same_domain():
    from mailtrim.core.sender_stats import group_by_domain

    groups = [
        _make_group("jobs@linkedin.com", 50, 5 * 1024 * 1024),
        _make_group("notif@linkedin.com", 30, 3 * 1024 * 1024),
        _make_group("news@github.com", 20, 2 * 1024 * 1024),
    ]
    domains = group_by_domain(groups)
    domain_names = [d.domain for d in domains]
    assert "linkedin.com" in domain_names
    assert "github.com" in domain_names

    li = next(d for d in domains if d.domain == "linkedin.com")
    assert li.count == 80
    assert len(li.senders) == 2


def test_group_by_domain_single_sender():
    from mailtrim.core.sender_stats import group_by_domain

    groups = [_make_group("a@foo.com", 5, 1024)]
    domains = group_by_domain(groups)
    assert domains[0].domain == "foo.com"
    assert len(domains[0].senders) == 1


def test_domain_group_has_unsubscribe_any():
    from mailtrim.core.sender_stats import SenderGroup, group_by_domain

    now = datetime.now(timezone.utc)

    def _sg(email, unsub):
        return SenderGroup(
            sender_email=email,
            sender_name=email,
            count=1,
            total_size_bytes=0,
            earliest_date=now,
            latest_date=now,
            sample_subjects=[],
            message_ids=[],
            has_unsubscribe=unsub,
        )

    groups = [_sg("a@foo.com", False), _sg("b@foo.com", True)]
    domains = group_by_domain(groups)
    assert domains[0].has_unsubscribe is True


# ── generate_insights ────────────────────────────────────────────────────────


def test_generate_insights_top_storage():
    from mailtrim.core.sender_stats import compute_impact_scores, generate_insights, group_by_domain

    groups = [
        _make_group("big@x.com", 10, 50 * 1024 * 1024),
        _make_group("small@x.com", 200, 1 * 1024 * 1024),
    ]
    compute_impact_scores(groups)
    domain_groups = group_by_domain(groups)
    insights = generate_insights(groups, domain_groups)

    assert insights.top_storage.sender_email == "big@x.com"
    assert insights.top_volume.sender_email == "small@x.com"


def test_generate_insights_empty():
    from mailtrim.core.sender_stats import generate_insights

    insights = generate_insights([], [])
    assert insights.total_scanned == 0
    assert insights.top_storage is None


def test_generate_insights_coverage():
    from mailtrim.core.sender_stats import compute_impact_scores, generate_insights, group_by_domain

    groups = [_make_group(f"{i}@x.com", 10, 1024 * 1024) for i in range(10)]
    compute_impact_scores(groups)
    domain_groups = group_by_domain(groups)
    insights = generate_insights(groups, domain_groups, top_n=5)

    assert insights.top_n_coverage_pct == 50.0


# ── generate_recommendations ─────────────────────────────────────────────────


def test_generate_recommendations_returns_at_most_top_n():
    from mailtrim.core.sender_stats import compute_impact_scores, generate_recommendations

    groups = [_make_group(f"{i}@x.com", 100, 5 * 1024 * 1024, days_ago=200) for i in range(10)]
    compute_impact_scores(groups)
    recs = generate_recommendations(groups, top_n=3)
    assert len(recs) == 3


def test_generate_recommendations_has_commands():
    from mailtrim.core.sender_stats import compute_impact_scores, generate_recommendations

    groups = [_make_group("news@example.com", 100, 10 * 1024 * 1024, days_ago=200)]
    compute_impact_scores(groups)
    recs = generate_recommendations(groups, top_n=1)
    assert len(recs) == 1
    for action in recs[0].actions:
        assert "mailtrim" in action.command
        assert "example.com" in action.command


def test_generate_recommendations_max_2_actions_per_sender():
    from mailtrim.core.sender_stats import compute_impact_scores, generate_recommendations

    groups = [_make_group("x@example.com", 200, 20 * 1024 * 1024, days_ago=300)]
    compute_impact_scores(groups)
    recs = generate_recommendations(groups, top_n=1)
    assert len(recs[0].actions) <= 2


# ── compute_confidence_score ──────────────────────────────────────────────────


def test_confidence_high_all_signals():
    """Old, high-frequency sender with unsubscribe link → maximum confidence."""
    from datetime import timedelta

    from mailtrim.core.sender_stats import SenderGroup, compute_confidence_score

    now = datetime.now(timezone.utc)
    g = SenderGroup(
        sender_email="promo@spam.com",
        sender_name="Spam Inc",
        count=100,
        total_size_bytes=5 * 1024 * 1024,
        earliest_date=now - timedelta(days=365),
        latest_date=now,
        sample_subjects=[],
        message_ids=[],
        has_unsubscribe=True,
    )
    score = compute_confidence_score(g)
    assert score == 100


def test_confidence_low_no_signals():
    """Recent, rare sender with no unsubscribe link → near-zero confidence."""
    from mailtrim.core.sender_stats import SenderGroup, compute_confidence_score

    now = datetime.now(timezone.utc)
    # count=1, age=0d, no unsub → only freq component = (1/50)*35 ≈ 1 pt
    g = SenderGroup(
        sender_email="alice@work.com",
        sender_name="Alice",
        count=1,
        total_size_bytes=1024,
        earliest_date=now,
        latest_date=now,
        sample_subjects=[],
        message_ids=[],
        has_unsubscribe=False,
    )
    score = compute_confidence_score(g)
    assert score <= 2  # almost zero — only a tiny frequency rounding contribution


def test_confidence_unsubscribe_adds_30():
    """has_unsubscribe with near-zero age/count should score ~30–31 pts."""
    from mailtrim.core.sender_stats import SenderGroup, compute_confidence_score

    now = datetime.now(timezone.utc)
    # unsub=30, age≈0, count=1 → freq=(1/50)*35≈0.7 → total ≈ 30–31
    g = SenderGroup(
        sender_email="x@y.com",
        sender_name="X",
        count=1,
        total_size_bytes=0,
        earliest_date=now,
        latest_date=now,
        sample_subjects=[],
        message_ids=[],
        has_unsubscribe=True,
    )
    score = compute_confidence_score(g)
    assert 30 <= score <= 32


def test_confidence_range():
    """Score must always be 0–100."""
    from datetime import timedelta

    from mailtrim.core.sender_stats import compute_confidence_score

    now = datetime.now(timezone.utc)
    for has_unsub in (True, False):
        for days in (0, 30, 180, 500):
            for count in (1, 10, 50, 200):
                from mailtrim.core.sender_stats import SenderGroup

                g = SenderGroup(
                    sender_email="t@t.com",
                    sender_name="T",
                    count=count,
                    total_size_bytes=0,
                    earliest_date=now - timedelta(days=days),
                    latest_date=now,
                    sample_subjects=[],
                    message_ids=[],
                    has_unsubscribe=has_unsub,
                )
                s = compute_confidence_score(g)
                assert 0 <= s <= 100, (
                    f"score {s} out of range (days={days}, count={count}, unsub={has_unsub})"
                )


# ── confidence_safety_label ───────────────────────────────────────────────────


def test_confidence_safety_label_high():
    from mailtrim.core.sender_stats import confidence_safety_label

    assert confidence_safety_label(70) == "Safe to clean"
    assert confidence_safety_label(100) == "Safe to clean"


def test_confidence_safety_label_medium():
    from mailtrim.core.sender_stats import confidence_safety_label

    assert confidence_safety_label(40) == "Low risk"
    assert confidence_safety_label(69) == "Low risk"


def test_confidence_safety_label_low():
    from mailtrim.core.sender_stats import confidence_safety_label

    assert confidence_safety_label(0) == "Review first"
    assert confidence_safety_label(39) == "Review first"


# ── impact_label ─────────────────────────────────────────────────────────────


def test_impact_label_high():
    from mailtrim.core.sender_stats import impact_label

    assert impact_label(75) == "High"
    assert impact_label(100) == "High"


def test_impact_label_medium():
    from mailtrim.core.sender_stats import impact_label

    assert impact_label(40) == "Medium"
    assert impact_label(74) == "Medium"


def test_impact_label_low():
    from mailtrim.core.sender_stats import impact_label

    assert impact_label(0) == "Low"
    assert impact_label(39) == "Low"


# ── reclaimable_mb ────────────────────────────────────────────────────────────


def test_reclaimable_mb_sums_first_actions():
    from mailtrim.core.sender_stats import (
        compute_impact_scores,
        generate_recommendations,
        reclaimable_mb,
    )

    groups = [_make_group(f"{i}@x.com", 100, 10 * 1024 * 1024, days_ago=200) for i in range(3)]
    compute_impact_scores(groups)
    recs = generate_recommendations(groups, top_n=3)
    total = reclaimable_mb(recs)
    # Each group has 10 MB; "delete all" is action 1 for large senders
    assert total > 0
    assert isinstance(total, float)


def test_reclaimable_mb_empty():
    from mailtrim.core.sender_stats import reclaimable_mb

    assert reclaimable_mb([]) == 0.0


# ── quick_win ─────────────────────────────────────────────────────────────────


def test_quick_win_returns_none_for_empty():
    from mailtrim.core.sender_stats import quick_win

    assert quick_win([]) is None


def test_quick_win_picks_highest_composite():
    """
    Of two recs, the one with higher confidence should win even if it has
    lower impact score (confidence is weighted 60%).
    """
    from datetime import timedelta

    from mailtrim.core.sender_stats import (
        Action,
        Recommendation,
        SenderGroup,
        compute_impact_scores,
        quick_win,
    )

    now = datetime.now(timezone.utc)

    def _sg(email, count, size_bytes, days, unsub):
        return SenderGroup(
            sender_email=email,
            sender_name=email,
            count=count,
            total_size_bytes=size_bytes,
            earliest_date=now - timedelta(days=days),
            latest_date=now,
            sample_subjects=[],
            message_ids=[],
            has_unsubscribe=unsub,
        )

    high_conf_sender = _sg("news@promo.com", 80, 2 * 1024 * 1024, days=300, unsub=True)
    low_conf_sender = _sg("friend@personal.com", 200, 50 * 1024 * 1024, days=5, unsub=False)

    compute_impact_scores([high_conf_sender, low_conf_sender])

    action = Action(label="Delete all", savings_mb=10, savings_exact=True, command="cmd")
    recs = [
        Recommendation(sender=high_conf_sender, actions=[action], confidence=90),
        Recommendation(sender=low_conf_sender, actions=[action], confidence=10),
    ]

    winner = quick_win(recs)
    assert winner.sender.sender_email == "news@promo.com"


def test_recommendation_has_confidence_field():
    """generate_recommendations must populate the confidence field."""
    from mailtrim.core.sender_stats import compute_impact_scores, generate_recommendations

    groups = [_make_group("x@example.com", 60, 8 * 1024 * 1024, days_ago=200)]
    compute_impact_scores(groups)
    recs = generate_recommendations(groups, top_n=1)
    assert 0 <= recs[0].confidence <= 100


def test_recommendation_commands_use_structured_flags():
    """Commands must use --domain flag, not NL bulk strings."""
    from mailtrim.core.sender_stats import compute_impact_scores, generate_recommendations

    groups = [_make_group("news@example.com", 100, 10 * 1024 * 1024, days_ago=200)]
    compute_impact_scores(groups)
    recs = generate_recommendations(groups, top_n=1)
    for action in recs[0].actions:
        assert "bulk" not in action.command, (
            f"Expected structured command, got NL: {action.command}"
        )
        assert "--domain" in action.command or "purge" in action.command


# ── confidence_reason ─────────────────────────────────────────────────────────


def test_confidence_reason_all_signals():
    from datetime import timedelta

    from mailtrim.core.sender_stats import SenderGroup, confidence_reason

    now = datetime.now(timezone.utc)
    g = SenderGroup(
        sender_email="news@promo.com",
        sender_name="Promo",
        count=50,
        total_size_bytes=0,
        earliest_date=now - timedelta(days=180),
        latest_date=now,
        sample_subjects=[],
        message_ids=[],
        has_unsubscribe=True,
    )
    reason = confidence_reason(g)
    assert "unsubscribe detected" in reason
    assert "old emails" in reason
    assert "high frequency" in reason


def test_confidence_reason_no_signals():
    from mailtrim.core.sender_stats import SenderGroup, confidence_reason

    now = datetime.now(timezone.utc)
    g = SenderGroup(
        sender_email="x@y.com",
        sender_name="X",
        count=1,
        total_size_bytes=0,
        earliest_date=now,
        latest_date=now,
        sample_subjects=[],
        message_ids=[],
        has_unsubscribe=False,
    )
    assert confidence_reason(g) == "limited signals"


def test_confidence_reason_partial():
    """Only unsubscribe signal present → only that part in reason."""
    from mailtrim.core.sender_stats import SenderGroup, confidence_reason

    now = datetime.now(timezone.utc)
    g = SenderGroup(
        sender_email="x@y.com",
        sender_name="X",
        count=1,
        total_size_bytes=0,
        earliest_date=now,
        latest_date=now,
        sample_subjects=[],
        message_ids=[],
        has_unsubscribe=True,
    )
    reason = confidence_reason(g)
    assert "unsubscribe detected" in reason
    assert "old emails" not in reason
    assert "high frequency" not in reason


# ── estimate_cleanup_seconds + format_time_estimate ──────────────────────────


def test_estimate_cleanup_seconds_small():
    from mailtrim.core.sender_stats import estimate_cleanup_seconds

    lo, hi = estimate_cleanup_seconds(10)
    assert lo >= 3
    assert hi >= lo


def test_estimate_cleanup_seconds_large():
    from mailtrim.core.sender_stats import estimate_cleanup_seconds

    lo, hi = estimate_cleanup_seconds(1000)
    assert lo == 5  # 1000 // 200
    assert hi == 10  # 1000 // 100


def test_estimate_cleanup_seconds_floor():
    """Very small counts always return at least (3, 5)."""
    from mailtrim.core.sender_stats import estimate_cleanup_seconds

    lo, hi = estimate_cleanup_seconds(0)
    assert lo == 3
    assert hi == 5


def test_format_time_estimate_range():
    from mailtrim.core.sender_stats import format_time_estimate

    result = format_time_estimate(1000)
    assert "~5" in result
    assert "10" in result


def test_format_time_estimate_starts_with_tilde():
    """Output always starts with '~' to signal it's an estimate."""
    from mailtrim.core.sender_stats import format_time_estimate

    result = format_time_estimate(0)
    assert result.startswith("~")


# ── reclaimable_pct ───────────────────────────────────────────────────────────


def test_reclaimable_pct_normal():
    from mailtrim.core.sender_stats import reclaimable_pct

    assert reclaimable_pct(87.4, 287.4) == pytest.approx(30.4, abs=0.2)


def test_reclaimable_pct_zero_total():
    from mailtrim.core.sender_stats import reclaimable_pct

    assert reclaimable_pct(10.0, 0.0) == 0.0


def test_reclaimable_pct_full():
    from mailtrim.core.sender_stats import reclaimable_pct

    assert reclaimable_pct(50.0, 50.0) == 100.0


def test_reclaimable_pct_zero_reclaimable():
    from mailtrim.core.sender_stats import reclaimable_pct

    assert reclaimable_pct(0.0, 100.0) == 0.0


# ── generate_share_text ───────────────────────────────────────────────────────


def test_generate_share_text_without_elapsed():
    from mailtrim.core.sender_stats import generate_share_text

    text = generate_share_text(freed_mb=87.4, sender_count=3, email_count=495)
    assert "87.4 MB" in text
    assert "3 senders" in text
    assert "495" in text
    assert "mailtrim" in text
    assert "🎉" in text
    # No timing when elapsed_seconds=None
    assert " in " not in text


def test_generate_share_text_with_elapsed():
    from mailtrim.core.sender_stats import generate_share_text

    text = generate_share_text(freed_mb=44.0, sender_count=1, email_count=312, elapsed_seconds=8)
    assert "in 8s" in text
    assert "44.0 MB" in text


def test_generate_share_text_formats_email_count():
    """Large email counts should be formatted with commas."""
    from mailtrim.core.sender_stats import generate_share_text

    text = generate_share_text(freed_mb=200.0, sender_count=5, email_count=12500)
    assert "12,500" in text


# ── risk_tier_icon ────────────────────────────────────────────────────────────


def test_risk_tier_icon_green():
    from mailtrim.core.sender_stats import risk_tier_icon

    assert risk_tier_icon(70) == "🟢"
    assert risk_tier_icon(100) == "🟢"


def test_risk_tier_icon_yellow():
    from mailtrim.core.sender_stats import risk_tier_icon

    assert risk_tier_icon(40) == "🟡"
    assert risk_tier_icon(69) == "🟡"


def test_risk_tier_icon_red():
    from mailtrim.core.sender_stats import risk_tier_icon

    assert risk_tier_icon(0) == "🔴"
    assert risk_tier_icon(39) == "🔴"


def test_risk_tier_icon_boundaries():
    from mailtrim.core.sender_stats import risk_tier_icon

    # Exact boundary values
    assert risk_tier_icon(70) == "🟢"  # first green
    assert risk_tier_icon(40) == "🟡"  # first yellow
    assert risk_tier_icon(39) == "🔴"  # last red


# ── generate_headline_insight ─────────────────────────────────────────────────


def _make_insights(
    total_scanned: int = 500,
    total_size_bytes: int = 100 * 1024 * 1024,
    unique_senders: int = 20,
    unique_domains: int = 15,
    oldest_email_days: int = 100,
):
    """Build a minimal InboxInsights for headline testing."""
    from mailtrim.core.sender_stats import InboxInsights

    return InboxInsights(
        top_storage=None,
        top_volume=None,
        oldest=None,
        multi_sender_domains=[],
        top_n_coverage_pct=40.0,
        top_n_size_mb=50.0,
        total_scanned=total_scanned,
        total_size_bytes=total_size_bytes,
        unique_senders=unique_senders,
        unique_domains=unique_domains,
        oldest_email_days=oldest_email_days,
    )


def test_headline_high_clutter_percentage():
    """≥30% clutter → lead with percentage."""
    from mailtrim.core.sender_stats import generate_headline_insight

    insights = _make_insights()
    headline = generate_headline_insight(
        insights, reclaim_pct=35.0, rec_count=3, reclaimable_mb_val=35.0
    )
    assert "35%" in headline
    assert "💥" in headline


def test_headline_large_absolute_size():
    """≥50 MB but <30% → lead with MB."""
    from mailtrim.core.sender_stats import generate_headline_insight

    insights = _make_insights(total_size_bytes=1000 * 1024 * 1024)  # 1 GB scanned
    headline = generate_headline_insight(
        insights, reclaim_pct=6.0, rec_count=2, reclaimable_mb_val=60.0
    )
    assert "60.0 MB" in headline
    assert "🗄" in headline


def test_headline_old_inbox():
    """Old inbox (≥365d) triggers time-based headline when size is small."""
    from mailtrim.core.sender_stats import generate_headline_insight

    insights = _make_insights(total_size_bytes=50 * 1024 * 1024, oldest_email_days=730)
    headline = generate_headline_insight(
        insights, reclaim_pct=5.0, rec_count=1, reclaimable_mb_val=2.5
    )
    assert "⏳" in headline
    assert "2 year" in headline


def test_headline_clean_inbox():
    """Nothing to reclaim → clean inbox message."""
    from mailtrim.core.sender_stats import generate_headline_insight

    insights = _make_insights()
    headline = generate_headline_insight(
        insights, reclaim_pct=0.0, rec_count=0, reclaimable_mb_val=0.0
    )
    assert "✅" in headline


def test_headline_empty_scan():
    """No emails scanned → explicit empty message."""
    from mailtrim.core.sender_stats import generate_headline_insight

    insights = _make_insights(total_scanned=0)
    headline = generate_headline_insight(
        insights, reclaim_pct=0.0, rec_count=0, reclaimable_mb_val=0.0
    )
    assert "📭" in headline


def test_headline_singular_sender():
    """Single sender uses 'sender' not 'senders'."""
    from mailtrim.core.sender_stats import generate_headline_insight

    insights = _make_insights()
    headline = generate_headline_insight(
        insights, reclaim_pct=40.0, rec_count=1, reclaimable_mb_val=40.0
    )
    # Should say "sender" not "senders" — no trailing 's'
    assert "1 sender" in headline
    assert "senders" not in headline


# ── generate_viral_share_text ─────────────────────────────────────────────────


def test_viral_share_includes_key_facts():
    from mailtrim.core.sender_stats import generate_viral_share_text

    text = generate_viral_share_text(
        freed_mb=87.4, sender_count=3, email_count=495, reclaim_pct=30.0
    )
    assert "87.4 MB" in text
    assert "3 sender" in text
    assert "495" in text
    assert "mailtrim" in text
    assert "🤯" in text


def test_viral_share_with_elapsed_time():
    from mailtrim.core.sender_stats import generate_viral_share_text

    text = generate_viral_share_text(
        freed_mb=44.0, sender_count=1, email_count=312, reclaim_pct=15.0, elapsed_seconds=8
    )
    assert "in 8s" in text


def test_viral_share_without_elapsed():
    from mailtrim.core.sender_stats import generate_viral_share_text

    text = generate_viral_share_text(
        freed_mb=44.0, sender_count=1, email_count=312, reclaim_pct=10.0
    )
    assert " in " not in text  # no time part when elapsed_seconds=None


def test_viral_share_pct_line_above_threshold():
    """reclaim_pct ≥ 5 should include the clutter percentage line."""
    from mailtrim.core.sender_stats import generate_viral_share_text

    text = generate_viral_share_text(
        freed_mb=50.0, sender_count=2, email_count=200, reclaim_pct=25.0
    )
    assert "25%" in text


def test_viral_share_pct_line_below_threshold():
    """reclaim_pct < 5 should omit the clutter percentage line."""
    from mailtrim.core.sender_stats import generate_viral_share_text

    text = generate_viral_share_text(freed_mb=1.0, sender_count=1, email_count=5, reclaim_pct=2.0)
    assert "clutter" not in text


def test_viral_share_includes_repo_url():
    from mailtrim.core.sender_stats import generate_viral_share_text

    text = generate_viral_share_text(
        freed_mb=10.0,
        sender_count=1,
        email_count=50,
        reclaim_pct=5.0,
        repo_url="https://example.com/repo",
    )
    assert "https://example.com/repo" in text


def test_viral_share_singular_sender():
    from mailtrim.core.sender_stats import generate_viral_share_text

    text = generate_viral_share_text(freed_mb=10.0, sender_count=1, email_count=50, reclaim_pct=5.0)
    # Should not say "1 senders"
    assert "1 sender\n" in text or "1 sender\r" in text or "1 senders" not in text


def test_viral_share_plural_senders():
    from mailtrim.core.sender_stats import generate_viral_share_text

    text = generate_viral_share_text(
        freed_mb=50.0, sender_count=3, email_count=300, reclaim_pct=20.0
    )
    assert "3 senders" in text


# ── generate_share_text pluralization ─────────────────────────────────────────


def test_generate_share_text_singular_sender():
    """Single sender uses 'sender' not 'senders'."""
    from mailtrim.core.sender_stats import generate_share_text

    text = generate_share_text(freed_mb=10.0, sender_count=1, email_count=42)
    assert "1 sender" in text
    assert "1 senders" not in text


def test_generate_share_text_plural_senders():
    """Multiple senders use 'senders'."""
    from mailtrim.core.sender_stats import generate_share_text

    text = generate_share_text(freed_mb=87.4, sender_count=5, email_count=800)
    assert "5 senders" in text


# ── estimate_reading_minutes ──────────────────────────────────────────────────


def test_estimate_reading_minutes_zero_emails():
    from mailtrim.core.sender_stats import estimate_reading_minutes

    assert estimate_reading_minutes(0) == 0


def test_estimate_reading_minutes_small_count():
    """5 emails * 5s = 25s → rounds to 0 minutes (below threshold)."""
    from mailtrim.core.sender_stats import estimate_reading_minutes

    assert estimate_reading_minutes(5) == 0


def test_estimate_reading_minutes_typical():
    """495 emails * 5s = 2475s → 41 minutes."""
    from mailtrim.core.sender_stats import estimate_reading_minutes

    assert estimate_reading_minutes(495) == 41


def test_estimate_reading_minutes_always_non_negative():
    from mailtrim.core.sender_stats import estimate_reading_minutes

    for count in (0, 1, 10, 100, 1000):
        assert estimate_reading_minutes(count) >= 0


# ── viral share reading time line ─────────────────────────────────────────────


def test_viral_share_reading_time_shown_for_large_count():
    """Large email counts should include a reading time line."""
    from mailtrim.core.sender_stats import generate_viral_share_text

    text = generate_viral_share_text(
        freed_mb=87.4, sender_count=3, email_count=495, reclaim_pct=30.0
    )
    assert "min of reading time reclaimed" in text


def test_viral_share_reading_time_hidden_for_tiny_count():
    """Very small counts should not show a reading time line (would be 0 min)."""
    from mailtrim.core.sender_stats import generate_viral_share_text

    text = generate_viral_share_text(freed_mb=0.5, sender_count=1, email_count=5, reclaim_pct=1.0)
    assert "reading time" not in text


def test_viral_share_hook_contains_email_count():
    """Email count should appear in the hook line (first line) for immediate impact."""
    from mailtrim.core.sender_stats import generate_viral_share_text

    text = generate_viral_share_text(
        freed_mb=50.0, sender_count=2, email_count=300, reclaim_pct=20.0
    )
    first_line = text.split("\n")[0]
    assert "300" in first_line
