"""AI engine — Claude-powered classification, NL→rule translation, and explainability."""

from __future__ import annotations

import json
from dataclasses import dataclass

import anthropic

from mailtrim.config import get_settings
from mailtrim.core.gmail_client import Message

# ── Data models ─────────────────────────────────────────────────────────────


@dataclass
class ClassifiedEmail:
    gmail_id: str
    category: str  # e.g. "action_required", "newsletter", "notification", "conversation", "receipt", "spam"
    priority: str  # "high", "medium", "low"
    explanation: str  # One-line human-readable reason
    suggested_action: str  # "reply", "archive", "unsubscribe", "delete", "keep", "delegate"
    requires_reply: bool
    deadline_hint: str  # e.g. "today", "this week", "" — extracted from content


@dataclass
class BulkOperation:
    gmail_query: str  # Gmail search query to find affected messages
    action: str  # "archive", "trash", "label", "mark_read", "unsubscribe"
    action_params: dict  # e.g. {"label_name": "newsletters"}
    explanation: str  # Human-readable description of what will happen
    estimated_count_hint: str  # e.g. "likely hundreds of messages"
    confidence: float  # 0.0–1.0


@dataclass
class NLRule:
    natural_language: str
    gmail_query: str
    action: str
    action_params: dict
    explanation: str
    warnings: list[str]  # Potential issues to warn the user about


CATEGORIES = {
    "action_required": "Requires a decision or response from you",
    "conversation": "Human-to-human exchange requiring no immediate action",
    "newsletter": "Periodic content you subscribed to",
    "notification": "Automated system/app notification",
    "receipt": "Order confirmation, invoice, or financial record",
    "calendar": "Meeting invite or calendar notification",
    "social": "Social network notification",
    "spam": "Unsolicited or irrelevant",
    "other": "Doesn't fit above categories",
}

SYSTEM_PROMPT = """\
You are an expert email classifier and automation assistant for mailtrim, \
a privacy-first Gmail management tool. You help users understand and organize \
their email with clear, honest explanations. Never invent information not \
present in the email. Be concise and direct.\
"""


# ── Main AI engine ───────────────────────────────────────────────────────────


