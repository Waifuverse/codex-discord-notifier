#!/usr/bin/env python3
"""Install, inspect, or uninstall the user-level Codex notify integration."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / ".state"
FORWARD_FILE = STATE_DIR / "original-notify.json"
INSTALL_FILE = STATE_DIR / "install.json"
NOTIFIER = ROOT / "notifier.py"
MANAGED_START = "<!-- codex-discord-notifier:start -->"
MANAGED_END = "<!-- codex-discord-notifier:end -->"
AGENT_BLOCK = f"""{MANAGED_START}
# Discord goal-status notifications

Only the primary/root agent may emit a Discord status block. Sub-agents must never
append one, invoke the notifier, or notify the user directly; they report results
only to their parent. The primary waits for delegated work and emits at most one
aggregated notification for the goal.

While an explicit goal is active and still running, omit the status block from
intermediate turns, progress reports, automatic continuations, and sub-agent
completions. Do not turn ordinary progress into `needs_input`. Emit a block only
when the primary reaches one of these user-relevant outcomes:

- `complete`: the whole goal is genuinely achieved and verified.
- `blocked`: the goal has reached the product's genuine blocked state.
- `needs_input`: progress cannot continue without a specific user answer or credential.
- `failed`: an unrecovered terminal failure prevents the goal from continuing.
- `turn_complete`: only for a standalone, non-goal response; these are quiet by default.

When eligible, append exactly one machine-readable block after the user-facing
answer. Keep it concise and do not include secrets or source code:

<!-- discord-status
state: complete|blocked|needs_input|failed|turn_complete
title: short outcome title
summary: one-sentence outcome or blocker
progress: percentage against the task's stated definition of 100%, when applicable
tests: concise verification result, when applicable
-->

If a goal remains running, emit no block at all. A user asking for status does not
make the goal terminal. Never send one notification per sub-agent or per turn.
# Discord bridge agent guide

When handling Discord replies or sending authorized media, first read
[the installed agent guide]({(ROOT / 'AGENT_GUIDE.md').as_posix()}).
Use its exact task-routing and delivery-verification instructions.
Only the primary/root agent sends notifications or media.
{MANAGED_END}
"""


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def desired_command() -> list[str]:
    return [str(Path(sys.executable).resolve()), str(NOTIFIER)]


def read_config(path: Path) -> tuple[str, dict]:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    data = tomllib.loads(text) if text.strip() else {}
    return text, data


def toml_array(values: list[str]) -> str:
    # JSON string escaping is valid for TOML basic strings.
    return "[" + ", ".join(json.dumps(item) for item in values) + "]"


def replace_notify(text: str, command: list[str]) -> str:
    pattern = re.compile(r"(?m)^notify\s*=\s*\[[^\r\n]*\]\s*$")
    replacement = f"notify = {toml_array(command)}"
    if pattern.search(text):
        # A replacement string treats backslashes specially; a callback preserves
        # the doubled Windows-path escapes emitted by toml_array().
        return pattern.sub(lambda _match: replacement, text, count=1)
    prefix = replacement + "\n"
    return prefix + text


def remove_managed_block(text: str) -> str:
    pattern = re.compile(re.escape(MANAGED_START) + r".*?" + re.escape(MANAGED_END) + r"\s*", re.DOTALL)
    return pattern.sub("", text)


def upsert_managed_block(text: str) -> str:
    pattern = re.compile(re.escape(MANAGED_START) + r".*?" + re.escape(MANAGED_END), re.DOTALL)
    if pattern.search(text):
        return pattern.sub(lambda _match: AGENT_BLOCK.rstrip(), text, count=1)
    separator = "\n" if text and not text.endswith("\n") else ""
    return text + separator + AGENT_BLOCK


def install() -> None:
    home = codex_home()
    home.mkdir(parents=True, exist_ok=True)
    config_path = home / "config.toml"
    agents_path = home / "AGENTS.md"
    text, data = read_config(config_path)
    current = data.get("notify")
    desired = desired_command()
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if current != desired:
        if isinstance(current, list) and current:
            FORWARD_FILE.write_text(json.dumps(current, indent=2), encoding="utf-8")
        elif not FORWARD_FILE.exists():
            FORWARD_FILE.write_text("[]\n", encoding="utf-8")
        config_path.write_text(replace_notify(text, desired), encoding="utf-8")

    agents_text = agents_path.read_text(encoding="utf-8") if agents_path.exists() else ""
    updated_agents = upsert_managed_block(agents_text)
    if updated_agents != agents_text:
        agents_path.write_text(updated_agents, encoding="utf-8")

    INSTALL_FILE.write_text(json.dumps({
        "config": str(config_path),
        "agents": str(agents_path),
        "command": desired,
    }, indent=2), encoding="utf-8")
    print(f"Installed Codex notify bridge in {config_path}")
    print("Existing toast notifier preserved: " + ("yes" if json.loads(FORWARD_FILE.read_text()) else "none found"))


def uninstall() -> None:
    home = codex_home()
    config_path = home / "config.toml"
    agents_path = home / "AGENTS.md"
    text, data = read_config(config_path)
    if data.get("notify") == desired_command():
        original = json.loads(FORWARD_FILE.read_text(encoding="utf-8")) if FORWARD_FILE.exists() else []
        if original:
            config_path.write_text(replace_notify(text, original), encoding="utf-8")
        else:
            config_path.write_text(re.sub(r"(?m)^notify\s*=\s*\[[^\r\n]*\]\s*\n?", "", text, count=1), encoding="utf-8")
    if agents_path.exists():
        agents_path.write_text(remove_managed_block(agents_path.read_text(encoding="utf-8")), encoding="utf-8")
    print("Uninstalled Codex Discord notifier and restored the previous notify command.")


def status() -> int:
    home = codex_home()
    config_path = home / "config.toml"
    _, data = read_config(config_path)
    installed = contains_notifier(data.get("notify"))
    env_path = ROOT / ".env"
    configured = False
    if env_path.exists():
        env_text = env_path.read_text(encoding="utf-8-sig")
        token = re.search(r"(?m)^DISCORD_BOT_TOKEN=(.+)$", env_text)
        channel = re.search(r"(?m)^DISCORD_CHANNEL_ID=(\d+)$", env_text)
        dm_user = re.search(r"(?m)^DISCORD_PING_USER_ID=(\d+)$", env_text)
        configured = bool(token and token.group(1).strip() and (channel or dm_user))
    print(f"Hook installed: {'yes' if installed else 'no'}")
    print(f"Discord credentials configured: {'yes' if configured else 'no'}")
    print(f"Existing notifier chained: {'yes' if FORWARD_FILE.exists() and json.loads(FORWARD_FILE.read_text()) else 'no'}")
    return 0 if installed else 1


def contains_notifier(command, depth=0):
    """Recognize the installed computer-use wrapper without replacing its chain."""
    if depth > 4 or not isinstance(command, list) or not command:
        return False
    if not all(isinstance(part, str) for part in command):
        return False
    if len(command) > 1 and os.path.normcase(command[1]) == os.path.normcase(str(NOTIFIER)):
        return Path(command[0]).is_file()
    if Path(command[0]).name != 'codex-computer-use.exe' or not Path(command[0]).is_file():
        return False
    try:
        index = command.index('--previous-notify')
        return contains_notifier(json.loads(command[index + 1]), depth + 1)
    except (ValueError, IndexError, TypeError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "status", "uninstall"))
    args = parser.parse_args()
    if args.action == "install":
        install()
        return 0
    if args.action == "uninstall":
        uninstall()
        return 0
    return status()


if __name__ == "__main__":
    raise SystemExit(main())
