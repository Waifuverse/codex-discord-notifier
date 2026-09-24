# Security and privacy

This bot gives its configured Discord owner a route to the local Codex account.
Treat that Discord account and bot token as privileged credentials. Use a private
channel or DMs, protect both accounts, and keep Codex's approval boundaries.

`.env`, `.state/`, local media, databases, logs, environments, and private history
must never be published. The public `.env.example` contains no credentials.
Git ignore rules protect untracked files; they cannot remove a secret already
committed or prevent an explicit `git add -f`.

Notifications can contain task answers, questions, titles, and optionally Git
metadata. Captures contain actual screen pixels. Those outputs go to Discord;
only request content you intend to share. Queued messages and attachments are
also retained locally under `.state/`.

If a token is exposed, reset it in Discord Developer Portal immediately, replace
it in your private `.env`, and restart the listener and watchdog. Removing the
file from the latest commit is insufficient: inspect repository history, forks,
artifacts and logs. Never submit the compromised token in a public issue.

Report security issues using [GitHub private vulnerability reporting](https://github.com/Waifuverse/codex-discord-notifier/security/advisories/new).
If that channel is unavailable, contact the maintainer privately before
disclosing exploitable details. Never include credentials in a report.
