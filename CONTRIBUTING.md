# Contributing to mailtrim

Thanks for taking the time to contribute. This document covers everything you need to go from zero to a merged pull request.

---

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [Project Structure](#project-structure)
- [Making Changes](#making-changes)
- [Testing](#testing)
- [Pull Request Process](#pull-request-process)
- [Design Principles](#design-principles)

---

## Code of Conduct

Be kind, direct, and constructive. We welcome contributors of all experience levels. Harassment of any kind will not be tolerated.

---

## Getting Started

1. **Browse open issues** — look for [`good first issue`](../../issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22) labels
2. **Comment on the issue** before starting work to avoid duplicate effort
3. **Fork the repository** and work on a feature branch

---

## Development Setup

```bash
git clone https://github.com/sadhgurutech/mailtrim
cd mailtrim

# Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# Install in editable mode with dev dependencies
pip install -e ".[dev]"

# Set up your Gmail credentials (see README.md for full steps)
cp /path/to/client_secret.json ~/.mailtrim/credentials.json

# Set your Anthropic key (optional — mock mode works without it)
export ANTHROPIC_API_KEY=sk-ant-...

# Run the test suite to confirm everything works
python -m pytest tests/ -v
```

---

## Project Structure

```
mailtrim/
├── config.py          # Settings via env vars / ~/.mailtrim/.env
├── core/
│   ├── ai/
│   │   ├── client.py      # AI provider abstraction (Anthropic / local / mock)
│   │   └── mode.py        # ai_mode enforcement: off | local | cloud
│   ├── providers/
│   │   ├── base.py        # EmailProvider ABC (get_profile, list_messages, …)
│   │   ├── factory.py     # get_provider() — returns Gmail or IMAP instance
│   │   ├── gmail.py       # Gmail provider (OAuth)
│   │   └── imap.py        # IMAP provider (Outlook, Yahoo, custom)
│   ├── gmail_client.py    # Gmail API: OAuth, CRUD, batching, retry
│   ├── storage.py         # Local SQLite via SQLAlchemy
│   ├── llm.py             # Claude API integration (classification, NL→query)
│   ├── mock_ai.py         # Drop-in AI stub for testing without an API key
│   ├── diagnostics.py     # Health checks used by `mailtrim doctor`
│   ├── errors.py          # Friendly error messages for common failures
│   ├── usage_stats.py     # Local-only command run counters (never uploaded)
│   ├── follow_up.py       # Conditional follow-up tracker
│   ├── bulk_engine.py     # Bulk operations: preview → execute → undo
│   ├── avoidance.py       # "Emails you're avoiding" detector
│   ├── unsubscribe.py     # List-Unsubscribe + Playwright headless fallback
│   └── sender_stats.py    # Aggregate emails by sender for purge/stats
└── cli/
    └── main.py            # Typer CLI — all user-facing commands
tests/
    conftest.py            # Shared fixtures — clean_db (in-memory SQLite)
    test_storage.py        # SQLite layer tests
    test_purge.py          # Sender aggregation + selection parser tests
    test_mock_ai.py        # MockAIEngine tests (all AI paths, no API key needed)
    test_ai_trust_boundary.py  # AI mode badges, cloud warning, off-mode blocking
    test_diagnostics.py    # doctor checks and usage stats
    test_smarter_confidence.py # Confidence scoring and sender blocklist
    test_stats_share.py    # stats --share text generation
    test_since_filter.py   # --since flag validation and query translation
    test_validation.py     # Input validation helpers
    test_privacy.py        # privacy command output
    test_setup.py          # setup command (Gmail + IMAP paths)
```

**Adding a new command:**
1. Add a function decorated with `@app.command()` in `cli/main.py`
2. Put business logic in `mailtrim/core/` (keep CLI thin)
3. Add tests in `tests/`

**Adding a new core feature:**
1. Create `mailtrim/core/my_feature.py`
2. Import it lazily inside the relevant CLI command (keeps startup fast)
3. Add tests

---

## Making Changes

- **One concern per PR** — don't mix bug fixes with new features
- **Keep the CLI thin** — `cli/main.py` should only handle I/O and call core modules
- **No new required dependencies** without discussion — check `pyproject.toml`
- **Privacy first** — never log or store email body content; snippets/subjects only

---

## Testing

```bash
# Run all tests — zero API calls, zero credentials needed
python -m pytest tests/ -v

# Run a specific test file
python -m pytest tests/test_mock_ai.py -v

# Run with coverage
python -m pytest tests/ --cov=mailtrim --cov-report=term-missing

# Lint
ruff check mailtrim/
```

The test suite is designed to run **without a Gmail account or Anthropic key**. The `MockAIEngine` covers all AI paths. Use `pytest -m "not integration"` if you add integration tests requiring real credentials.

**Tests that touch the database** must use the `clean_db` fixture from `tests/conftest.py`. It provides an isolated in-memory SQLite instance per test — no files written to `~/.mailtrim`. Add it as an autouse fixture at the module level:

```python
@pytest.fixture(autouse=True)
def _use_clean_db(clean_db):
    pass
```

---

## Pull Request Process

1. Run `python -m pytest tests/` — all tests must pass
2. Run `ruff check mailtrim/` — no lint errors
3. Write a clear PR description: what changed, why, how to test it
4. Link the related issue (e.g. `Closes #42`)
5. Keep PRs focused — one logical change per PR

A maintainer will review within a few days. We may ask for changes before merging.

---

## Design Principles

These guide every decision in this project:

1. **Privacy by default** — all state in local SQLite; nothing stored externally
2. **AI trust boundary** — users always know whether data leaves the machine (`ai_mode: off | local | cloud`); no silent fallback
3. **Reversibility** — destructive operations have a 30-day undo window
4. **Transparency** — every AI decision has a one-line human-readable explanation
5. **Beginner-friendly** — works without an Anthropic key; helpful error messages
6. **No over-engineering** — prefer simple and obvious over clever and abstract
