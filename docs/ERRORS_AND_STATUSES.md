# Discord errors and statuses

Checked against the installed Codex 0.149.0 experimental app-server JSON schema
on 2026-09-06, plus the local rollout and task-history formats. Protocol support
does not imply every event is written to local history; the bridge only reports
events it can actually observe in tasks mapped to the configured Discord owner.

## Task errors

All named CodexErrorInfo variants in that schema have specific messages:

| Category | Discord explanation/action |
| --- | --- |
| Account usage exhausted | Usage allowance reached; service-provided retry date when available, otherwise explicitly unknown. |
| Context window exceeded | Task context too large; review/compact context in Codex. |
| Session budget exceeded | Configured task token budget reached; review the budget rather than assume account exhaustion. |
| Model at capacity | Retry later or choose another available model. |
| Request rate limit | Temporary request throttling; no assumption about account allowance. |
| Authentication rejected | Check sign-in securely in Codex. |
| Invalid request | Check request/model/settings before retrying. |
| Rollback failed | Inspect task/files; do not assume rollback happened. |
| Policy restriction | Review the restriction and revise to a supported request; do not recommend bypasses. |
| HTTP/stream connection failure or disconnection | Check connection and partial work before retrying. |
| Response retries exhausted | Retry attempts exhausted, distinct from account token limits. |
| Internal service error | Retry later; review diagnostics if persistent. |
| Sandbox/environment error | Review execution environment and permissions. |
| Active turn cannot be steered | Wait for review/compaction; check whether the message is already queued. |
| Other/unrecognized | Cause unavailable; inspect the original task. No guessed diagnosis. |

Known structured HTTP status codes and review/compaction activity may be included.
Raw messages, commands, paths, arbitrary additionalDetails, and secret fields are
not forwarded. Retry dates are extracted only from recognized service wording;
an unspecified timezone is never invented. No alert automatically buys credits,
redeems resets, changes models, resends a task, or grants permissions.

## Other task statuses

- **Goal paused:** explicitly paused; resume through Codex.
- **Goal budget-limited:** configured goal budget stopped progress; reports numeric
  usage/budget if supplied, then directs the user to Codex.
- **Goal usage-limited:** account usage stopped the goal; does not invent a reset.
- **Interrupted:** turn stopped; no assumption that it crashed or ran out of tokens.
- **Question / native action:** Discord-answerable questions are distinguished
  from secure input and approval dialogs that still require Codex.
- **Complete / blocked / failed:** remain governed by the primary agent's status
  contract; the goal watcher does not duplicate those completion messages.

Goal status notices emit only on relevant transitions, not changing token counts.
Active/progress events remain quiet. Errors explicitly marked as retrying do not
produce terminal-failure notices. Errors for the same failed turn are deduplicated;
pending generic notices can be enriched by detailed task history. Already sent
historical messages are not replayed.

## Bridge and media statuses already covered

Queued replies use the thinking reaction. Missing/archived tasks, missing working
directories/images, unavailable CLI/storage, and uncertain receipt have explicit
messages. An uncertain send is never automatically resent. Media validation,
capture interruption/failure, oversized uploads, and overdue output delivery have
their own explanations. The independent watchdog reports persistent disconnection,
stale heartbeat, unsuccessful worker cycles, stuck outputs, and recovery.

## Observation limits

Final response delivery is deduplicated by task and turn, including when a turn
consumes multiple Discord replies or completion hooks run concurrently. All
included inputs are marked handled and one final response references the latest
included message. Existing historical duplicate Discord messages are retained;
the fix does not delete or resend old conversation messages.

Question/answer routing is tested with two simultaneous tasks, reverse-order
answers, persisted-state restart, duplicate input, mismatched task completion,
follow-up replies, cross-channel references, and native-dialog rejection. Quoted
question context accompanies replies to stored bridge messages. These automated
integration checks do not substitute for the user-deferred manual Discord test.

The bridge does not read arbitrary tool output or reasoning to guess errors.
Agents must summarize relevant build/test/tool failures in their final response.
Missing runtime error details remain unknown; generic history records cannot
reconstruct a cause. Native dialogs may need direct desktop action. Discord
delivery itself cannot work while the PC/network/credentials are unavailable.
