from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import notifier
import configure


class NotifierTests(unittest.TestCase):
    def test_structured_complete_status(self) -> None:
        message = "Done.\n<!-- discord-status\nstate: complete\ntitle: Built it\nsummary: All checks passed.\nprogress: 100% (6/6)\ntests: 8 passed\n-->"
        status = notifier.parse_status(message, r"C:\work\demo")
        self.assertEqual(status.state, "complete")
        self.assertEqual(status.title, "Built it")
        self.assertEqual(status.summary, "All checks passed.")
        self.assertEqual(status.progress, "100% (6/6)")

    def test_unstructured_status_is_turn_complete(self) -> None:
        status = notifier.parse_status("A normal informational response.", r"C:\work\demo")
        self.assertEqual(status.state, "turn_complete")
        self.assertFalse(status.has_contract)

    def test_unknown_state_is_safe_fallback(self) -> None:
        status = notifier.parse_status("<!-- discord-status\nstate: exploding\n-->")
        self.assertEqual(status.state, "turn_complete")

    def test_mentions_are_restricted_to_configured_user(self) -> None:
        event = notifier.synthetic_event("blocked")
        env = {
            "DISCORD_CHANNEL_ID": "654321",
            "DISCORD_PING_ON": "blocked",
            "DISCORD_PING_USER_ID": "123456",
        }
        payload = notifier.make_payload(event, notifier.parse_status(event["last-assistant-message"]), env)
        self.assertEqual(payload["content"], "<@123456>")
        self.assertEqual(payload["allowed_mentions"], {"users": ["123456"]})

    def test_no_implicit_mentions(self) -> None:
        event = notifier.synthetic_event("complete")
        payload = notifier.make_payload(event, notifier.parse_status(event["last-assistant-message"]), {})
        self.assertEqual(payload["allowed_mentions"], {"parse": []})

    def test_dm_uses_user_id_to_resolve_channel(self) -> None:
        env = {"DISCORD_PING_USER_ID": "123456"}
        with patch.object(notifier, "discord_request", return_value={"id": "987654"}) as request:
            channel = notifier.resolve_destination_channel(env, "token", 15, 3)
        self.assertEqual(channel, "987654")
        request.assert_called_once_with(
            "POST", "/users/@me/channels", "token", {"recipient_id": "123456"}, 15, 3
        )

    def test_dm_payload_does_not_repeat_user_mention(self) -> None:
        event = notifier.synthetic_event("blocked")
        env = {"DISCORD_PING_ON": "blocked", "DISCORD_PING_USER_ID": "123456"}
        payload = notifier.make_payload(event, notifier.parse_status(event["last-assistant-message"]), env)
        self.assertEqual(payload["content"], "")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})

    def test_minimal_permission_invite_url(self) -> None:
        env = {"DISCORD_BOT_TOKEN": "secret"}
        with patch.object(notifier, "discord_request", return_value={"id": "123456"}) as request:
            url = notifier.bot_invite_url(env)
        self.assertEqual(
            url,
            "https://discord.com/oauth2/authorize?client_id=123456&scope=bot&permissions=0",
        )
        request.assert_called_once_with("GET", "/users/@me", "secret", None, 15, 3)

    def test_quiet_structured_turn_complete_when_disabled(self) -> None:
        event = notifier.synthetic_event("turn_complete")
        self.assertEqual(
            notifier.handle_event(event, {}, dry_run=True),
            "ignored structured turn_complete",
        )

    def test_footerless_turn_stays_quiet_when_turn_complete_enabled(self) -> None:
        event = notifier.synthetic_event("turn_complete")
        event["last-assistant-message"] = "A footerless background completion."
        self.assertEqual(
            notifier.handle_event(event, {"DISCORD_SEND_TURN_COMPLETE": "true"}, dry_run=True),
            "ignored unstructured turn",
        )

    def test_structured_turn_complete_sends_when_enabled(self) -> None:
        event = notifier.synthetic_event("turn_complete")
        rendered = notifier.handle_event(
            event,
            {"DISCORD_SEND_TURN_COMPLETE": "true"},
            dry_run=True,
        )
        payload = json.loads(rendered)
        self.assertEqual(payload["embeds"][0]["color"], 0x95A5A6)

    def test_dry_run_payload_is_valid_json(self) -> None:
        event = notifier.synthetic_event("complete")
        rendered = notifier.handle_event(event, {}, dry_run=True)
        payload = json.loads(rendered)
        self.assertEqual(payload["embeds"][0]["color"], 0x2ECC71)

    def test_dotenv_parsing_and_environment_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("DISCORD_CHANNEL_ID='123'\nDISCORD_BOT_TOKEN=file-token\n", encoding="utf-8")
            with patch.dict("os.environ", {"DISCORD_BOT_TOKEN": "environment-token"}, clear=False):
                values = notifier.load_env(path)
        self.assertEqual(values["DISCORD_CHANNEL_ID"], "123")
        self.assertEqual(values["DISCORD_BOT_TOKEN"], "environment-token")

    def test_windows_notify_command_remains_valid_toml(self) -> None:
        command = [r"C:\Users\example\Python\python.exe", r"C:\tools\notifier.py"]
        rendered = configure.replace_notify('notify = ["old"]\nmodel = "test"\n', command)
        import tomllib
        self.assertEqual(tomllib.loads(rendered)["notify"], command)

    def test_agent_contract_silences_running_goals_and_subagents(self) -> None:
        self.assertIn("Sub-agents must never", configure.AGENT_BLOCK)
        self.assertIn("If a goal remains running, emit no block at all", configure.AGENT_BLOCK)
        self.assertIn("at most one", configure.AGENT_BLOCK)

    def test_managed_agent_contract_is_upgradeable(self) -> None:
        old = (
            "before\n"
            f"{configure.MANAGED_START}\nold rules\n{configure.MANAGED_END}\n"
            "after\n"
        )
        updated = configure.upsert_managed_block(old)
        self.assertIn("before", updated)
        self.assertIn("after", updated)
        self.assertNotIn("old rules", updated)
        self.assertEqual(updated.count(configure.MANAGED_START), 1)


if __name__ == "__main__":
    unittest.main()
