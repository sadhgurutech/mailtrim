"""
Sender aggregation, impact scoring, domain grouping, and recommendation engine.

Data flow:
  raw messages → SenderGroup (per address)
               → DomainGroup  (aggregated per domain)
               → impact scores + confidence scores applied
               → InboxInsights  (key callouts)
               → list[Recommendation] (actionable next steps with confidence + reason)
               → quick_win()  (single best starting point)
               → generate_share_text()  (copyable one-liner for social/team sharing)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from mailtrim.config import get_settings
from mailtrim.core.gmail_client import GmailClient, Message

SortKey = Literal["score", "count", "oldest", "size"]

# ── Transactional keyword detection ───────────────────────────────────────────

# These keywords in subject lines suggest the email is transactional (receipts,
# invoices, security alerts, etc.) — content the user likely needs to keep.
# When detected, confidence score is penalised to reduce false positives.
_TRANSACTIONAL_KEYWORDS: frozenset[str] = frozenset(
    {
        "receipt",
        "invoice",
        "order",
        "order confirmation",
        "confirmation",
        "tracking",
        "shipment",
        "delivery",
        "payment",
        "statement",
        "bill",
        "security alert",
        "verification",
        "password",
        "your account",
        "purchase",
        "subscription renewal",
    }
)

_TRANSACTIONAL_PENALTY = 25  # pts deducted when transactional keywords are found


# ── Age formatting ────────────────────────────────────────────────────────────


def format_age(days: int) -> str:
    """Convert a number of days into a human-friendly age string."""
    if days < 1:
        return "today"
    if days < 30:
        return f"{days}d ago"
    if days < 365:
        months = days // 30
        return f"{months}mo ago"
    years = days // 365
    months = (days % 365) // 30
    if months:
        return f"{years}y {months}mo ago"
    return f"{years}y ago"


# ── SenderGroup ───────────────────────────────────────────────────────────────


@dataclass
class SenderGroup:
    sender_email: str
    sender_name: str
    count: int
    total_size_bytes: int
    earliest_date: datetime
    latest_date: datetime
    sample_subjects: list[str]
    message_ids: list[str]
    has_unsubscribe: bool
    impact_score: int = 0  # 0–100; set by compute_impact_scores()

    @property
    def domain(self) -> str:
        """Extract the domain part of the sender address."""
        addr = self.sender_email
        return addr.split("@")[-1].lower() if "@" in addr else addr.lower()

    @property
    def total_size_mb(self) -> float:
        return round(self.total_size_bytes / (1024 * 1024), 2)

    @property
    def display_name(self) -> str:
        return self.sender_name if self.sender_name else self.sender_email

    @property
    def inbox_days(self) -> int:
        return (datetime.now(timezone.utc) - self.earliest_date).days

    @property
    def age_str(self) -> str:
        return format_age(self.inbox_days)


# ── DomainGroup ───────────────────────────────────────────────────────────────


@dataclass
class DomainGroup:
    domain: str
    senders: list[SenderGroup]  # all per-address groups under this domain
    impact_score: int = 0

    @property
    def count(self) -> int:
        return sum(s.count for s in self.senders)

    @property
    def total_size_bytes(self) -> int:
        return sum(s.total_size_bytes for s in self.senders)

    @property
    def total_size_mb(self) -> float:
        return round(self.total_size_bytes / (1024 * 1024), 2)

    @property
    def earliest_date(self) -> datetime:
        return min(s.earliest_date for s in self.senders)

    @property
    def inbox_days(self) -> int:
        return (datetime.now(timezone.utc) - self.earliest_date).days

    @property
    def age_str(self) -> str:
        return format_age(self.inbox_days)

    @property
    def has_unsubscribe(self) -> bool:
        return any(s.has_unsubscribe for s in self.senders)

    @property
    def display_name(self) -> str:
        """Use the most common sender name, or the domain if names are inconsistent."""
        names = [s.sender_name for s in self.senders if s.sender_name]
        if not names:
            return self.domain
        # Pick the most-frequent name
        return max(set(names), key=names.count)

    @property
    def message_ids(self) -> list[str]:
        return [mid for s in self.senders for mid in s.message_ids]

    @property
    def sample_subjects(self) -> list[str]:
        subjects: list[str] = []
        for s in self.senders:
            subjects.extend(s.sample_subjects)
        return subjects[:3]


# ── Impact scoring ────────────────────────────────────────────────────────────


def compute_impact_scores(groups: list[SenderGroup]) -> None:
    """
    Assign a 0–100 impact score to each SenderGroup **in place**.

    Formula: 60% weight on storage, 40% on count.
    Both are normalized against the highest value in the provided list,
    so scores are always relative to the current dataset — not absolute.

    Size is weighted higher because freed storage is the most tangible
    outcome for the user. Count matters for noise/clutter even without size.
    """
    if not groups:
        return

    max_size = max(g.total_size_bytes for g in groups) or 1
    max_count = max(g.count for g in groups) or 1

    for g in groups:
        size_component = (g.total_size_bytes / max_size) * 60
        count_component = (g.count / max_count) * 40
        g.impact_score = round(size_component + count_component)


def compute_domain_impact_scores(domains: list[DomainGroup]) -> None:
    """Same formula applied to DomainGroups (in place)."""
    if not domains:
        return

    max_size = max(d.total_size_bytes for d in domains) or 1
    max_count = max(d.count for d in domains) or 1

    for d in domains:
        size_component = (d.total_size_bytes / max_size) * 60
        count_component = (d.count / max_count) * 40
        d.impact_score = round(size_component + count_component)


def impact_label(score: int) -> str:
    """Convert a 0-100 impact score to a human-readable tier."""
    if score >= 75:
        return "High"
    if score >= 40:
        return "Medium"
    return "Low"


# ── Confidence scoring ────────────────────────────────────────────────────────


def compute_confidence_score(g: SenderGroup) -> int:
    """
    Estimate how safe it is to bulk-delete emails from this sender. Returns 0–100.

    Three evidence pillars (weights sum to 100):
    - Unsubscribe header present (30 pts): senders that include List-Unsubscribe
      self-identify as bulk/marketing mail — the clearest single signal.
    - Age (35 pts): emails sitting >180 days in the inbox are almost certainly
      no longer actionable. Normalized at 180d ceiling.
    - Frequency (35 pts): >50 emails from one sender in a typical inbox scan
      is an almost certain indicator of automated bulk mail. Normalized at 50.

    Higher score → safer to delete without reviewing individual messages.
    """
    unsub_score = 30 if g.has_unsubscribe else 0
    age_score = min(g.inbox_days / 180, 1.0) * 35
    freq_score = min(g.count / 50, 1.0) * 35
    raw = round(min(unsub_score + age_score + freq_score, 100))

    # Penalise if sample subjects contain transactional keywords.
    # Transactional mail (receipts, invoices, security alerts) is high-cost to
    # delete by mistake — lower the score to surface the 🔴 "review first" warning.
    subjects_lower = " ".join(g.sample_subjects).lower()
    if any(kw in subjects_lower for kw in _TRANSACTIONAL_KEYWORDS):
        raw = max(0, raw - _TRANSACTIONAL_PENALTY)

    return raw


def confidence_safety_label(score: int) -> str:
    """Map a confidence score to a user-facing safety description."""
    if score >= 70:
        return "Safe to clean"
    if score >= 40:
        return "Low risk"
    return "Review first"


def risk_tier_icon(confidence: int) -> str:
    """
    Traffic-light icon for the confidence score.

    🟢  ≥ 70  — confident this is bulk/marketing mail; safe to bulk-delete
    🟡  ≥ 40  — some signals; low risk but worth a quick look
    🔴  < 40  — limited signals; review before deleting
    """
    if confidence >= 70:
        return "🟢"
    if confidence >= 40:
        return "🟡"
    return "🔴"


def confidence_reason(g: SenderGroup) -> str:
    """
    Return a brief human-readable explanation of *why* this sender has
    the confidence score it does.

    Used as an explainability hint in the CLI:
      "Confidence: 92% (old emails + unsubscribe detected)"
    """
    parts: list[str] = []
    if g.has_unsubscribe:
        parts.append("unsubscribe detected")
    if g.inbox_days >= 90:
        parts.append("old emails")
    if g.count >= 30:
        parts.append("high frequency")
    subjects_lower = " ".join(g.sample_subjects).lower()
    if any(kw in subjects_lower for kw in _TRANSACTIONAL_KEYWORDS):
        parts.append("transactional keywords detected")
    return " + ".join(parts) if parts else "limited signals"


# ── Time estimation ───────────────────────────────────────────────────────────


def estimate_cleanup_seconds(total_emails: int) -> tuple[int, int]:
    """
    Estimate how long a purge operation will take, in seconds.

    Based on Gmail API batch-delete throughput (~100–200 emails/sec for
    trash operations, conservative to account for 429 rate-limiting bursts).
    Returns (min_seconds, max_seconds) — both at least 3s for very small sets.
    """
    min_secs = max(3, total_emails // 200)  # optimistic: 200 emails/sec
    max_secs = max(5, total_emails // 100)  # conservative: 100 emails/sec
    return (min_secs, max_secs)


def format_time_estimate(total_emails: int) -> str:
    """Format a cleanup time estimate as a readable range string."""
    lo, hi = estimate_cleanup_seconds(total_emails)
    if lo == hi:
        return f"~{lo}s"
    return f"~{lo}–{hi}s"


# ── Reclaimable percentage ────────────────────────────────────────────────────


def reclaimable_pct(reclaimable_mb_val: float, total_mb: float) -> float:
    """
    What fraction of the scanned inbox is reclaimable, as a percentage.
    Returns 0.0 if total_mb is zero (no division errors).
    """
    if total_mb <= 0:
        return 0.0
    return round(reclaimable_mb_val / total_mb * 100, 1)


# ── Share summary ─────────────────────────────────────────────────────────────


def generate_share_text(
    freed_mb: float,
    sender_count: int,
    email_count: int,
    elapsed_seconds: int | None = None,
) -> str:
    """
    Generate a concise, copyable share summary.

    Examples:
      "Freed 87 MB from 3 senders (495 emails) using mailtrim 🎉"
      "Freed 87 MB from 3 senders (495 emails) in 12s using mailtrim 🎉"

    elapsed_seconds=None means the user is previewing (stats --share),
    not reporting an actual completed purge.
    """
    time_part = f" in {elapsed_seconds}s" if elapsed_seconds is not None else ""
    sender_word = "sender" if sender_count == 1 else "senders"
    return (
        f"Freed {freed_mb} MB from {sender_count} {sender_word} "
        f"({email_count:,} emails{time_part}) using mailtrim 🎉"
    )


# ── Headline insight ─────────────────────────────────────────────────────────


def generate_headline_insight(
    insights: "InboxInsights",
    reclaim_pct: float,
    rec_count: int,
    reclaimable_mb_val: float,
) -> str:
    """
    Generate a punchy, personalised one-liner that is the very first thing
    the user reads. Designed to immediately convey the scale of the problem
    and make the tool feel like it "gets" them.

    Decision logic (most dramatic fact wins):
    - Heavy clutter (≥ 30%): lead with the percentage
    - Large absolute size (≥ 50 MB): lead with the MB
    - Old inbox (≥ 365d oldest): lead with time
    - High-volume, small files: acknowledge the noise
    - Clean inbox: celebrate it
    """
    if insights.total_scanned == 0:
        return "📭 No emails found matching the scan query."

    if reclaim_pct >= 30:
        return (
            f"💥 {reclaim_pct:.0f}% of your inbox is clutter — caused by just "
            f"{rec_count} sender{'s' if rec_count != 1 else ''}. "
            f"{reclaimable_mb_val} MB gone in one command."
        )

    if reclaimable_mb_val >= 50:
        return (
            f"🗄  {reclaimable_mb_val} MB sitting in your inbox — "
            f"{rec_count} sender{'s' if rec_count != 1 else ''} "
            "responsible. All of it deletable right now."
        )

    if insights.oldest_email_days >= 365:
        years = insights.oldest_email_days // 365
        return (
            f"⏳ You have emails going back {years} year{'s' if years != 1 else ''} — "
            "the oldest clutter is always the easiest to kill."
        )

    if reclaimable_mb_val > 0:
        return (
            f"📬 Scanned {insights.unique_senders} senders — "
            f"{rec_count} sender{'s' if rec_count != 1 else ''} "
            f"responsible for {reclaimable_mb_val} MB you don't need."
        )

    return "✅ Inbox looking clean — nothing worth deleting right now."


# ── Reading time estimate ─────────────────────────────────────────────────────


def estimate_reading_minutes(email_count: int) -> int:
    """
    Estimate how many minutes a user would spend triaging these emails.

    Assumes ~5 seconds per promotional/newsletter email (open, scan, close/delete).
    Used to make share text feel more visceral: "41 minutes of reading time reclaimed."
    """
    return max(0, round(email_count * 5 / 60))


# ── Viral share text ──────────────────────────────────────────────────────────


def generate_viral_share_text(
    freed_mb: float,
    sender_count: int,
    email_count: int,
    reclaim_pct: float = 0.0,
    elapsed_seconds: int | None = None,
    repo_url: str = "https://github.com/sadhgurutech/mailtrim",
) -> str:
    """
    Generate a multi-line, tweet/Slack-shaped share text designed to be
    copied and pasted. Reads like a brag, not a log line.

    Example output:
      🤯 495 emails deleted · 87 MB freed in 8s using mailtrim
         • 3 senders responsible
         • My inbox was 30% clutter — now it's clean
         • ~41 min of reading time reclaimed

      Core cleanup runs locally — no API key needed. Free forever.
      → https://github.com/sadhgurutech/mailtrim
    """
    time_part = f" in {elapsed_seconds}s" if elapsed_seconds is not None else ""
    pct_line = (
        f"\n   • My inbox was {reclaim_pct:.0f}% clutter — now it's clean"
        if reclaim_pct >= 5
        else ""
    )
    reading_mins = estimate_reading_minutes(email_count)
    reading_line = (
        f"\n   • ~{reading_mins} min of reading time reclaimed" if reading_mins >= 1 else ""
    )
    sender_word = "sender" if sender_count == 1 else "senders"
    return (
        f"🤯 {email_count:,} emails deleted · {freed_mb} MB freed{time_part} using mailtrim\n"
        f"   • {sender_count} {sender_word} responsible\n"
        f"   • Core cleanup runs locally — no API key needed"
        + pct_line
        + reading_line
        + f"\n\nFree forever. → {repo_url}"
    )


# ── Domain grouping ───────────────────────────────────────────────────────────


def group_by_domain(groups: list[SenderGroup]) -> list[DomainGroup]:
    """
    Merge per-address SenderGroups into per-domain DomainGroups.
    Example: jobs@linkedin.com + notifications@linkedin.com → linkedin.com
    """
    buckets: dict[str, list[SenderGroup]] = {}
    for g in groups:
        buckets.setdefault(g.domain, []).append(g)

    domains = [DomainGroup(domain=domain, senders=senders) for domain, senders in buckets.items()]
    compute_domain_impact_scores(domains)
    domains.sort(key=lambda d: d.impact_score, reverse=True)
    return domains


# ── Insights ──────────────────────────────────────────────────────────────────


@dataclass
class InboxInsights:
    top_storage: SenderGroup | None  # largest by size
    top_volume: SenderGroup | None  # most emails
    oldest: SenderGroup | None  # longest-standing clutter
    multi_sender_domains: list[DomainGroup]  # domains with 2+ addresses
    top_n_coverage_pct: float  # % of inbox from top 5 senders
    top_n_size_mb: float  # MB held by top 5 senders
    total_scanned: int
    total_size_bytes: int
    unique_senders: int
    unique_domains: int
    oldest_email_days: int

    @property
    def total_size_mb(self) -> float:
        return round(self.total_size_bytes / (1024 * 1024), 1)


def generate_insights(
    groups: list[SenderGroup],
    domain_groups: list[DomainGroup],
    top_n: int = 5,
) -> InboxInsights:
    if not groups:
        return InboxInsights(
            top_storage=None,
            top_volume=None,
            oldest=None,
            multi_sender_domains=[],
            top_n_coverage_pct=0,
            top_n_size_mb=0,
            total_scanned=0,
            total_size_bytes=0,
            unique_senders=0,
            unique_domains=0,
            oldest_email_days=0,
        )

    total_scanned = sum(g.count for g in groups)
    total_size = sum(g.total_size_bytes for g in groups)

    top_storage = max(groups, key=lambda g: g.total_size_bytes)
    top_volume = max(groups, key=lambda g: g.count)
    oldest = min(groups, key=lambda g: g.earliest_date)

    by_score = sorted(groups, key=lambda g: g.impact_score, reverse=True)
    top_slice = by_score[:top_n]
    top_n_count = sum(g.count for g in top_slice)
    top_n_size = sum(g.total_size_bytes for g in top_slice)

    coverage_pct = (top_n_count / total_scanned * 100) if total_scanned else 0
    multi = [d for d in domain_groups if len(d.senders) >= 2]

    oldest_days = max((g.inbox_days for g in groups), default=0)

    return InboxInsights(
        top_storage=top_storage,
        top_volume=top_volume,
        oldest=oldest,
        multi_sender_domains=multi,
        top_n_coverage_pct=round(coverage_pct, 1),
        top_n_size_mb=round(top_n_size / (1024 * 1024), 1),
        total_scanned=total_scanned,
        total_size_bytes=total_size,
        unique_senders=len(groups),
        unique_domains=len(domain_groups),
        oldest_email_days=oldest_days,
    )


# ── Recommendations ───────────────────────────────────────────────────────────


@dataclass
class Action:
    label: str  # "Delete all", "Keep last 10", etc.
    savings_mb: float  # Estimated MB freed (exact or ~)
    savings_exact: bool  # True = exact, False = estimate
    command: str  # Ready-to-run mailtrim command


@dataclass
class Recommendation:
    sender: SenderGroup
    actions: list[Action]
    confidence: int = 0  # 0–100; how safe this deletion is


def generate_recommendations(
    groups: list[SenderGroup],
    top_n: int = 3,
    domain_map: dict[str, DomainGroup] | None = None,
) -> list[Recommendation]:
    """
    For the top N senders by impact score, produce up to 2 concrete actions each.

    domain_map: optional {domain: DomainGroup} used so that count/size thresholds
    and savings figures reflect the full domain scope that ``purge --domain`` targets,
    not just the single sender address that stats grouped by.

    Decision logic (determines which actions are shown):
    - High-size sender    → Delete all (exact savings) + Delete older than 90d
    - High-count/low-size → Mark as read + Delete older than 30d
    - High-count + old    → Delete all + Keep last 10 (estimated savings)
    - Old clutter only    → Delete older than 90d
    - Tiny (< 1 MB)       → Delete older than 30d

    Commands use structured flags (not NL strings) for reliability:
      mailtrim purge --domain example.com --yes
      mailtrim purge --domain example.com --keep 10
      mailtrim purge --domain example.com --older-than 90
    """
    by_score = sorted(groups, key=lambda g: g.impact_score, reverse=True)
    recs: list[Recommendation] = []

    for g in by_score[:top_n]:
        actions: list[Action] = []
        domain = g.domain

        # Use domain-level totals when available so savings and thresholds
        # match what `purge --domain` will actually delete.
        d = domain_map.get(domain) if domain_map else None
        size_mb = d.total_size_mb if d else g.total_size_mb
        count = d.count if d else g.count
        days = g.inbox_days  # sender-level age is the right signal here

        # Action 1: always offer "delete all" if there's meaningful size
        if size_mb >= 1:
            actions.append(
                Action(
                    label="Delete all",
                    savings_mb=size_mb,
                    savings_exact=True,
                    command=f"mailtrim purge --domain {domain} --yes",
                )
            )

        # Action 2: depends on the sender profile
        if size_mb < 3 and count >= 30:
            # High noise, low storage — mark as read first, then age-based delete
            actions.append(
                Action(
                    label="Mark all as read",
                    savings_mb=0,
                    savings_exact=True,
                    command=f"mailtrim bulk mark-read --domain {domain}",
                )
            )
            actions.append(
                Action(
                    label="Delete older than 30d",
                    savings_mb=round(size_mb * 0.85, 1),
                    savings_exact=False,
                    command=f"mailtrim purge --domain {domain} --older-than 30",
                )
            )

        elif count >= 50 and days >= 60:
            # High-count, long history — keep a small recent tail
            keep = 10
            fraction_deleted = max(0, (count - keep) / count)
            actions.append(
                Action(
                    label=f"Keep last {keep}",
                    savings_mb=round(size_mb * fraction_deleted, 1),
                    savings_exact=False,
                    command=f"mailtrim purge --domain {domain} --keep {keep}",
                )
            )

        elif days >= 60:
            # Old clutter — delete by age
            actions.append(
                Action(
                    label="Delete older than 90d",
                    savings_mb=round(size_mb * 0.85, 1),
                    savings_exact=False,
                    command=f"mailtrim purge --domain {domain} --older-than 90",
                )
            )

        if size_mb < 1:
            # Too small for size-based actions; still useful as a noise cleanup
            actions.append(
                Action(
                    label="Delete older than 30d",
                    savings_mb=round(size_mb * 0.8, 1),
                    savings_exact=False,
                    command=f"mailtrim purge --domain {domain} --older-than 30",
                )
            )

        recs.append(
            Recommendation(
                sender=g,
                actions=actions[:2],
                confidence=compute_confidence_score(g),
            )
        )

    return recs


# ── Reclaimable space + quick win ─────────────────────────────────────────────


def reclaimable_mb(recs: list[Recommendation]) -> float:
    """
    Total MB that could be freed by executing the primary action for each recommendation.
    This is a conservative floor — the real savings may be higher if secondary
    actions are also taken.
    """
    return round(sum(rec.actions[0].savings_mb for rec in recs if rec.actions), 1)


def quick_win(recs: list[Recommendation]) -> Recommendation | None:
    """
    The single recommendation most worth doing first.

    Composite score: 60% confidence + 40% impact.
    Confidence is weighted higher so the "quick win" feels safe to act on
    immediately, not just impactful.
    """
    if not recs:
        return None
    return max(recs, key=lambda r: r.confidence * 0.6 + r.sender.impact_score * 0.4)


# ── Fetch + pipeline ─────────────────────────────────────────────────────────


def fetch_sender_groups(
    client: GmailClient,
    query: str = "category:promotions OR label:newsletters",
    max_messages: int = 2000,
    min_count: int = 2,
    top_n: int = 30,
    sort_by: SortKey = "score",
) -> list[SenderGroup]:
    """
    Fetch emails matching query, group by sender, score, and return ranked list.

    sort_by: "score" (default) | "count" | "oldest" | "size"
    """
    ids = client.list_message_ids(query=query, max_results=max_messages)
    if not ids:
        return []

    messages = _fetch_metadata_batch(client, ids)

    # Group by sender address
    accumulators: dict[str, _Accumulator] = {}
    for msg in messages:
        key = msg.sender_email
        if key not in accumulators:
            accumulators[key] = _Accumulator(sender_email=key, sender_name=msg.sender_name)
        accumulators[key].add(msg)

    result = [acc.to_group() for acc in accumulators.values() if acc.count >= min_count]

    # Score first (needed for default sort)
    compute_impact_scores(result)

    if sort_by == "oldest":
        result.sort(key=lambda g: g.earliest_date)
    elif sort_by == "size":
        result.sort(key=lambda g: g.total_size_bytes, reverse=True)
    elif sort_by == "count":
        result.sort(key=lambda g: g.count, reverse=True)
    else:  # "score" (default)
        result.sort(key=lambda g: g.impact_score, reverse=True)

    return result[:top_n]


# ── Internal helpers ──────────────────────────────────────────────────────────


class _Accumulator:
    def __init__(self, sender_email: str, sender_name: str):
        self.sender_email = sender_email
        self.sender_name = sender_name
        self.count = 0
        self.total_size_bytes = 0
        self.earliest_ts = float("inf")
        self.latest_ts = 0.0
        self.subjects: list[str] = []
        self.message_ids: list[str] = []
        self.has_unsubscribe = False

    def add(self, msg: Message) -> None:
        self.count += 1
        self.total_size_bytes += msg.size_estimate
        ts = msg.internal_date or 0
        if ts and ts < self.earliest_ts:
            self.earliest_ts = ts
        if ts and ts > self.latest_ts:
            self.latest_ts = ts
        if msg.headers.subject and len(self.subjects) < 3:
            self.subjects.append(msg.headers.subject[:80])
        self.message_ids.append(msg.id)
        if msg.headers.list_unsubscribe:
            self.has_unsubscribe = True

    def to_group(self) -> SenderGroup:
        now_ts = datetime.now(timezone.utc).timestamp() * 1000
        earliest = self.earliest_ts if self.earliest_ts != float("inf") else now_ts
        latest = self.latest_ts if self.latest_ts else now_ts
        return SenderGroup(
            sender_email=self.sender_email,
            sender_name=self.sender_name,
            count=self.count,
            total_size_bytes=self.total_size_bytes,
            earliest_date=datetime.fromtimestamp(earliest / 1000, tz=timezone.utc),
            latest_date=datetime.fromtimestamp(latest / 1000, tz=timezone.utc),
            sample_subjects=self.subjects,
            message_ids=self.message_ids,
            has_unsubscribe=self.has_unsubscribe,
        )


def _fetch_metadata_batch(client: GmailClient, ids: list[str]) -> list[Message]:
    """
    Fetch message metadata in batches.
    Uses client._fetch_batch() which correctly avoids the closure-over-loop-variable bug.
    """
    settings = get_settings()
    results: list[Message] = []

    from mailtrim.core.gmail_client import _chunks

    for chunk in _chunks(ids, settings.gmail_batch_size):
        # Delegate to the client's tested batch helper rather than duplicating the pattern
        batch_msgs = client._fetch_batch(
            chunk,
            format="metadata",
            # metadata headers are set inside _fetch_batch via the service call
        )
        results.extend(batch_msgs)

    return results
