# Changelog

All notable changes to mailtrim are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

---

## [0.3.0] — 2026-05-01

### Added
- `mailtrim setup` — guided first-time onboarding: provider selection (Gmail/IMAP),
  auth, health checks, and first inbox scan in ~2 minutes
- `mailtrim stats --since <Nd>` and `mailtrim purge --since <Nd>` — time-based filtering;
  translates to `newer_than:Nd` for Gmail and `SINCE` criteria for IMAP
- `mailtrim stats --share` — shareable summary output in Twitter (≤280 chars) or plain format;
  no personal data, top domains only
- **AI trust boundary system** — `ai_status_line()` helper; AI state badge (`AI: OFF / LOCAL / CLOUD`)
  visible in `stats`, `quickstart`, and `doctor`; `_cloud_ai_warning()` panel shown before any
  cloud AI command; `require_cloud()` wrapped in try/except in all four AI commands for clean exits

### Changed
- `quickstart` redesigned for instant value: ≤10 lines of output, safe candidates, undo hint,
  best first action surfaced immediately
- README rewritten for v0.3.0: trust-first framing, 35% shorter, structured for GitHub visitors
- `AIModeError` now renders multi-line messages in `_handle_error` (first line bold, rest dim)

---

## [0.2.1] — 2026-04-11

### Added
- `mailtrim doctor` — health check command: verifies auth token, Gmail connection,
  Trash access, data directory, undo storage, config, and optional local AI endpoint.
  Prints ✓/⚠/✗ per check with actionable fix hints. Exits non-zero when required checks fail.
- `mailtrim quickstart` — guided first-run command: checks auth, scans 500 messages,
  explains what was found, surfaces the single safest first cleanup action.
- `--verbose` / `--simple` flags on `stats`:
  - `--verbose` shows ACCOUNT SUMMARY, KEY INSIGHTS, domain patterns, full TOP SENDERS table
  - `--simple` shows plain-language recommendations without scores or tables
- `mailtrim stats --max-scan` default raised from 300 → 1000 for better coverage
- Human-readable error messages: 401/expired token, network timeouts, permission errors,
  rate limits, and database corruption all show plain-language guidance instead of raw tracebacks
- Local-only usage metrics (`~/.mailtrim/usage.json`): command runs, emails trashed,
  undo count, first run date — never uploaded, used only for local product insight
- `DEMO.md` — 60-second demo script for recording an asciinema/vhs walkthrough

### Changed
- `--permanent` flag on `purge` is now hidden from `--help` and requires a second
  `--i-understand-permanent` flag; confirmation phrase changed to `DELETE FOREVER`
- `--imap-password` CLI flag removed from `stats` and `purge` — now read from
  `MAILTRIM_IMAP_PASSWORD` env var or interactive hidden prompt (no shell history leak)
- `purge` docstring: "delete" → "move to Trash (recoverable)"
- `stats` docstring: "delete" → "move to Trash"
- `_action_explanation()` now says "Moves … to Trash (recoverable)" instead of "Deletes"
- `digest`, `avoid`, `follow-up`, and `stats --ai` marked `[EXPERIMENTAL]` in help text
- `undo` completion message: "Restored X emails" with progress spinner
- `_print_cleanup_complete`: undo hint is now bold and prominent

### Fixed
- `test_confidence_safety_label_medium` and `_low` tests updated to match current
  `confidence_safety_label()` return values ("Needs review", "Sensitive / personal")

---

## [0.1.0] — 2026-04-05

### Added
- `stats` — rank senders by storage impact with confidence scoring (no API key needed)
- `purge` — interactive bulk delete with 30-day undo window
- `triage` — optional AI inbox classification via Claude (subjects + snippets only, never full body)
- `sync` — pull inbox into local cache for fast repeated queries
- `unsubscribe` — RFC 8058 one-click + mailto fallback + Playwright headless fallback
- `follow-up` — conditional reminder drafts ("remind me if they haven't replied")
- `rules` — save and replay natural language cleanup rules
- `avoid` — surface emails viewed 3+ times with no action taken
- `digest` — weekly plain-text inbox summary
- `undo` — reverse any bulk operation within the 30-day window
- MockAIEngine — full test suite runs without Gmail credentials or Anthropic key
- 115 tests passing on Python 3.11, 3.12, 3.13
