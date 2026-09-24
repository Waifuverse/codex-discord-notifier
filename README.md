# Codex Discord Bridge

A personal Windows bridge between local Codex tasks and Discord. Receive task
outcomes and questions, reply to continue the original task, query usage and
recorded active jobs, and request screenshots or short clips.

This is an independent integration, not an official OpenAI or Discord product.
It is designed for one trusted owner on one Windows account, not a public
multi-user bot. Anyone controlling the owner's Discord account can send work to
Codex with that account's existing permissions.

## Requirements and compatibility

- Windows with PowerShell and Task Scheduler, Python 3.12, and Git.
- Codex installed and signed in under the same Windows user. `codex --version`
  and `codex queue --help` must work from PowerShell.
- A Discord account and a bot application you control.
- An awake, online PC. Background tasks start at **Windows sign-in**, not before
  login. Captures additionally need an accessible desktop.

Development was tested with Python 3.12 and Codex CLI 0.149.0. This bridge depends
on local Codex protocol/history details (`state_5.sqlite`,
`thread_history_1.sqlite`, `session_index.jsonl`, rollout events and `codex queue`).
Future Codex versions may require adapter changes. It only reads Codex storage;
it never modifies those databases. The entire flow has not been verified on a
fresh second PC. macOS and Linux are not supported by the startup/capture helpers.

## 1. Put the project in a permanent folder

Clone this repository, or download and extract its source, into a folder such as
`C:\tools\codex-discord-notifier`. Open PowerShell **in that folder**. Hook and
scheduled-task registration save absolute paths, so do not move it afterward.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
if (!(Test-Path -LiteralPath .env)) { Copy-Item -LiteralPath .env.example -Destination .env }
notepad .env
```

`.env.example` is a blank public template. `.env` is your private local copy and
is ignored by Git. The copy command preserves an existing setup. Never paste
credentials into an issue, screenshot, or chat.

## 2. Create and configure the Discord bot

Open the [Discord Developer Portal](https://discord.com/developers/applications),
create an application, and use its Bot settings to create/reset a bot token.
Put the token in `DISCORD_BOT_TOKEN` in `.env` (not the application ID or client
secret). Enable Developer Mode in Discord, copy your own user ID, and put it in
`DISCORD_PING_USER_ID`. Leave `DISCORD_DM_USER_ID` blank unless you intentionally
want that setting to override the owner/DM recipient.

For the default DM setup, leave `DISCORD_CHANNEL_ID` blank. Generate an invite:

```powershell
.\.venv\Scripts\python.exe .\notifier.py --invite-url
```

Open the printed URL and add the bot to a server you control. The bot and owner
need a shared server and the owner's privacy settings must allow its DMs. DM
mode does not need the privileged Message Content intent or server administrator
permissions.

For **server-channel mode**, set `DISCORD_CHANNEL_ID` to a private channel ID,
enable Message Content intent in the application's Bot settings, and grant the
bot View Channel, Send Messages, Embed Links, Attach Files, Read Message History,
and Add Reactions in that channel. Do not grant Administrator. Replies are
accepted only from the configured owner in the configured destination.

Other settings are commented in `.env.example`. Repository metadata sharing is
disabled in the template. Normal standalone turns are quiet by default.

## 3. Verify and install

First run the tests and a preview; these do not send a Discord test message:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
.\.venv\Scripts\python.exe .\notifier.py --dry-run complete
```

Then send a real test message to your configured destination:

```powershell
.\.venv\Scripts\python.exe .\notifier.py --test complete
```

Install the hook and background listener:

```powershell
.\.venv\Scripts\python.exe .\configure.py install
.\.venv\Scripts\python.exe .\configure.py status
.\manage_bridge.ps1 install
.\manage_bridge.ps1 status
```

`configure.py` updates your user-level Codex `config.toml` and `AGENTS.md`,
preserves an existing notification command, and points agents at the installed
[agent guide](AGENT_GUIDE.md). It stores restoration information in `.state/`.
Restart Codex to load its changed configuration.

`manage_bridge.ps1 install` registers two hidden tasks for this Windows account:
**Codex Discord Reply Bridge** and **Codex Discord Bridge Health**. Install only
one copy per user: these task names are shared. If Windows blocks the script,
review the downloaded source and your execution policy; organizational policies
may require your administrator. Do not disable machine-wide security policy.

## 4. Test an actual task round trip

1. Start a harmless Codex task, such as asking it to summarize a test file, and
   ask it to report completion using the installed status contract.
2. Wait for that task's notification in Discord. A synthetic setup test is not a
   real task destination.
3. Reply directly to the notification with a harmless follow-up. The bot reacts
   with a thinking emoji after queue acceptance; that means queued, not finished.
