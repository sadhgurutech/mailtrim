## Summary

<!-- What does this PR do? One paragraph max. -->

## Related issue

Closes #<!-- issue number -->

## Changes

-
-
-

## Testing

```bash
# How to test this change manually
mailtrim <command>
```

- [ ] `python -m pytest tests/` passes
- [ ] `ruff check mailtrim/` passes
- [ ] Tested manually with a real Gmail account (if applicable)
- [ ] No email content (body text/HTML) is logged or stored by new code
- [ ] New destructive operations have an undo path or explicit irreversibility warning

## Security checklist

<!-- Complete this for any change that touches external data or network I/O. -->

- [ ] **URL fetching** — any URL derived from email content passes through `_is_safe_url()` before fetch
- [ ] **Untrusted input** — data from email headers/bodies/senders is treated as attacker-controlled; no eval, no shell exec, no path traversal
- [ ] **Outbound data** — nothing beyond subjects + 300-char snippets is sent to Anthropic; no full body content
- [ ] **Disk writes** — any new file paths are validated; sensitive files created with `chmod 0o600`
- [ ] **New trust boundary** — if this PR crosses a new boundary (network, disk, subprocess), it is documented in `THREAT_MODEL.md`

## Screenshots / sample output

<!-- If your change affects CLI output, paste a before/after here. -->
