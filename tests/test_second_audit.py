import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock,patch
import unittest
import discord
from datetime import datetime,timezone

from bridge_store import Store
from health_watchdog import health
from attention_monitor import AttentionMonitor
from reply_bridge import Bridge
import notifier


class SecondAuditTests(unittest.TestCase):
    def test_watcher_outage_alerts_even_with_healthy_listener(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'db'); now=time.time()
            for key in ('heartbeat','worker_success'): store.put(key,now)
            store.put('connected','true'); store.put('attention_error_since',now-301)
            self.assertIn('question/error watcher',health(store,now)[0])
            store.put('attention_error_since',0)
            self.assertEqual(health(store,now),[])

    def test_hook_retries_use_same_server_dedup_key(self):
        event=notifier.synthetic_event('complete')
        store=Mock(); store.complete_event.return_value=False
        with patch('bridge_store.Store',return_value=store),patch.object(notifier,'already_sent',return_value=False), \
             patch.object(notifier,'mark_sent'),patch.object(notifier,'post_discord',return_value={}) as send:
            notifier.handle_event(event,{'DISCORD_INCLUDE_GIT':'false'})
            notifier.handle_event(event,{'DISCORD_INCLUDE_GIT':'false'})
        a,b=[c.args[0] for c in send.call_args_list]
        self.assertEqual(a['nonce'],b['nonce']); self.assertTrue(a['enforce_nonce'])

    def test_invalid_timestamp_and_goal_status_do_not_poison_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); store=Store(root/'db'); monitor=AttentionMonitor(store)
            stamp=datetime.fromtimestamp(time.time()+1,timezone.utc).isoformat()
            records=[{'timestamp':{},'type':'event_msg','payload':{'type':'turn_failed'}},
                     {'timestamp':stamp,'type':'event_msg','payload':{'type':'thread_goal_updated','goal':{'status':[]}}},
                     {'timestamp':stamp,'type':'event_msg','payload':{'type':'turn_failed','turn_id':'valid'}}]
            path=root/'rollout'; path.write_text('\n'.join(json.dumps(r) for r in records)+'\n')
            monitor.scan({'thread_id':'task','message_id':'1','channel_id':'2','cwd':'.'},path)
            self.assertEqual(len(store.outputs()),1)


class LongOutageTests(unittest.IsolatedAsyncioTestCase):
    async def test_already_sent_message_beyond_100_is_not_sent_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'db')
            row={'thread_id':'','message_id':'1','channel_id':'2','cwd':'.'}
            store.output(row,'usage',{'embeds':[{'title':'Usage','description':'Existing answer','color':123}]})
            output=store.outputs()[0]
            stamp=datetime.fromtimestamp(int(output['created']*1000)/1000,timezone.utc)
            matched=NS(id=999,author=NS(id=3),channel=NS(id=2),nonce=None,
                reference=NS(message_id=1),embeds=[discord.Embed(title='Usage',description='Existing answer',color=123,timestamp=stamp)])
            async def history(**kwargs):
                self.assertIsNone(kwargs['limit']); self.assertIn('after',kwargs)
                for i in range(150):
                    yield NS(id=i,author=NS(id=4),reference=None,embeds=[],nonce=None)
                yield matched
            bridge=Bridge({'DISCORD_PING_USER_ID':'1','DISCORD_BOT_TOKEN':'test'},store)
            bridge._connection.user=NS(id=3); bridge.destination=NS(id=2,history=history)
            with patch('reply_bridge.notifier.discord_request') as send:
                await bridge.flush_output(output)
            send.assert_not_called()
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT sent_id FROM outbox').fetchone()[0],'999')


if __name__=='__main__': unittest.main()