4. Confirm the input appears in the original Codex task and its answer returns
   to Discord. Repeat with a second task to check independent routing.
5. Send `!usage` and `!jobs` as standalone messages. Check startup again after
   your next sign-in. Test media only with screen contents you intend to share.

## Commands and behavior

| Discord input | Behavior |
| --- | --- |
| Reply to a task notification/question/answer | Queue text in that exact task |
| Message immediately after this bot's task message | Continue that task without using Discord Reply |
| `!usage` or `!u` | Remaining account allowance and reset times; Spark hidden |
| `!jobs` | Latest local saved in-progress tasks, with stale records separated |
| Reply with `!screenshot [monitor]` | Capture the selected monitor (default 1) |
| Reply with `!clip [seconds] [monitor]` | Silent clip, default 10 seconds, maximum 30 |
| Reply with `!screenshot window Unique title` | Capture only the matching window |
| Reply with `!clip window Unique title` | Ten-second isolated window clip |
| Reply with up to four image attachments | Pass validated local images to the task |

`!usage`, `!u`, and `!jobs` need no task reference and start no model turn. `!jobs` is a
saved-status snapshot, not a live heartbeat: running and waiting turns share an
in-progress state; unfinished records older than 24 hours are unconfirmed.
Reply to a task notification, not a global query result, to select a task.

Explicit replies always choose their referenced task. Without a reply, only the
immediately preceding message can select a task, and it must be this bot's mapped
task message. A preceding human message, other bot, or account/health response
does not choose a task. The original question context and desktop-only approval
rules are preserved, including after a bridge restart.

Task headers use the first 64 characters of the sidebar title. Routing uses
stored UUIDs, never titles or colors. Seven categorical colors are allocated
persistently; inactive colors can be recycled. Overflow is uncolored instead
of using near-identical shades. Red is reserved for errors.

Required conversational questions can be answered in Discord. Native approval,
credential, and blocking desktop dialogs still require action in Codex. A Discord
reply cannot approve those dialogs. Active tasks receive queued follow-ups as
subsequent turns, not guaranteed immediate steering. Uncertain dispatches are
not automatically retried, to avoid sending work twice.

Media uploads target 9 MiB, with smaller retries if Discord rejects them. Incoming
images support PNG, JPEG, WebP and GIF (first frame), limited to 20 MiB each,
40 MiB combined, and 40 megapixels. Local managed media expires after seven days
unless configured otherwise; pending work is protected. Window capture fails
instead of falling back to the desktop. Protected/fullscreen rendering and
locked desktops may not be capturable. Nothing records continuously.

## Maintenance and troubleshooting

```powershell
.\manage_bridge.ps1 status
.\.venv\Scripts\python.exe .\health_watchdog.py --once
.\manage_bridge.ps1 stop
.\manage_bridge.ps1 start
```

Stop/start affects the listener only; the independent health watchdog remains
active. Network startup failures retry automatically. Invalid credentials need
correction in `.env`. HTTP 403 usually requires checking channel access or DM
privacy settings. See [errors and statuses](docs/ERRORS_AND_STATUSES.md).

On Windows, long notification events can fail before the hook starts (error 206).
The listener independently reads completed-turn records for already mapped root
tasks and uses the same durable delivery queue as the hook. First activation
recovers just the latest completion per task from the last day; subsequent polls
recover each new completion. It does not replay prompts or forward ordinary
progress. Job status also uses rollout lifecycle events if the local history
projection has no turns for a task.

Private logs, routes, replies, media, and restoration files live under `.state/`.
Keep that directory when updating, and never publish it. Back it up privately if
needed. Alerts cannot arrive while the PC is offline, powered off, or unable to
contact Discord. The watchdog is local, not an external monitoring service.

To update, first remove both startup tasks with `manage_bridge.ps1 uninstall`,
update the source and dependencies, run tests, then run `configure.py install`
and `manage_bridge.ps1 install` again. Preserve `.env` and `.state/`. Restart
Codex if the hook configuration changed. Reinstall after moving the folder.

To uninstall both startup tasks and restore the previous Codex hook:

```powershell
.\manage_bridge.ps1 uninstall
.\.venv\Scripts\python.exe .\configure.py uninstall
```

Uninstall retains `.env` and `.state/`; securely remove them yourself if no longer
needed. Do not delete restoration data before uninstalling.

## Contributing and publishing

Run the test command above before submitting changes. Include a regression test
for routing, retries, persistence, or authorization changes. Use synthetic IDs
and mocked Discord calls; never contribute real messages or credentials.

Read [SECURITY.md](SECURITY.md) and the [public release checklist](docs/PUBLISHING.md)
before publishing. No license has been selected yet; public visibility alone does
not grant an open-source license to copy, modify, or redistribute this project.
