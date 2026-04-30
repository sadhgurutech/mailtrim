"""
Optional local LLM integration — llama.cpp server at http://localhost:8080.

Drop-in enhancement: if the server is unavailable, every call returns {}
and the caller falls back to rule-based logic unchanged.

Usage:
    from mailtrim.core.llm import analyze_email, analyze_batch, confidence_delta
    from mailtrim.core.llm import classify_for_triage  # triage fallback
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING

from mailtrim.core.ai.client import AIClient, get_ai_client

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from mailtrim.core.ai_engine import ClassifiedEmail
    from mailtrim.core.gmail_client import Message

# ── Constants ─────────────────────────────────────────────────────────────────

# Default client — llama.cpp at localhost:8080.
# Override via analyze_email(ai_client=...) or replace _default_client at startup.
_default_client: AIClient = get_ai_client(backend="llama")

# Prompt uses TinyLlama/llama.cpp chat template so the model generates a
# completion rather than echoing the instructions back.
_SYSTEM_PROMPT = "You are an email classifier. Only output S/C/A lines, nothing else."

# Few-shot examples teach the format without relying on instruction-following.
_FEW_SHOT = (
    "<|user|>\n"
    "From: Bank\nSubject: Security alert\nNew login from unknown device.</s>\n"
    "<|assistant|>\n"
    "S:New login detected on account\nC:important\nA:keep</s>\n"
    "<|user|>\n"
    "From: Deals\nSubject: 50% off this weekend only\nShop now for huge savings.</s>\n"
    "<|assistant|>\n"
    "S:Weekend sale fifty percent discount\nC:promo\nA:delete</s>\n"
    "<|user|>\n"
    "From: GitHub\nSubject: Build failed on main\nYour CI run #42 failed.</s>\n"
    "<|assistant|>\n"
    "S:CI build failed on main branch\nC:update\nA:archive</s>\n"
)

_VALID_CATEGORIES = frozenset({"important", "promo", "update", "spam"})
_VALID_ACTIONS = frozenset({"keep", "archive", "delete"})

# AI signal → confidence delta.  Spam overrides action; capped at ±15.
_ACTION_DELTA: dict[str, int] = {"delete": 10, "archive": -3, "keep": -10}
_SPAM_DELTA = 15  # applied instead of action delta when category == "spam"
_DELTA_CAP = 15

# Per-category display icon — consistent across stats, purge, triage.
CATEGORY_ICON: dict[str, str] = {
    "important": "🟢",
    "promo": "🟠",
    "update": "🔵",
    "spam": "🔴",
}


# ── Core function ─────────────────────────────────────────────────────────────


def analyze_email(
    text: str,
    cache_key: str = "",
    ai_client: AIClient | None = None,
) -> dict:
    """
    Analyze email text via the configured local AI backend.

    Args:
        text:      Subject + snippet. Auto-truncated to 600 chars.
        cache_key: Optional sender email / domain for result caching.
                   Pass "" to skip caching.
        ai_client: Override the module-level default client.
                   Pass an OllamaClient or LlamaCppClient to switch backends
                   without changing global state.

    Returns:
        On success: {"summary": str, "category": str, "action": str}
        On any failure: {} — caller must treat this as "no AI signal".
    """
    if cache_key:
        cached = get_cached(cache_key)
        if cached:
            return cached

    content = text.strip()[:600]
    prompt = (
        f"<|system|>\n{_SYSTEM_PROMPT}</s>\n{_FEW_SHOT}<|user|>\n{content}</s>\n<|assistant|>\n"
    )

    client = ai_client or _default_client
    response_text = client.generate(prompt)
    if not response_text:
        return {}

    result = _parse_response(response_text)
    if cache_key and result:
        set_cached(cache_key, result)
    return result


def analyze_batch(
    texts: list[str],
    cache_keys: list[str] | None = None,
    max_workers: int = 4,
    ai_client: AIClient | None = None,
) -> list[dict]:
    """
    Analyze multiple email texts in parallel (bounded thread pool).

    Args:
        texts:       Email content strings (one per sender).
        cache_keys:  Optional per-entry keys for result caching (same length as texts).
        max_workers: Thread concurrency cap.
        ai_client:   Override the module-level default client.

    Results are returned in the same order as ``texts``.
    Individual failures produce {} entries, not exceptions.
    """
    keys = cache_keys or [""] * len(texts)
    results: list[dict] = [{}] * len(texts)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(analyze_email, t, k, ai_client): i
            for i, (t, k) in enumerate(zip(texts, keys))
        }
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                results[idx] = fut.result()
            except Exception as exc:
                logger.debug("Batch LLM item %d failed (%s): %s", idx, type(exc).__name__, exc)
                results[idx] = {}
    return results


# ── In-memory cache (keyed by sender email or domain) ────────────────────────

_cache: dict[str, dict] = {}


def get_cached(key: str) -> dict:
    """Return a cached AI result for *key*, or {} if not cached."""
    return _cache.get(key, {})


def set_cached(key: str, result: dict) -> None:
    """Store an AI result in the process-level cache."""
    if result:
        _cache[key] = result


# ── Display helpers ───────────────────────────────────────────────────────────

# Words that carry no signal when used as the entire "reason".
_FILLER = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "it",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "and",
        "or",
        "with",
        "your",
        "you",
        "we",
        "our",
        "this",
        "that",
    }
)


def _short_summary(summary: str, max_words: int = 6) -> str:
    """
    Trim a raw summary to ≤max_words meaningful words.

    Strips filler-only prefixes and truncates cleanly.
    """
    words = summary.strip().split()
    # Drop leading filler words
    start = 0
    for w in words:
        if w.lower().rstrip(".,!?") in _FILLER:
            start += 1
        else:
            break
    meaningful = words[start:] or words  # never return empty
    return " ".join(meaningful[:max_words])


def format_ai_line(ai: dict) -> str:
    """
    Build the inline AI insight line shown under each sender.

    Format:  [AI] 🟠 promo → delete · "weekly sale"

    Placing action immediately after category makes the intent obvious even
    when category and action appear to contradict each other (e.g. promo→keep).
    The quoted summary provides the reason without cluttering the main line.
    """
    cat = ai.get("category", "")
    action = ai.get("action", "")
    icon = CATEGORY_ICON.get(cat, "")
    short = _short_summary(ai.get("summary", ""))
    return f'[AI] {icon} {cat} → {action} · "{short}"'


# ── Confidence integration ────────────────────────────────────────────────────


def confidence_delta(ai_result: dict) -> int:
    """
    Map AI signal to a confidence score adjustment, capped at ±_DELTA_CAP.

    spam category overrides the action-based delta (strongest signal).
    Positive → more confident it is bulk/deletable.
    Negative → AI says keep or archive → reduce confidence.
    """
    if not ai_result:
        return 0
    if ai_result.get("category") == "spam":
        return _SPAM_DELTA
    delta = _ACTION_DELTA.get(ai_result.get("action", ""), 0)
    return max(-_DELTA_CAP, min(_DELTA_CAP, delta))


# ── Impact score nudge ───────────────────────────────────────────────────────


def apply_impact_nudge(groups: list, ai_insights: dict[str, dict]) -> None:
    """
    Adjust SenderGroup.impact_score in-place based on AI category signal.

    promo  → +5  (more likely to be cleanable clutter)
    important → -10 (reduce urgency to delete)

    Changes are small and bounded to [0, 100] so the base formula dominates.
    Called only when --ai is active.
    """
    _nudge = {"promo": 5, "important": -10}
    for g in groups:
        ai = ai_insights.get(g.sender_email, {})
        delta = _nudge.get(ai.get("category", ""), 0)
        if delta:
            g.impact_score = max(0, min(100, g.impact_score + delta))


# ── Eligibility filter ────────────────────────────────────────────────────────


_SENSITIVE_KEYWORDS: frozenset[str] = frozenset(
    {"bank", "finance", "financial", "card", "statement", "credit", "loan", "invest"}
)


def should_analyze(
    rule_confidence: int,
    email_count: int,
    sender_email: str,
    *,
    is_top_sender: bool = False,
) -> bool:
    """
    Return True only for senders where AI adds value.

    Always analyze:
    - Top recommended senders (is_top_sender=True) — AI validates the recommendation.
    - Senders with bank/finance keywords — high cost of mistaken deletion.

    Skip AI when:
    - Rule confidence is already very high (≥80) and sender is not sensitive.
    - Very high email count (>50) with moderate-high confidence — frequency is decisive.

    Run AI when:
    - Low confidence (<60) — AI may resolve ambiguity.
    - Small send volume (<20) — fewer signals for rules, AI adds more.
    """
    if is_top_sender:
        return True
    email_lower = sender_email.lower()
    if any(kw in email_lower for kw in _SENSITIVE_KEYWORDS):
        return True
    if rule_confidence >= 80:
        return False
    if email_count > 50 and rule_confidence >= 60:
        return False
    return True


# ── Internal ──────────────────────────────────────────────────────────────────


def _parse_response(text: str) -> dict:
    """
    Parse the grammar-constrained model output.

    The grammar guarantees the format is exactly:
        S:<summary>
        C:<important|promo|update|spam>
        A:<keep|archive|delete>

    Returns {} only if something unexpected slipped through.
    """
    summary: str | None = None
    category: str | None = None
    action: str | None = None

    for line in text.splitlines():
        line = line.strip()
        if line.startswith("S:") and summary is None:
            summary = line[2:].strip()
        elif line.startswith("C:") and category is None:
            category = line[2:].strip()
        elif line.startswith("A:") and action is None:
            action = line[2:].strip()

    if not summary or category not in _VALID_CATEGORIES or action not in _VALID_ACTIONS:
        logger.debug(
            "parse failed: summary=%r category=%r action=%r",
            summary,
            category,
            action,
        )
        return {}

    return {"summary": summary, "category": category, "action": action}


# ── Triage adapter ────────────────────────────────────────────────────────────

# Map local LLM categories → triage display categories + metadata defaults.
# The local model uses a simpler 4-way split; we widen it to match ClassifiedEmail.
_TRIAGE_MAP: dict[str, tuple[str, str, str, bool]] = {
    #           category         priority  suggested_action  requires_reply
    "important": ("action_required", "high", "reply", True),
    "promo": ("newsletter", "low", "unsubscribe", False),
    "update": ("notification", "low", "archive", False),
    "spam": ("spam", "low", "delete", False),
}

# When local LLM says "keep" but category implies deletable, trust the category.
# "archive" and "delete" are mapped directly.
_ACTION_OVERRIDE: dict[str, str] = {
    "archive": "archive",
    "delete": "delete",
}


def classify_for_triage(messages: list[Message]) -> list[ClassifiedEmail]:
    """
    Classify a list of Gmail messages using the local llama.cpp server.

    Fallback chain per message:
      1. local LLM (localhost:8080) — best signal
      2. MockAIEngine               — rule-based heuristics (subject keywords,
                                      List-Unsubscribe header, etc.)

    The triage table is always fully populated regardless of server availability.

    Returns:
        list[ClassifiedEmail] in the same order as ``messages``.
    """
    from mailtrim.core.ai_engine import ClassifiedEmail
    from mailtrim.core.mock_ai import MockAIEngine

    mock = MockAIEngine()

    texts = [
        f"From: {m.sender_name or m.sender_email}\nSubject: {m.headers.subject}\n{m.snippet[:500]}"
        for m in messages
    ]
    raw_results = analyze_batch(texts, max_workers=4)

    classified: list[ClassifiedEmail] = []
    for msg, result in zip(messages, raw_results):
        if result:
            llm_cat = result["category"]
            llm_action = result["action"]
            llm_summary = result["summary"]

            triage_cat, priority, default_action, requires_reply = _TRIAGE_MAP[llm_cat]
            suggested_action = _ACTION_OVERRIDE.get(llm_action, default_action)

            classified.append(
                ClassifiedEmail(
                    gmail_id=msg.id,
                    category=triage_cat,
                    priority=priority,
                    explanation=f"[local AI] {llm_summary}",
                    suggested_action=suggested_action,
                    requires_reply=requires_reply,
                    deadline_hint="",
                )
            )
        else:
            # Local LLM unavailable — fall back to MockAIEngine heuristics.
            classified.append(mock.classify_emails([msg])[0])

    return classified
