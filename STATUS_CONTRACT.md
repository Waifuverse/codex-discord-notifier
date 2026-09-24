# Discord status contract

Codex's native notification event reports that a turn ended; it does not expose a
goal status field. Agents make status explicit by ending final responses with one
machine-readable HTML comment:

```text
<!-- discord-status
state: complete
title: Vehicle suspension parity recovered
summary: Focused tests and the file-size guard passed.
progress: 100% (6/6 exit criteria)
tests: 18/18 passed
-->
```

Valid states are `complete`, `blocked`, `needs_input`, `failed`, and
`turn_complete`. The notifier ignores unknown fields and treats malformed or
missing contracts as `turn_complete`, while retaining whether a valid contract
was actually present. Ordinary desktop footerless turns are suppressed. A structured
`turn_complete` is eligible only when `DISCORD_SEND_TURN_COMPLETE=true`.
Discord-originated final answers are an exception: the reply bridge relays the
full answer to its mapped conversation even without a status contract.

Only the primary/root agent emits a contract. Sub-agents report to their parent
without a contract. While an explicit goal remains active, all intermediate turns,
progress replies, automatic continuations, and individual sub-agent completions
omit the contract entirely. The primary sends at most one aggregated contract when
the whole goal completes, genuinely blocks, needs indispensable user input, or
fails terminally. A status request does not make a running goal terminal.

Use `complete` only when the requested outcome is genuinely achieved. Use
`blocked` only for a genuine impasse. Use `needs_input` when a user decision or
credential is required but useful work may resume immediately after it arrives.
Do not include secrets, source code, raw prompts, or sensitive paths in fields.
