# Changelog

All notable changes to mailtrim are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### Added
- `--scope anywhere` flag on `stats`, `purge`, and `sync` — scans all mail
  (`in:anywhere -in:trash -in:spam`), not just inbox. Surfaces hidden bloat
  in archived, sent, and all-mail folders. Most storage waste is not in inbox.

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
