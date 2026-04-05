# Threat Model

This document maps every trust boundary in mailtrim and the threats considered at each one.
Update it whenever a PR crosses a new boundary or adds a new data flow.

## Trust boundaries

### 1. Gmail API → mailtrim (inbound email data)

**What crosses this boundary:**
- Email headers: `From`, `Subject`, `List-Unsubscribe`, `List-Unsubscribe-Post`, `Date`, `Message-ID`
- Email metadata: `sizeEstimate`, `internalDate`, `labelIds`
- Email snippets (≤300 chars, used for AI features)
- Email body HTML/text (used only by headless unsubscribe path to find unsubscribe links)

**Threat: attacker-controlled header values**
A malicious sender controls every header field. Headers must be treated as untrusted input.
- `List-Unsubscribe` URLs → validated by `_is_safe_url()` before any fetch (SSRF prevention)
- `Subject` → used in AI prompts; treated as plain text, never executed
- `From` → used as a display label only; never passed to a shell or eval

**Threat: oversized or malformed inputs**
- Subjects are truncated to 80 chars before storage (`_Accumulator.add`)
- Snippets are truncated to 300 chars before sending to Anthropic (`ai_engine.py`)

**Mitigations in place:** `_is_safe_url()` in `unsubscribe.py`; field truncation in `sender_stats.py` and `ai_engine.py`

---

### 2. mailtrim → Anthropic API (outbound AI data)

**What crosses this boundary:**
- Email subjects (plain text, truncated to 80 chars)
- Email snippets (≤300 chars, no full body)
- Sender names/addresses (display only)
- Natural language instructions typed by the user

**What never crosses this boundary:**
- Full email body content
- OAuth tokens
- Local file paths

**Threat: PII leakage**
Subject lines may contain names, account numbers, or other PII. This is disclosed to users
at runtime via the `[AI]` notice printed before every AI command, and documented in
`PRIVACY.md` and `README.md`.

**Threat: prompt injection via email content**
A malicious sender could craft a subject like "Ignore previous instructions and...".
Mitigations: subjects are passed as data inside structured JSON payloads, not interpolated
raw into prompt text. Claude models are resistant to prompt injection in structured inputs,
but not immune — this is a known limitation of LLM-based features.

**Mitigations in place:** `_print_ai_data_notice()` in `cli/main.py`; data minimisation in
`ai_engine.py`; disclosure in `README.md` and `PRIVACY.md`

---

### 3. mailtrim → web (outbound URL fetches)

**What crosses this boundary:**
- `List-Unsubscribe` header URLs (attacker-controlled)
- Unsubscribe links found in email body HTML (attacker-controlled)
- Playwright browser navigation to unsubscribe pages

**Threat: SSRF (Server-Side Request Forgery)**
A malicious email can contain a `List-Unsubscribe` header pointing at internal
infrastructure (e.g. `http://169.254.169.254/latest/meta-data/`, `http://10.0.0.1/admin`).
Fixed in v0.1.1 via `_is_safe_url()`.

**Threat: redirect-based SSRF**
A public URL that redirects to a private IP. Mitigated by `follow_redirects=False` on all
`httpx` calls.

**Threat: DNS rebinding**
A hostname that resolves to a public IP at validation time but rebinds to a private IP at
fetch time. Partially mitigated — `_is_safe_url()` checks all resolved addresses, but a
determined attacker with control over DNS TTLs could still rebind between the check and the
fetch. Acceptable risk for a local CLI tool; would require a network-level fix (e.g. binding
to a specific interface) to fully eliminate.

**Mitigations in place:** `_is_safe_url()` in `unsubscribe.py`; `follow_redirects=False`
on all `httpx` calls

---

### 4. Local disk → mailtrim (config, tokens, database)

**What crosses this boundary:**
- `~/.mailtrim/token.json` — OAuth refresh token
- `~/.mailtrim/credentials.json` — OAuth client secrets
- `~/.mailtrim/.env` — Anthropic API key
- `~/.mailtrim/mailtrim.db` — SQLite database (cached email metadata, undo log, rules)

**Threat: token theft via weak file permissions**
OAuth tokens written with default permissions would be readable by other local users.
Mitigated: `token.json` is written `chmod 0o600` by `gmail_client.py`.

**Threat: credential leakage via logging**
API keys or tokens could be accidentally logged to stdout/stderr.
Mitigated: no logging framework is used; `token.json` and `.env` contents are never printed.

**Mitigations in place:** `chmod 0o600` on `token.json`; `chmod 600 ~/.mailtrim/.env`
recommended in README

---

## What is explicitly out of scope

- **Multi-user / server deployment**: mailtrim is a single-user local CLI. Server-side
  multi-tenancy threats (account isolation, rate limiting, auth bypass) are not modelled.
- **Physical access attacks**: if an attacker has physical or root access to the machine,
  all local secrets are compromised regardless. Out of scope.
- **Anthropic infrastructure**: threats within Anthropic's systems are governed by
  [Anthropic's privacy policy](https://www.anthropic.com/privacy), not this document.

---

## Adding a new feature that crosses a trust boundary?

Before writing code, answer these three questions:

1. **Who controls this input?** (user, email sender, Gmail API, Anthropic, local disk)
2. **What is the worst case if that input is malicious?**
3. **What is the validation / sanitisation layer?**

Then add a row to the relevant section above and tick the security checklist in your PR.
