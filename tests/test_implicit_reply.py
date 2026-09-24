"""Implicit replies use only the immediately preceding authenticated bot message."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from bridge_store import Store
from reply_bridge import Bridge


class ImplicitReplyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/'bridge.sqlite')
        self.bridge=Bridge({'DISCORD_PING_USER_ID':'1'},self.store)
        self.bridge._connection.user=NS(id=3)
        self.previous=NS(id=90,channel=NS(id=2),author=NS(id=3),webhook_id=None,embeds=[])
        self.history_calls=[]
        async def history(**kwargs):
            self.history_calls.append(kwargs)
            if self.previous: yield self.previous
        self.bridge.destination=NS(id=2,history=history,fetch_message=AsyncMock())
        self.message=NS(id=100,channel=NS(id=2),author=NS(id=1,bot=False),webhook_id=None,
                        content='Continue',attachments=[],stickers=[],reference=None)
        row={'message_id':'seed','thread_id':'task-a','channel_id':'2','cwd':'.'}
        self.store.output(row,'final',{'embeds':[{'description':'Task A completed its check.'}]})
        self.store.output_sent(self.store.outputs()[0],
                               {'id':'90','channel_id':'2','author':{'id':'3'}})

    async def asyncTearDown(self):
        await self.bridge.close()
        self.tmp.cleanup()

    async def test_previous_bot_routes_once_with_context_after_store_reopen(self):
        # A newer delivered task must not steal a message sent before it.
        self.store.map_message({'id':'110','channel_id':'2','author':{'id':'3'}},
                               {'thread-id':'task-b','cwd':'.'})
        self.bridge.store=Store(self.store.path)
        await self.bridge.ingest(self.message)
        await self.bridge.ingest(self.message)
        rows=self.store.rows(('pending',))
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['thread_id'],'task-a')
        self.assertIn('Reply reference: Discord message 90',rows[0]['text'])
        self.assertIn('Task A completed its check.',rows[0]['text'])
        self.assertEqual(self.history_calls[0]['limit'],1)
        self.assertEqual(self.history_calls[0]['before'].id,100)
        self.assertFalse(self.history_calls[0]['oldest_first'])
        with patch('reply_bridge.codex_transport.queue',return_value=('submitted','queued','queue-id')) as queue:
            await self.bridge.dispatch_pending()
        self.assertEqual(queue.call_args.args[0]['thread_id'],'task-a')

    async def test_explicit_reply_wins_and_does_not_read_predecessor(self):
        self.store.map_message({'id':'80','channel_id':'2','author':{'id':'3'}},
                               {'thread-id':'task-b','cwd':'.'})
        self.message.reference=NS(message_id=80,channel_id=2)
        await self.bridge.ingest(self.message)
        self.assertEqual(self.store.rows(('pending',))[0]['thread_id'],'task-b')
        self.assertEqual(self.history_calls,[])

    async def test_previous_human_other_bot_or_webhook_never_routes(self):
        for author,webhook in [(1,None),(999,None),(3,123)]:
            with self.subTest(author=author,webhook=webhook):
                self.previous.author.id=author; self.previous.webhook_id=webhook
                self.assertIsNone(await self.bridge.route_for(self.message))

    async def test_unmapped_usage_or_health_message_does_not_select_older_task(self):
        self.previous.id=95
        await self.bridge.ingest(self.message)
        self.assertEqual(self.store.rows(('pending',)),[])
        self.assertIn('Reply to a Codex notification',self.store.outputs()[0]['payload'])
        self.bridge.destination.fetch_message.assert_not_called()

    async def test_native_dialog_policy_survives_implicit_routing(self):
        self.store.put('reply_policy:90','desktop')
        await self.bridge.ingest(self.message)
        self.assertEqual(self.store.rows(('pending',)),[])
        self.assertIn('native Codex dialog',self.store.outputs()[0]['payload'])

    async def test_claude_and_wrong_destination_routes_remain_rejected(self):
        for field,value in [('origin','claude'),('channel_id','999'),('bot_id','999')]:
            with self.subTest(field=field):
                with self.store.connect() as db:
                    db.execute('UPDATE routes SET '+field+'=? WHERE message_id=?',(value,'90'))
                route=await self.bridge.route_for(self.message)
                self.assertTrue(route is None or route['origin']=='claude')
                with self.store.connect() as db:
                    db.execute("UPDATE routes SET origin='codex',channel_id='2',bot_id='3' WHERE message_id='90'")

    async def test_history_failure_is_retryable_not_a_wrong_route(self):
        async def failed(**kwargs):
            raise OSError('network unavailable')
            yield
        self.bridge.destination.history=failed
        with self.assertRaises(OSError): await self.bridge.ingest(self.message)
        self.assertEqual(self.store.rows(('pending',)),[])
        self.assertEqual(self.store.outputs(),[])

    async def test_implicit_image_keeps_attachment_and_task_context(self):
        self.message.content=''; self.message.attachments=[NS()]
        with patch('reply_bridge.media.download_images',return_value=['C:/test.png']):
            await self.bridge.ingest(self.message)
        row=self.store.rows(('pending',))[0]
        self.assertEqual(row['thread_id'],'task-a')
        self.assertEqual(json.loads(row['images_json']),['C:/test.png'])
        self.assertIn('Task A completed',row['text'])

    async def test_unauthorized_input_never_reads_history(self):
        self.message.author.id=999
        await self.bridge.ingest(self.message)
        self.assertEqual(self.history_calls,[])
