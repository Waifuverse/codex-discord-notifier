#!/usr/bin/env python3
"""Claude Code Stop hook -> notifier.py bridge.

Claude Code invokes Stop hooks with a JSON object on stdin carrying
`session_id`, `transcript_path` (a JSONL conversation log) and `cwd`. Codex
instead passes notifier.py a single JSON argument with the last assistant
message inline. This bridge reads the Claude hook payload, extracts the final
assistant message text from the transcript, and forwards a Codex-shaped event
to notifier.py, so both agents share one delivery path, one status contract
(AGENTS.md's <!-- discord-status --> block) and one dedup store.

Stdlib only, like the notifier itself. Always exits 0: a broken notification
must never block Claude's turn.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NOTIFIER = ROOT / "notifier.py"


def last_assistant_message(transcript_path: str) -> tuple[str, str]:
    """Return (text, message_uuid) of the last assistant text in the JSONL."""
    text, uuid = "", ""
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") != "assistant":
                    continue
                message = row.get("message") or {}
                parts = []
                for block in message.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(str(block.get("text") or ""))
                if parts:
                    text = "\n".join(parts)
                    uuid = str(row.get("uuid") or "")
    except OSError:
        pass
    return text, uuid


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    transcript = str(payload.get("transcript_path") or "")
    text, uuid = last_assistant_message(transcript)
    if not text:
        return 0
    event = {
        "origin": "claude",
        "type": "agent-turn-complete",
        "thread-id": str(payload.get("session_id") or ""),
        "turn-id": uuid,
        "cwd": str(payload.get("cwd") or ""),
        "last-assistant-message": text,
    }
    try:
        subprocess.run(
            [sys.executable, str(NOTIFIER), json.dumps(event)],
            timeout=60,
            check=False,
            capture_output=True,
        )
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