class AIEngine:
    def __init__(self, api_key: str | None = None):
        settings = get_settings()
        key = api_key or settings.anthropic_api_key
        if not key:
            raise ValueError(
                "ANTHROPIC_API_KEY is not set. "
                "Set it as an environment variable before running this command."
            )
        self._client = anthropic.Anthropic(api_key=key)
        self._model = settings.ai_model

    # ── Classification ───────────────────────────────────────────────────────

    def classify_emails(self, messages: list[Message]) -> list[ClassifiedEmail]:
        """Classify a batch of emails. Returns one ClassifiedEmail per message."""
        settings = get_settings()
        results: list[ClassifiedEmail] = []

        for chunk in _chunks(messages, settings.ai_max_classify_batch):
            results.extend(self._classify_batch(chunk))

        return results

    def _classify_batch(self, messages: list[Message]) -> list[ClassifiedEmail]:
        email_summaries = []
        for i, msg in enumerate(messages):
            email_summaries.append(
                f"EMAIL {i + 1} (id={msg.id}):\n"
                f"From: {msg.headers.from_}\n"
                f"Subject: {msg.headers.subject}\n"
                f"Snippet: {msg.snippet[:300]}"
            )

        prompt = f"""\
Classify the following {len(messages)} emails. For each, provide a JSON object with:
- gmail_id: the email id given
- category: one of {list(CATEGORIES.keys())}
- priority: "high", "medium", or "low"
- explanation: ONE sentence explaining why you classified it this way
- suggested_action: one of "reply", "archive", "unsubscribe", "delete", "keep", "delegate"
- requires_reply: true/false — does this email need a reply from the user?
- deadline_hint: if there's a time constraint, brief hint ("today", "this week", "by Friday"), else ""

Respond with a JSON array of objects, one per email, in the same order. Nothing else.

{chr(10).join(email_summaries)}\
"""
        response = self._client.messages.create(
            model=self._model,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = response.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]

        parsed: list[dict] = json.loads(raw)
        results = []
        for item, msg in zip(parsed, messages):
            results.append(
                ClassifiedEmail(
                    gmail_id=item.get("gmail_id", msg.id),
                    category=item.get("category", "other"),
                    priority=item.get("priority", "medium"),
                    explanation=item.get("explanation", ""),
                    suggested_action=item.get("suggested_action", "keep"),
                    requires_reply=bool(item.get("requires_reply", False)),
                    deadline_hint=item.get("deadline_hint", ""),
                )
            )
        return results

    # ── Natural language → Gmail query + action ──────────────────────────────

    def translate_rule(self, natural_language: str) -> NLRule:
        """Convert a natural language instruction to a Gmail query + action."""
        prompt = f"""\
Convert the following natural language email rule into a structured automation.

Rule: "{natural_language}"

Respond with a single JSON object with these fields:
- gmail_query: a valid Gmail search query string (using Gmail operators: from:, to:, subject:, \
  older_than:, newer_than:, has:attachment, label:, is:unread, etc.)
- action: one of "archive", "trash", "label", "mark_read", "unsubscribe"
- action_params: dict of action-specific params (for "label": {{"label_name": "..."}}; else {{}})
- explanation: one sentence describing what this rule will do in plain English
- warnings: list of strings — edge cases or potential issues the user should know about
- confidence: float 0.0–1.0 — how confident you are the query accurately captures the intent

Respond with valid JSON only. No markdown.\
"""
        response = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]

        data = json.loads(raw)
        return NLRule(
            natural_language=natural_language,
            gmail_query=data["gmail_query"],
            action=data["action"],
            action_params=data.get("action_params", {}),
            explanation=data.get("explanation", ""),
            warnings=data.get("warnings", []),
        )

    # ── Bulk operation intent parsing ────────────────────────────────────────

    def parse_bulk_intent(self, instruction: str) -> BulkOperation:
        """Parse a one-off bulk operation instruction (not a recurring rule)."""
        prompt = f"""\
Parse the following bulk email operation request into a structured action.

Request: "{instruction}"

Respond with a JSON object:
- gmail_query: Gmail search query to find the affected messages
- action: one of "archive", "trash", "label", "mark_read", "unsubscribe"
- action_params: dict (for "label": {{"label_name": "..."}})
- explanation: plain English description of what will happen (be specific)
- estimated_count_hint: rough estimate of how many emails this might affect ("dozens", "hundreds", etc.)
- confidence: 0.0–1.0

Respond with valid JSON only.\
"""
        response = self._client.messages.create(
            model=self._model,
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]

        data = json.loads(raw)
        return BulkOperation(
            gmail_query=data["gmail_query"],
            action=data["action"],
            action_params=data.get("action_params", {}),
            explanation=data.get("explanation", ""),
            estimated_count_hint=data.get("estimated_count_hint", ""),
            confidence=float(data.get("confidence", 0.8)),
        )

    # ── Weekly digest summary ────────────────────────────────────────────────

    def generate_digest(
        self,
        inbox_summary: dict,
        follow_ups: list[dict],
        avoided_count: int,
        top_senders: list[dict],
    ) -> str:
        """Generate a plain-English weekly digest summary."""
        prompt = f"""\
Generate a concise, useful weekly email digest for the user. Be direct and actionable.

Inbox stats: {json.dumps(inbox_summary)}
Overdue follow-ups: {json.dumps(follow_ups)}
Emails being avoided (viewed but not acted on): {avoided_count}
Top senders by volume this week: {json.dumps(top_senders)}

Write a brief (under 200 words) digest in plain text. Structure:
1. One-line overview
2. Action items (follow-ups that need attention)
3. Quick win (1 cleanup suggestion based on the data)

Be honest and helpful, not cheerful or corporate.\
"""
        response = self._client.messages.create(
            model=self._model,
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()

    # ── Avoidance analysis ───────────────────────────────────────────────────

    def analyze_avoided_email(self, msg: Message) -> str:
        """Suggest why the user is avoiding this email and what to do."""
        prompt = f"""\
The user has viewed this email multiple times but hasn't replied, archived, or deleted it.
Suggest in 1-2 sentences why they might be avoiding it and one concrete action they could take.

From: {msg.headers.from_}
Subject: {msg.headers.subject}
Snippet: {msg.snippet[:400]}

Be direct and empathetic. No filler words.\
"""
        response = self._client.messages.create(
            model=self._model,
            max_tokens=150,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()


# ── Helpers ──────────────────────────────────────────────────────────────────


def _chunks(lst: list, n: int) -> list:
    for i in range(0, len(lst), n):
        yield lst[i : i + n]
