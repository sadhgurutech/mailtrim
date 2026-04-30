# mailtrim

**mailtrim helps you clean your inbox safely in seconds — everything goes to Trash first, undo anytime, nothing leaves your machine.**

Free, open-source. Core features need no API key.

[![PyPI](https://img.shields.io/pypi/v/mailtrim.svg)](https://pypi.org/project/mailtrim/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![CI](https://github.com/sadhgurutech/mailtrim/actions/workflows/ci.yml/badge.svg)](https://github.com/sadhgurutech/mailtrim/actions/workflows/ci.yml)

---

> 🤯 495 emails deleted · 87.4 MB freed in 8s using mailtrim
> 💥 34% of your inbox is clutter — caused by just 3 senders.

mailtrim is a CLI tool that finds inbox clutter, ranks it by impact, and bulk-deletes it safely — with a 30-day undo window.

**Core workflow (`stats`, `purge`, `undo`) is fully local** — no API key required, nothing sent anywhere. Optional AI commands (`triage`, `bulk`, `avoid`, `digest`, `rules --add`) send only email subjects and 300-character snippets to Anthropic for classification — never full body content. See [Anthropic's privacy policy](https://www.anthropic.com/privacy) for how API data is handled on their side.

No subscription. No black box.

---

## Why not SaneBox / Superhuman?

The paid tools charge $7–$40/month, process your email on their servers, and still don't solve the problems that matter most:

| Problem | SaneBox / Superhuman | mailtrim |
|---------|----------------------|------------|
| "Remind me only if they haven't replied" | ✗ Not solved | ✅ Conditional follow-up |
| *Why* did AI move this email? | ✗ Black box | ✅ One-line explanation per email |
| Natural language bulk cleanup | ✗ Not solved | ✅ "Archive newsletters older than 60 days" |
| 30-day undo for bulk operations | ✗ Not solved | ✅ Full undo log |
| "Emails I keep avoiding" detection | ✗ Not solved | ✅ AI insight per avoided email |
| Unsubscribe success rate | 70–85% | ✅ Near-100% (headless browser fallback) |
| Privacy — core commands local | ✗ Cloud-processed | ✅ Core: local only. AI commands: subjects/snippets to Anthropic |
| Cost | $7–$40/month | **Free** |

---

## Privacy

- **All data stays in `~/.mailtrim/`** — no external servers, no telemetry, no analytics
- **OAuth token** is written `chmod 0o600` (owner read-only)
- **Local AI (Ollama/llama.cpp)** — fully offline; nothing leaves your machine. Use `--ai-backend ollama` or `--ai-backend llama`.
- **Cloud AI features** send only email subjects and snippets to Anthropic — never full body content. See [Anthropic's privacy policy](https://www.anthropic.com/privacy) for their data handling.
- **No AI key?** — everything except `triage`, `bulk`, `avoid`, `digest`, and `rules --add` works without one. Local AI enrichment also needs no Anthropic key.
- **Why `gmail.modify` scope?** This grants read, compose, trash, and label access — mailtrim uses it to list messages, move mail to Trash, and manage labels. The scope technically permits reading full body content; mailtrim fetches metadata only and never reads or stores body text.
- **Why `gmail.send` scope?** The `follow-up` command creates reminder drafts. It is never called by `stats`, `purge`, `triage`, `bulk`, `undo`, or any cleanup command. If you don't use `follow-up`, this permission is never exercised.
- **Revoking access:** Go to [myaccount.google.com/permissions](https://myaccount.google.com/permissions) and remove mailtrim. Delete `~/.mailtrim/token.json` locally to complete the removal.
- See [PRIVACY.md](PRIVACY.md) for the full data flow

---

## What's free vs. paid?

| Feature | Commands | Cost |
|---------|----------|------|
| Inbox analysis + bulk delete | `stats`, `purge`, `undo`, `sync`, `unsubscribe`, `follow-up`, `rules --run` | **Free — no API key needed** |
| Local AI enrichment (sender confidence) | `stats --ai-backend ollama`, `purge --ai-backend llama` | **Free — runs on your machine** (requires Ollama or llama.cpp) |
| Cloud AI classification + NL cleanup | `triage`, `bulk`, `avoid`, `digest`, `rules --add` | Requires [Anthropic API key](https://console.anthropic.com) · ~$0.01–0.05 per run |

The core cleanup workflow — scan, rank, delete, undo — costs nothing and requires no AI key. Local AI enrichment (Ollama/llama.cpp) is also free and fully offline. Cloud AI features are optional and pay-per-use; there is no subscription.

---

## 60-Second Quick Start

Already have credentials.json from Google? This is all you need:

```bash
pip install mailtrim
mailtrim auth        # opens browser once
mailtrim quickstart  # guided first cleanup
```

Not set up yet? See the full setup below — it takes about 15 minutes once.

---

## Safe by Default

- **Everything goes to Trash first** — nothing is permanently deleted unless you explicitly use `--permanent` (hidden flag, requires a second confirmation flag)
- **30-day undo window** — run `mailtrim undo` anytime to reverse any cleanup
- **All data stays on your machine** — `~/.mailtrim/` only, no telemetry, no cloud sync
- **Dry-run first** — most commands show you what they'd do before asking you to confirm

```bash
mailtrim purge --domain linkedin.com   # shows what would be deleted, asks to confirm
mailtrim undo                          # shows recent operations, pick one to reverse
mailtrim doctor                        # checks auth, storage, and connection health
```

---

## Common Fixes

If something isn't working, run:

```bash
mailtrim doctor
```

This checks auth, Gmail connection, storage, and optional AI — and tells you exactly what to fix.

| Symptom | Fix |
|---------|-----|
| "Gmail connection expired" | `mailtrim auth` |
| "Token file not found" | `mailtrim auth` |
| "Cannot write to ~/.mailtrim/" | `chmod 700 ~/.mailtrim` |
| "Rate limit hit" | Wait 60 seconds, retry with `--max-scan 300` |
| Scan feels slow | Use `--max-scan 500` (default is 1000) |
| Not seeing enough senders | Try `mailtrim stats --scope anywhere` |

---

## Quick start (~20 minutes first time, ~30 seconds after)

### 1. Install

```bash
git clone https://github.com/sadhgurutech/mailtrim
cd mailtrim
python3 -m venv venv && source venv/bin/activate
pip install -e .

# Optional: headless browser for near-100% unsubscribe success
pip install -e ".[headless]" && playwright install chromium
```

### 2. Get Gmail API credentials (one-time setup, ~15 minutes)

This is a standard OAuth setup — you're authorising yourself to access your own inbox. Google never charges for this. You only do this once; after that, `mailtrim auth` refreshes your token automatically.

> **Stuck?** The OAuth consent screen step trips up most people. When asked for "User type", choose **External**. Under "Test users", add your own Gmail address. That's it — you don't need to publish the app.

1. Go to [console.cloud.google.com](https://console.cloud.google.com) → **New project**
2. **APIs & Services** → **Enable APIs** → search **Gmail API** → Enable
3. **OAuth consent screen** → External → add your Gmail as a **test user**
4. **Credentials** → Create → **OAuth 2.0 Client ID** → Desktop app → Download JSON
5. Save it: `mv ~/Downloads/client_secret_*.json ~/.mailtrim/credentials.json`

> **Scopes requested:** `gmail.modify` (read, trash, label management) and `gmail.send` (follow-up drafts). `gmail.modify` grants the capability to read body content — mailtrim never does, but you should know the scope allows it.

> **"This app isn't verified" warning:** Google shows this for any OAuth app that hasn't gone through their review process. It is expected and safe to proceed — you are authorising your own app to access your own inbox. Click **Advanced → Go to mailtrim (unsafe)** to continue.

### 3. Authenticate

```bash
mailtrim auth
# Opens browser → click Allow → done
```

### 4. See what's in your inbox

```bash
mailtrim stats
```

**Sample output** *(illustrative — your numbers will vary)*:
```
Scan complete — 2,000 emails · 38 senders

34% of your inbox is clutter — caused by just 3 senders. 87.4 MB gone in one command.

TOTAL RECLAIMABLE SPACE
  You can safely free ~87.4 MB (34.0% of scanned inbox)
  from your top 3 senders · Each cleanup takes ~3-5s
  All deletions go to Trash — undo anytime

 #  Impact         Sender                Emails  Size     Oldest       Risk
 1  100 (High)     LinkedIn Jobs            312  44.0MB   847d ago     Safe to clean
 2   82 (High)     Substack Weekly          183  26.1MB   512d ago     Safe to clean
 3   51 (Medium)   GitHub Notifications     147   9.3MB    91d ago     Needs review
 4   29 (Low)      Shopify                   94  12.2MB   203d ago     Safe to clean
 5   18 (Low)      Medium Daily Digest       87  11.4MB   445d ago     Safe to clean

Impact = 60% storage + 40% volume (0-100)
```

### 5. Bulk delete the offenders

```bash
mailtrim purge
```

**Sample output** *(illustrative)*:
```
  Top Email Offenders  (823 emails · 102.3 MB)
 # │ Sender                      │ Emails │ Size  │ Latest  │ Sample subject
───┼─────────────────────────────┼────────┼───────┼─────────┼─────────────────────────────
 1 │ LinkedIn Jobs <jobs@li...>  │   312  │  44MB │ Apr 03  │ 12 new jobs matching your...
 2 │ Substack <hello@subst...>   │   183  │  26MB │ Apr 01  │ This week: AI is eating...
 3 │ GitHub <noreply@github...>  │   147  │   9MB │ Apr 04  │ [myrepo] New issue opened...

Select senders to delete.
Enter numbers (1,3), ranges (1-5), all, or q to quit.

Your selection: 1,2

Selected 2 senders — 495 emails (70 MB):
  ✕ LinkedIn Jobs (312 emails)
  ✕ Substack (183 emails)

Move 495 emails to Trash? (undo available for 30 days) [y/N]: y

✓ Moved 495 emails to Trash. Undo log ID: 1 (mailtrim undo 1)
```

### 6. Share what you cleaned

```bash
mailtrim stats --share
```

The command outputs the following text, ready to copy and paste:

```
🤯 495 emails deleted · 87.4 MB freed in 8s using mailtrim
   • 3 senders responsible
   • Core cleanup runs locally — no API key needed
   • My inbox was 34% clutter — now it's clean
   • ~41 min of reading time reclaimed

Free forever. → https://github.com/sadhgurutech/mailtrim
```

---

## All Commands

### `quickstart` — Guided first cleanup *(no AI needed)*

```bash
mailtrim quickstart   # checks auth, scans inbox, shows your first safe action
```

![mailtrim quickstart](mailtrim/screenshots/quickstart.png)

### `doctor` — Health check

```bash
mailtrim doctor        # checks auth, Gmail, storage, config
mailtrim doctor --ai   # also checks local AI endpoint
```

![mailtrim doctor](mailtrim/screenshots/doctor.png)

### `stats` — Quick inbox overview *(no AI needed)*

```bash
mailtrim stats
mailtrim stats --json                        # machine-readable output

# Use with any IMAP account (Outlook, Fastmail, iCloud, self-hosted…)
mailtrim stats --provider imap \
  --imap-server imap.fastmail.com \
  --imap-user you@fastmail.com
# IMAP password is read from MAILTRIM_IMAP_PASSWORD env var or prompted securely

# Enrich confidence scores with a local AI model (no Anthropic key needed)
mailtrim stats --ai-backend ollama --ai-model phi3    # requires Ollama running
mailtrim stats --ai-backend llama                      # requires llama.cpp at localhost:8080
```

![mailtrim stats](mailtrim/screenshots/stats.png)

### `purge` — Bulk delete by sender *(no AI needed)*

**How the Risk/Confidence score works:**

Three signals combine to estimate how safe bulk-deletion is (0–100):

| Signal | Weight | Logic |
|--------|--------|-------|
| `List-Unsubscribe` header present | 30 pts | Sender self-identifies as bulk/marketing |
| Age ≥ 180 days in inbox | up to 35 pts | Emails sitting >6 months are rarely actionable |
| Volume ≥ 50 from one sender | up to 35 pts | High frequency = almost certainly automated |

🟢 ≥70 = Safe to clean · 🟡 40–69 = Needs review · 🔴 Sensitive / personal (bank, health, legal — never auto-deleted)

Scores are heuristics — the 30-day undo exists precisely because no heuristic is perfect.

```bash
mailtrim purge                          # sort by email count (default)
mailtrim purge --sort oldest            # show oldest clutter first
mailtrim purge --sort size              # largest senders first
mailtrim purge --query "older_than:1y"  # custom query
mailtrim purge --unsub                  # also unsubscribe while deleting
mailtrim purge --permanent              # skip Trash — IRREVERSIBLE
mailtrim purge --json                   # output sender list as JSON

# IMAP account
mailtrim purge --provider imap --imap-server imap.outlook.com --imap-user you@outlook.com

# Local AI enrichment
mailtrim purge --ai-backend ollama --ai-model phi3
```

### `sync` — Pull inbox into local cache

```bash
mailtrim sync             # last 200 messages
mailtrim sync --limit 500
mailtrim sync --query "in:inbox is:unread"
```

### `triage` — AI inbox classification

```bash
mailtrim triage           # classify unread inbox
mailtrim triage --limit 50
```

Every email gets: **priority** (high/medium/low) · **category** · **why** · **suggested action**

### `bulk` — Natural language bulk operations

```bash
mailtrim bulk "archive all newsletters I haven't opened in 60 days"
mailtrim bulk "delete all emails from noreply@* older than 1 year"
mailtrim bulk "label receipts from order@ or receipt@ senders"
mailtrim bulk "archive LinkedIn notifications" --dry-run  # preview first
```

### `undo` — Reverse a bulk operation (within 30 days)

```bash
mailtrim undo        # list recent operations
mailtrim undo 42     # undo operation #42
```

### `follow-up` — Conditional follow-up tracking

```bash
mailtrim follow-up <message-id> --days 3   # remind only if no reply in 3 days
mailtrim follow-up --list                   # see what's due today
mailtrim follow-up --sync                   # check threads for replies
```

### `avoid` — Emails you keep putting off

```bash
mailtrim avoid                               # show with AI insight
mailtrim avoid --no-insights                 # faster, no AI
mailtrim avoid --process <id> --action archive
```

### `unsubscribe` — Unsubscribe that actually works

```bash
mailtrim unsubscribe newsletters@company.com
mailtrim unsubscribe --from-query "label:newsletters" --limit 20
mailtrim unsubscribe --history
```

### `rules` — Recurring automation

```bash
mailtrim rules --add "archive LinkedIn notifications older than 7 days"
mailtrim rules --list
mailtrim rules --run
mailtrim rules --run --dry-run
```

### `digest` — Weekly inbox summary

```bash
mailtrim digest
```

---

## Configuration

All settings via environment variables or `~/.mailtrim/.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | *(not set)* | Anthropic API key. Without it, mock AI mode is used. |
| `MAILTRIM_AI_MODEL` | `claude-sonnet-4-6` | Claude model for AI features |
| `MAILTRIM_DRY_RUN` | `false` | Global dry-run (preview without executing) |
| `MAILTRIM_UNDO_WINDOW_DAYS` | `30` | How long undo logs are kept |
| `MAILTRIM_AVOIDANCE_VIEW_THRESHOLD` | `3` | Views before an email is "avoided" |
| `MAILTRIM_FOLLOW_UP_DEFAULT_DAYS` | `3` | Default follow-up window |
| `MAILTRIM_DIR` | `~/.mailtrim` | Where tokens, DB, and config are stored |
| `MAILTRIM_IMAP_PASSWORD` | *(not set)* | IMAP password for `--provider imap` (avoids interactive prompt) |

**`~/.mailtrim/.env` example:**
```
ANTHROPIC_API_KEY=sk-ant-...
MAILTRIM_DRY_RUN=false
MAILTRIM_UNDO_WINDOW_DAYS=30
```

> **Security note:** Restrict permissions on this file — `chmod 600 ~/.mailtrim/.env` — so only your user account can read the API key.

---

## Testing (no credentials required)

```bash
# Run all tests — zero API calls, zero credentials needed
python -m pytest tests/ -v

# With coverage
python -m pytest tests/ --cov=mailtrim --cov-report=term-missing
```

All AI paths are covered by `MockAIEngine` — the full CLI can be exercised without any API key.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Bug reports and feature requests welcome via [GitHub Issues](../../issues).

---

## Architecture

```
mailtrim/
├── config.py              # Settings (env vars, ~/.mailtrim/.env)
├── core/
│   ├── providers/
│   │   ├── base.py        # EmailProvider ABC — 8-method interface
│   │   ├── gmail.py       # Gmail implementation (OAuth + REST API)
│   │   ├── imap.py        # IMAP implementation (stdlib only, SSL, batch fetch)
│   │   └── factory.py     # get_provider("gmail"|"imap", ...) — selection point
│   ├── ai/
│   │   └── client.py      # AIClient ABC + LlamaCppClient + OllamaClient
│   ├── gmail_client.py    # Gmail API: OAuth, CRUD, batching, retry on 429/5xx
│   ├── storage.py         # Local SQLite: emails, follow-ups, rules, undo log
│   ├── ai_engine.py       # Claude API: classify, NL→query, digest, avoidance
│   ├── mock_ai.py         # Deterministic stub — full testing without API key
│   ├── llm.py             # Local scoring engine: confidence, recommendations
│   ├── follow_up.py       # Conditional follow-up: only surfaces if no reply
│   ├── bulk_engine.py     # NL → dry-run preview → execute → 30-day undo
│   ├── avoidance.py       # "Emails you avoid" detector + per-email AI insight
│   ├── unsubscribe.py     # RFC 8058 one-click + mailto + Playwright headless
│   ├── sender_stats.py    # Sender aggregation, risk classification, scoring
│   ├── validation.py      # Input sanitization (query strings, user input)
│   ├── diagnostics.py     # doctor command checks (auth, storage, connection)
│   ├── errors.py          # Human-readable error translation layer
│   └── usage_stats.py     # Local-only run metrics (never uploaded)
└── cli/main.py            # Typer + Rich CLI — 15 commands
```

---

## License

[MIT](LICENSE) — free to use, modify, and distribute.
