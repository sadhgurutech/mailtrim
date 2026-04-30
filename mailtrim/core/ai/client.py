"""Local AI backend abstraction — llama.cpp and Ollama.

Design:
  - AIClient.generate(prompt) → raw string from model
  - Callers (llm.py) handle parsing, caching, and confidence logic unchanged
  - Both clients fail silently on network errors — returning "" signals no AI signal
  - No new dependencies: urllib only

Usage:
    client = get_ai_client(backend="ollama", url="http://localhost:11434", model="phi3")
    raw = client.generate(prompt)   # "" on any failure
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

_TIMEOUT = 2  # seconds — must not slow down CLI noticeably on CPU-only machines


# ── Base class ────────────────────────────────────────────────────────────────


class AIClient:
    """
    Abstract local AI backend.

    Subclasses implement _post(prompt) → raw response string.
    generate() wraps _post with timeout enforcement and silent failure.
    """

    def generate(self, prompt: str) -> str:
        """
        Run inference and return the model's raw text output.

        Returns "" on any failure (network error, timeout, parse error).
        Callers must treat "" as "no AI signal" and fall back to rule-based logic.
        """
        truncated = prompt[:600]
        logger.debug("→ prompt sent (%d chars): %s", len(truncated), truncated[:120])
        try:
            result = self._post(truncated)
            logger.debug("← raw response: %r", result)
            return result
        except urllib.error.URLError as exc:
            logger.debug("%s unavailable (URLError): %s", self.__class__.__name__, exc)
        except TimeoutError as exc:
            logger.debug("%s timeout: %s", self.__class__.__name__, exc)
        except json.JSONDecodeError as exc:
            logger.warning("%s returned invalid JSON: %s", self.__class__.__name__, exc)
        except Exception as exc:
            logger.debug(
                "%s request failed (%s): %s", self.__class__.__name__, type(exc).__name__, exc
            )
        logger.debug("← no response (failure)")
        return ""

    def _post(self, prompt: str) -> str:
        raise NotImplementedError

    def _http_post(self, url: str, payload: dict) -> dict:
        """Shared HTTP POST helper — returns parsed JSON response dict."""
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # nosec B310 — localhost only
            return json.loads(resp.read().decode("utf-8"))


# ── llama.cpp backend ─────────────────────────────────────────────────────────


class LlamaCppClient(AIClient):
    """
    Client for llama.cpp server (github.com/ggerganov/llama.cpp).

    Default endpoint: http://localhost:8080/completion
    The server must be running with a GBNF grammar to constrain output format.
    """

    # GBNF grammar constrains the model to only emit valid tokens.
    # This makes the S/C/A parser in llm.py trivial and eliminates hallucinations.
    _GRAMMAR = (
        'root ::= "S:" summary "\\nC:" category "\\nA:" action\n'
        "summary ::= [^\\n]+\n"
        'category ::= "important" | "promo" | "update" | "spam"\n'
        'action ::= "keep" | "archive" | "delete"'
    )

    def __init__(self, url: str = "http://localhost:8080") -> None:
        self._endpoint = url.rstrip("/") + "/completion"

    def _post(self, prompt: str) -> str:
        payload = {
            "prompt": prompt,
            "n_predict": 60,
            "temperature": 0.1,
            "grammar": self._GRAMMAR,
            "stop": ["</s>", "<|user|>", "<|system|>"],
        }
        data = self._http_post(self._endpoint, payload)
        return data.get("content", "").strip()


# ── Ollama backend ────────────────────────────────────────────────────────────


class OllamaClient(AIClient):
    """
    Client for Ollama (ollama.ai).

    Default endpoint: http://localhost:11434/api/generate
    Default model:    phi3  (small, fast on CPU; user-configurable)

    Ollama's response format:
        {"response": "S:...\nC:...\nA:..."}
    """

    def __init__(
        self,
        url: str = "http://localhost:11434",
        model: str = "phi3",
    ) -> None:
        self._endpoint = url.rstrip("/") + "/api/generate"
        self._model = model

    def _post(self, prompt: str) -> str:
        payload = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
        }
        data = self._http_post(self._endpoint, payload)
        return data.get("response", "").strip()


# ── Factory ───────────────────────────────────────────────────────────────────


def get_ai_client(
    backend: str = "llama",
    url: str = "",
    model: str = "phi3",
) -> AIClient:
    """
    Construct and return the appropriate AIClient.

    Args:
        backend: "llama" (llama.cpp) or "ollama"
        url:     Override the default server URL
        model:   Model name — only used by Ollama

    The returned client fails silently on all network errors, so the pipeline
    degrades gracefully when no local AI server is running.
    """
    if backend == "ollama":
        return OllamaClient(
            url=url or "http://localhost:11434",
            model=model,
        )

    if backend == "llama":
        return LlamaCppClient(url=url or "http://localhost:8080")

    raise ValueError(f"Unknown AI backend '{backend}'. Valid options: llama, ollama")
