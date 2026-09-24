import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock,patch
import aiohttp
import discord
from bridge_store import Store
from reply_bridge import run_listener
from health_watchdog import health


class StartupTests(unittest.TestCase):
    def test_initial_network_failures_retry_with_fresh_clients(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'bridge.sqlite')
            clients=[Mock(),Mock(),Mock()]
            clients[0].run.side_effect=aiohttp.ClientConnectionError('DNS unavailable')
            clients[1].run.side_effect=TimeoutError()
            sleep=Mock()
            with patch('reply_bridge.Bridge',side_effect=clients) as factory:
                self.assertEqual(run_listener({'DISCORD_BOT_TOKEN':'test'},store,sleep),0)
            self.assertEqual(factory.call_count,3)
            self.assertEqual([c.args[0] for c in sleep.call_args_list],[5,10])

    def test_invalid_credentials_do_not_retry_as_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'bridge.sqlite'); client=Mock()
            client.run.side_effect=discord.LoginFailure('invalid')
            sleep=Mock()
            with patch('reply_bridge.Bridge',return_value=client):
                self.assertEqual(run_listener({'DISCORD_BOT_TOKEN':'test'},store,sleep),1)
            sleep.assert_not_called()
            self.assertEqual(store.get('startup_error'),'authentication')
            self.assertIn('rejected the bot login',health(store)[0])

    def test_stale_network_heartbeat_reports_cause_and_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'bridge.sqlite'); store.put('startup_error','network')
            issues=health(store)
            self.assertEqual(len(issues),1)
            self.assertIn('network/DNS/timeout',issues[0])
            self.assertIn('automatically retries',issues[0])


if __name__=='__main__': unittest.main()
