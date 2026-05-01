"""AI mode enforcement — single place that decides whether an AI call is allowed.

Three modes (set via `mailtrim config ai-mode` or MAILTRIM_AI_MODE env var):

  off   → default; no AI calls whatsoever.  Privacy-safe out of the box.
  local → only local backends (Ollama, llama.cpp).  Nothing leaves the machine.
  cloud → external API calls permitted (Anthropic Claude).
          Sends email subjects and snippets to Anthropic's servers.

Callers must call require_local() before any local-model call and
require_cloud() before any external API call.  Both raise AIModeError
with actionable guidance if the current mode doesn't permit the call.
"""

from __future__ import annotations

_VALID_MODES = ("off", "local", "cloud")


class AIModeError(Exception):
    """Raised when an AI call is blocked by the current ai_mode setting."""


def validate_mode(mode: str) -> str:
    """Return mode if valid, else raise ValueError."""
    if mode not in _VALID_MODES:
        raise ValueError(f"Invalid ai_mode '{mode}'. Valid values: {', '.join(_VALID_MODES)}")
    return mode


def require_local(mode: str) -> None:
    """
    Assert that local AI calls are permitted.

    Allowed in: local, cloud
    Blocked in: off
    """
    if mode == "off":
        raise AIModeError(
            "Local AI is disabled (ai_mode=off).\n"
            "Enable it with:  mailtrim config ai-mode local\n"
            "Local AI runs entirely on your machine — no data leaves it."
        )


def require_cloud(mode: str) -> None:
    """
    Assert that cloud (external) AI calls are permitted.

    Allowed in: cloud
    Blocked in: off, local
    """
    if mode == "off":
        raise AIModeError(
            "AI is disabled (ai_mode=off).\n"
            "To use cloud AI:  mailtrim config ai-mode cloud\n"
            "Warning: cloud mode sends email subjects and snippets to Anthropic."
        )
    if mode == "local":
        raise AIModeError(
            "Cloud AI is blocked (ai_mode=local).\n"
            "To allow external calls:  mailtrim config ai-mode cloud\n"
            "Warning: cloud mode sends email subjects and snippets to Anthropic."
        )
