#!/usr/bin/env python3
"""Codex agent-turn-complete -> Discord bot notification bridge.

The runtime intentionally uses only Python's standard library. Codex passes one
JSON argument to this program. An existing notify command can be preserved in
.state/original-notify.json and receives the original argument first.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import mimetypes
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from thread_colors import thread_color
from thread_presentation import task_title


ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / ".state"
FORWARD_FILE = STATE_DIR / "original-notify.json"
DEDUP_FILE = STATE_DIR / "sent-turns.json"
LOG_FILE = STATE_DIR / "notifier.log"
VALID_STATES = {"complete", "blocked", "needs_input", "failed", "turn_complete"}
STATUS_RE = re.compile(r"<!--\s*discord-status\s*(.*?)-->", re.IGNORECASE | re.DOTALL)

STATE_STYLE = {
    "complete": ("Goal complete", 0x2ECC71),
    "blocked": ("Goal blocked", 0xF39C12),
    "needs_input": ("Input needed", 0x3498DB),
    "failed": ("Goal failed", 0xE74C3C),
    "turn_complete": ("Turn complete", 0x95A5A6),
}


@dataclass(frozen=True)
class Status:
    state: str
    title: str
    summary: str
    progress: str = ""
    tests: str = ""
    has_contract: bool = False


def log(message: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 512_000:
            backup = LOG_FILE.with_suffix(".log.1")
            backup.unlink(missing_ok=True)
            LOG_FILE.replace(backup)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except OSError:
        pass


def load_env(path: Path = ROOT / ".env") -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
    for key, value in os.environ.items():
        if key.startswith("DISCORD_"):
            values[key] = value
    return values


def bool_value(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def clean_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").replace("\x00", "").split())
    # Prevent accidental mention-like text from being visually misleading.
    text = text.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def parse_status(message: str, cwd: str = "") -> Status:
    match = STATUS_RE.search(message or "")
    fields: dict[str, str] = {}
    if match:
        for raw_line in match.group(1).splitlines():
            if ":" not in raw_line:
                continue
            key, value = raw_line.split(":", 1)
            fields[key.strip().lower()] = value.strip()

    state = fields.get("state", "turn_complete").lower()
    if state not in VALID_STATES:
        state = "turn_complete"
    repo = Path(cwd).name if cwd else "Codex"
    visible = STATUS_RE.sub("", message or "").strip()
    default_title = f"{STATE_STYLE[state][0]} — {repo}"
    return Status(
        state=state,
        title=clean_text(fields.get("title") or default_title, 256),
        summary=clean_text(fields.get("summary") or visible or "Codex finished a turn.", 2000),
        progress=clean_text(fields.get("progress", ""), 1024),
        tests=clean_text(fields.get("tests", ""), 1024),
        has_contract=match is not None,
    )


def git_metadata(cwd: str) -> str:
    if not cwd or not Path(cwd).is_dir():
        return ""
    try:
        branch = subprocess.run(
            ["git", "branch", "--show-current"], cwd=cwd, capture_output=True,
            text=True, timeout=3, check=True,
        ).stdout.strip()
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=cwd, capture_output=True,
            text=True, timeout=3, check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=cwd, capture_output=True,
            text=True, timeout=3, check=True,
        ).stdout.strip()
        return clean_text(f"{branch or '(detached)'} · {commit} · {'dirty' if dirty else 'clean'}", 1024)
    except (OSError, subprocess.SubprocessError):
        return ""


def make_payload(event: dict[str, Any], status: Status, env: dict[str, str]) -> dict[str, Any]:
    label, color = STATE_STYLE[status.state]
    fields: list[dict[str, Any]] = []
    if status.progress:
        fields.append({"name": "Progress", "value": status.progress, "inline": True})
    if status.tests:
        fields.append({"name": "Verification", "value": status.tests, "inline": True})
    cwd = str(event.get("cwd") or "")
    if bool_value(env.get("DISCORD_INCLUDE_GIT"), True):
        git = git_metadata(cwd)
        if git:
            fields.append({"name": "Git", "value": git, "inline": False})

    thread_id = clean_text(event.get("thread-id", "unknown"), 80)
    turn_id = clean_text(event.get("turn-id", "unknown"), 80)
    embed = {
        "title": task_title(event.get('thread-id')),
        "description": status.summary,
        "color": color if status.state == 'failed' else thread_color(event.get('thread-id'), color),
        "fields": fields,
        "footer": {"text": label + (' · ' + status.title if status.title != label else '')},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    ping_states = {item.strip() for item in env.get("DISCORD_PING_ON", "").split(",") if item.strip()}
    user_id = env.get("DISCORD_PING_USER_ID", "").strip()
    content = ""
    allowed: dict[str, Any] = {"parse": []}
    # A DM already targets the user and does not need an in-message mention.
    is_dm = not env.get("DISCORD_CHANNEL_ID", "").strip()
    if not is_dm and status.state in ping_states and user_id.isdigit():
        content = f"<@{user_id}>"
        allowed = {"users": [user_id]}
    return {"content": content, "embeds": [embed], "allowed_mentions": allowed}


def event_key(event: dict[str, Any]) -> str:
    return f"{event.get('thread-id', '')}:{event.get('turn-id', '')}"


def already_sent(key: str) -> bool:
    if not key or key == ":" or not DEDUP_FILE.exists():
        return False
    try:
        return key in json.loads(DEDUP_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False


def mark_sent(key: str) -> None:
    if not key or key == ":":
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    entries: list[str] = []
    if DEDUP_FILE.exists():
        try:
            entries = list(json.loads(DEDUP_FILE.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError):
            entries = []
    entries = (entries + [key])[-500:]
    fd, temp_name = tempfile.mkstemp(prefix="sent-", suffix=".json", dir=STATE_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(entries, handle)
        os.replace(temp_name, DEDUP_FILE)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def forward_existing(raw_json: str) -> None:
    if not FORWARD_FILE.exists():
        return
    try:
        command = json.loads(FORWARD_FILE.read_text(encoding="utf-8"))
        if not isinstance(command, list) or not command:
            return
        subprocess.run([str(part) for part in command] + [raw_json], timeout=15, check=False)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        log(f"existing notifier failed: {type(exc).__name__}: {exc}")


class DiscordAPIError(RuntimeError):
    def __init__(self, status, detail):
        super().__init__(f'Discord HTTP {status}: {detail}')
        self.status = status
        try:
            self.code = json.loads(detail).get('code')
        except (ValueError, AttributeError):
            self.code = None


def discord_request(
    method: str,
    path: str,
    token: str,
    body_value: dict[str, Any] | None,
    timeout: int,
    retries: int,
    file_path: str | None = None,
) -> dict[str, Any]:
    body = json.dumps(body_value, ensure_ascii=False).encode("utf-8") if body_value is not None else None
    content_type = 'application/json; charset=utf-8'
    if file_path:
        file = Path(file_path)
        if file.stat().st_size > 9 * 1024 * 1024:
            raise ValueError('Media exceeds the conservative 9 MiB upload target')
        boundary = 'CodexMedia' + uuid.uuid4().hex
        filename = file.name
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', filename):
            raise ValueError('Unsafe upload filename')
        content_type = 'multipart/form-data; boundary=' + boundary
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="payload_json"\r\n'
                'Content-Type: application/json\r\n\r\n').encode() + (body or b'{}') + (
                f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="files[0]"; filename="{filename}"\r\n'
                f'Content-Type: {mimetypes.guess_type(filename)[0] or "application/octet-stream"}\r\n\r\n').encode() + file.read_bytes() + f'\r\n--{boundary}--\r\n'.encode()
    url = f"https://discord.com/api/v10{path}"
    clean_token = token[4:].strip() if token.lower().startswith("bot ") else token
    for attempt in range(retries + 1):
        request = urllib.request.Request(
            url, data=body, method=method,
            headers={
                "Authorization": f"Bot {clean_token}",
                "Content-Type": content_type,
                "User-Agent": "CodexDiscordNotifier/1.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_body = response.read().decode("utf-8", errors="replace")
                if response.status not in (200, 201):
                    raise RuntimeError(f"Discord returned HTTP {response.status}")
                return json.loads(response_body) if response_body else {}
        except urllib.error.HTTPError as exc:
            response_text = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code == 429 and attempt < retries:
                try:
                    delay = float(json.loads(response_text).get("retry_after", 1))
                except (ValueError, json.JSONDecodeError, AttributeError):
                    delay = 1
                time.sleep(min(max(delay, 0.25), 10))
                continue
            if 500 <= exc.code < 600 and attempt < retries:
                time.sleep(min(2 ** attempt, 8))
                continue
            raise DiscordAPIError(exc.code, response_text) from exc
        except urllib.error.URLError as exc:
            if attempt < retries:
                time.sleep(min(2 ** attempt, 8))
                continue
            raise RuntimeError(f"Discord connection failed: {exc.reason}") from exc
    raise RuntimeError("Discord request exhausted retries")


def resolve_destination_channel(env: dict[str, str], token: str, timeout: int, retries: int) -> str:
    channel = env.get("DISCORD_CHANNEL_ID", "").strip()
    if channel:
        if not channel.isdigit():
            raise ValueError("DISCORD_CHANNEL_ID must contain digits only")
        return channel
    recipient = env.get("DISCORD_DM_USER_ID", "").strip() or env.get("DISCORD_PING_USER_ID", "").strip()
    if not recipient or not recipient.isdigit():
        raise ValueError("Set DISCORD_PING_USER_ID to your numeric Discord user ID for DM delivery")
    dm = discord_request("POST", "/users/@me/channels", token, {"recipient_id": recipient}, timeout, retries)
    dm_channel = str(dm.get("id") or "")
    if not dm_channel.isdigit():
        raise RuntimeError("Discord did not return a valid DM channel ID")
    return dm_channel


def post_discord(payload: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    token = env.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("DISCORD_BOT_TOKEN must be set in .env")
    timeout = max(3, min(60, int(env.get("DISCORD_TIMEOUT_SECONDS", "15"))))
    retries = max(0, min(5, int(env.get("DISCORD_MAX_RETRIES", "3"))))
    channel = resolve_destination_channel(env, token, timeout, retries)
    return discord_request("POST", f"/channels/{channel}/messages", token, payload, timeout, retries)


def synthetic_event(state: str) -> dict[str, Any]:
    return {
        "type": "agent-turn-complete",
        "thread-id": "setup-test",
        "turn-id": str(int(time.time())),
        "cwd": str(ROOT),
        "input-messages": ["Send a setup test"],
        "last-assistant-message": (
            "Notifier setup test.\n\n<!-- discord-status\n"
            f"state: {state}\n"
            "title: Codex Discord notifier is connected\n"
            "summary: The bot token and channel permissions are working.\n"
            "progress: 100% (live delivery verified)\n"
            "tests: setup message accepted by Discord\n-->"
        ),
    }


def bot_invite_url(env: dict[str, str]) -> str:
    token = env.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("DISCORD_BOT_TOKEN must be set in .env")
    timeout = max(3, min(60, int(env.get("DISCORD_TIMEOUT_SECONDS", "15"))))
    retries = max(0, min(5, int(env.get("DISCORD_MAX_RETRIES", "3"))))
    bot = discord_request("GET", "/users/@me", token, None, timeout, retries)
    client_id = str(bot.get("id") or "")
    if not client_id.isdigit():
        raise RuntimeError("Discord did not return the bot application ID")
    # No guild permissions are requested; mutual membership is enough for DMs.
    return f"https://discord.com/oauth2/authorize?client_id={client_id}&scope=bot&permissions=0"


def handle_event(event: dict[str, Any], env: dict[str, str], dry_run: bool = False) -> str:
    if event.get("type") != "agent-turn-complete":
        return "ignored unsupported event"
    if not dry_run:
        from bridge_store import Store
        store = Store()
        if store.complete_event(event):
            return "queued Discord reply completion"
    status = parse_status(str(event.get("last-assistant-message") or ""), str(event.get("cwd") or ""))
    if not status.has_contract:
        return "ignored unstructured turn"
    if status.state == "turn_complete" and not bool_value(env.get("DISCORD_SEND_TURN_COMPLETE"), False):
        return "ignored structured turn_complete"
    key = event_key(event)
    if not dry_run and already_sent(key):
        return "ignored duplicate turn"
    payload = make_payload(event, status, env)
    if dry_run:
        return json.dumps(payload, indent=2, ensure_ascii=False)
    if store.queue_notification(event, payload) is True:
        return "queued structured notification"
    # Concurrent hook invocations must share Discord's server-side dedup key.
    payload.update(nonce=hashlib.sha256(('notify:'+key).encode()).hexdigest()[:24],enforce_nonce=True)
    message = post_discord(payload, env)
    store.map_message(message, event)
    mark_sent(key)
    return f"sent {status.state} notification"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event_json", nargs="?", help="Codex notification JSON")
    parser.add_argument("--test", choices=sorted(VALID_STATES), help="send a live synthetic notification")
    parser.add_argument("--dry-run", choices=sorted(VALID_STATES), help="print a synthetic Discord payload")
    parser.add_argument("--invite-url", action="store_true", help="print a minimal-permission bot invite URL")
    args = parser.parse_args(argv)
    env = load_env()

    if args.invite_url:
        try:
            print(bot_invite_url(env))
            return 0
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    if args.test or args.dry_run:
        try:
            result = handle_event(synthetic_event(args.test or args.dry_run), env, dry_run=bool(args.dry_run))
            print(result)
            return 0
        except Exception as exc:  # CLI should provide an actionable setup error.
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    if not args.event_json:
        parser.error("event_json is required unless --test or --dry-run is used")
    raw_json = args.event_json
    forward_existing(raw_json)
    try:
        event = json.loads(raw_json)
        result = handle_event(event, env)
        log(result)
        return 0
    except Exception as exc:
        # Notification failures must never make the Codex turn itself fail.
        log(f"Discord notification failed: {type(exc).__name__}: {exc}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
