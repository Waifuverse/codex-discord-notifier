import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from bridge_store import Store
import codex_transport
from reply_bridge import Bridge, authorized

TID = '22222222-2222-4222-8222-222222222222'
ROUTE = {'thread_id': TID, 'cwd': 'C:\\work'}


class StoreTests(unittest.TestCase):
    def test_multiple_answers_in_one_turn_emit_once_even_with_concurrent_hooks(self):
        from concurrent.futures import ThreadPoolExecutor
        for mid in ('100','101'):
            self.store.enqueue(mid,'200',ROUTE,'answer')
            self.store.update(mid,'submitted')
        event={'thread-id':TID,'turn-id':'shared-turn','input-messages':[
            '[Discord reply 100] yes','[Discord reply 101] also this'],
            'last-assistant-message':'Handled both answers.'}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:Store(self.path).complete_event(event),range(2)))
        self.assertEqual(results,[True,True])
        self.assertEqual(len(self.store.outputs()),1)
        self.assertEqual(self.store.outputs()[0]['message_id'],'101')
        self.assertEqual(len(self.store.rows(('completed',))),2)
        # A replay with only an earlier input must not produce a second output.
        event['input-messages']=['[Discord reply 100] yes']
        self.assertTrue(Store(self.path).complete_event(event))
        self.assertEqual(len(self.store.outputs()),1)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'bridge.sqlite'
        self.store = Store(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_restart_preserves_input_and_deduplicates(self):
        self.assertTrue(self.store.enqueue('100', '200', ROUTE, 'hello'))
        reopened = Store(self.path)
        self.assertFalse(reopened.enqueue('100', '200', ROUTE, 'duplicate'))
        self.assertEqual(reopened.rows(('pending',))[0]['text'], 'hello')

    def test_only_exact_thread_and_marker_complete(self):
        self.store.enqueue('100', '200', ROUTE, 'hello')
        self.store.update('100', 'submitted')
        event = {'thread-id': TID, 'turn-id': 'new', 'input-messages': ['unrelated'],
                 'last-assistant-message': 'done'}
        self.assertFalse(self.store.complete_event(event))
        event['input-messages'] = ['[Discord reply 100]\nhello']
        event['origin'] = 'claude'
        self.assertFalse(self.store.complete_event(event))
        event['origin'] = 'codex'
        event['thread-id'] = 'another-task'
        self.assertFalse(self.store.complete_event(event))
        event['thread-id'] = TID
        self.assertTrue(self.store.complete_event(event))
        self.assertTrue(self.store.complete_event(event))
        self.assertEqual(len(self.store.outputs()), 1)
        self.assertEqual(json.loads(self.store.outputs()[0]['payload'])['embeds'][0]['description'], 'done')
        # Codex includes earlier user inputs in subsequent notification events.
        # An old Discord marker must not consume an unrelated later completion.
        event['turn-id'] = 'later-desktop-turn'
        self.assertFalse(self.store.complete_event(event))
        self.assertEqual(self.store.rows(('completed',))[0]['final_turn'], 'new')

    def test_routes_persist_origin_and_bot_identity(self):
        self.store.map_message({'id':'100','channel_id':'200','author':{'id':'300'}},
                               {'thread-id':TID,'origin':'claude'})
        route = Store(self.path).route('100')
        self.assertEqual(route['origin'], 'claude')
        self.assertEqual(route['bot_id'], '300')

    def test_outbox_is_idempotent_and_remains_replyable(self):
        self.store.enqueue('100', '200', ROUTE, 'hello')
        row = self.store.rows(('pending',))[0]
        self.store.output(row, 'submitted', {'content':'queued'})
        self.store.output(row, 'submitted', {'content':'queued'})
        self.assertEqual(len(self.store.outputs()), 1)
        self.store.output_sent(self.store.outputs()[0],
                               {'id':'101','channel_id':'200','author':{'id':'300'}})
        self.assertEqual(self.store.outputs(), [])
        self.assertEqual(self.store.route('101')['thread_id'], TID)


class TransportTests(unittest.TestCase):
    def test_argument_array_preserves_shell_metacharacters(self):
        text = 'hello $(whoami) `test` ; & "quotes"\nnext line'
        row = dict(ROUTE, message_id='100', text=text)
        with patch.object(codex_transport, 'lookup_thread', return_value={'cwd':'.','archived':0}), \
             patch('app_transport.deliver', return_value=('submitted','sent',None)) as deliver:
            state, _, _ = codex_transport.dispatch(row)
        self.assertEqual(state, 'submitted')
        self.assertEqual(deliver.call_args.args[0], TID)
        self.assertTrue(deliver.call_args.args[1].startswith('[Discord reply 100]\n'+text+'\n\n'))

    def test_missing_task_does_not_send(self):
        with patch.object(codex_transport, 'lookup_thread', return_value=None), \
             patch('app_transport.deliver') as deliver:
            self.assertEqual(codex_transport.dispatch(dict(ROUTE,message_id='100',text='hello'))[0], 'failed')
        deliver.assert_not_called()


class ListenerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name)/'bridge.sqlite')
        self.bridge = Bridge({'DISCORD_PING_USER_ID':'1'},self.store)
        self.bridge._connection.user = NS(id=3)
        self.bridge.destination = NS(id=2,fetch_message=AsyncMock())
        self.message = NS(id=100,channel=NS(id=2),author=NS(id=1,bot=False),webhook_id=None,
                          content='hello',attachments=[],stickers=[],reference=NS(message_id=90,channel_id=2))
        self.store.map_message({'id':'90','channel_id':'2','author':{'id':'3'}},
                               {'thread-id':TID,'cwd':'.'})

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_authorization_and_bot_loops(self):
        for author,bot,webhook,channel in [(9,False,None,2),(1,True,None,2),(1,False,55,2),(1,False,None,9)]:
            self.message.author=NS(id=author,bot=bot)
            self.message.webhook_id=webhook
            self.message.channel=NS(id=channel)
            self.assertFalse(authorized(self.message,1,2))
            await self.bridge.ingest(self.message)
        self.assertEqual(self.store.rows(('pending',)), [])
        self.assertEqual(self.store.outputs(), [])

    async def test_gateway_resume_clears_disconnected_status_without_ready(self):
        self.store.put('connected','true')
        await self.bridge.on_disconnect()
        self.assertEqual(self.store.get('connected'),'false')
        disconnected=self.store.get('disconnected_since')
        self.assertGreater(float(disconnected),0)
        await self.bridge.on_disconnect()
        self.assertEqual(self.store.get('disconnected_since'),disconnected)
        await self.bridge.on_resumed()
        self.assertEqual(self.store.get('connected'),'true')
        self.assertEqual(self.store.get('disconnected_since'),'0')
        self.assertTrue(self.bridge.wake.is_set())

    async def test_reply_enqueues_exact_task_once(self):
        await self.bridge.ingest(self.message)
        await self.bridge.ingest(self.message)
        rows = self.store.rows(('pending',))
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['thread_id'],TID)

    async def test_claude_reply_rejected(self):
        with self.store.connect() as db:
            db.execute("UPDATE routes SET origin='claude'")
        await self.bridge.ingest(self.message)
        self.assertEqual(self.store.rows(('pending',)), [])
        self.assertIn('Claude',self.store.outputs()[0]['payload'])

    async def test_native_dialog_answer_is_not_queued_as_approval(self):
        self.store.put('reply_policy:90','desktop')
        self.message.content='approve'
        await self.bridge.ingest(self.message)
        self.assertEqual(self.store.rows(('pending',)), [])
        self.assertEqual(json.loads(self.store.outputs()[0]['payload'])['_reply_policy'],'desktop')

    async def test_attachments_are_not_silently_dropped(self):
        from media_support import MediaError
        self.message.attachments=[NS()]
        with patch('reply_bridge.media.download_images',side_effect=MediaError('Unsupported image')):
            await self.bridge.ingest(self.message)
        self.assertEqual(self.store.rows(('pending',)), [])
        self.assertIn('Unsupported image',self.store.outputs()[0]['payload'])

    async def test_image_only_reply_is_queued_with_image_paths(self):
        self.message.content=''
        self.message.attachments=[NS()]
        with patch('reply_bridge.media.download_images',return_value=['C:/image.png']):
            await self.bridge.ingest(self.message)
        row=self.store.rows(('pending',))[0]
        self.assertEqual(json.loads(row['images_json']),['C:/image.png'])
        self.assertIn('inspect the attached',row['text'])

    async def test_capture_command_does_not_invoke_codex(self):
        self.message.content='!clip 5 2'
        await self.bridge.ingest(self.message)
        await self.bridge.ingest(self.message)
        self.assertEqual(self.store.rows(('pending',)),[])
        jobs=self.store.media_jobs('pending')
        self.assertEqual(len(jobs),1)
        self.assertEqual(json.loads(jobs[0]['params']),{'kind':'clip','seconds':5,'monitor':2})

    async def test_no_reply_without_previous_message_does_not_guess_task(self):
        self.message.reference=None
        async def history(**kwargs):
            if False: yield None
        self.bridge.destination.history=history
        await self.bridge.ingest(self.message)
        self.assertEqual(self.store.rows(('pending',)), [])
        self.assertIn('Reply to a Codex notification',self.store.outputs()[0]['payload'])

    async def test_cursor_does_not_advance_past_failed_ingest(self):
        async def history(**kwargs):
            yield self.message
        self.bridge.destination.history=history
        self.store.put('cursor:2','99')
        self.bridge.ingest=AsyncMock(side_effect=RuntimeError('storage unavailable'))
        with self.assertRaises(RuntimeError):
            await self.bridge.catch_up()
        self.assertEqual(self.store.get('cursor:2'),'99')

    async def test_dispatch_restart_does_not_resend_uncertain(self):
        self.store.enqueue('100','2',ROUTE,'hello')
        self.store.update('100','uncertain')
        with patch.object(codex_transport,'dispatch') as queue:
            await self.bridge.dispatch_pending()
        queue.assert_not_called()

    async def test_gateway_dispatch_method_is_not_overridden(self):
        import discord
        self.assertIs(Bridge.dispatch,discord.Client.dispatch)

    async def test_usage_without_reference_is_deduplicated_and_never_queued(self):
        self.message.content='!usage'; self.message.reference=None
        with patch('usage_status.read_usage',return_value={'rateLimits':{'primary':{'usedPercent':25}}}) as read:
            await self.bridge.ingest(self.message)
            await self.bridge.ingest(self.message)
        read.assert_called_once()
        self.assertEqual(self.store.rows(('pending',)),[])
        self.assertEqual(len(self.store.outputs()),1)
        self.assertIn('75% remaining',self.store.outputs()[0]['payload'])
        self.assertEqual(self.store.outputs()[0]['thread_id'],'')

    async def test_usage_shortcut_is_case_insensitive_and_uses_same_command(self):
        self.message.content=' !U '; self.message.reference=None
        self.bridge.route_for=AsyncMock(side_effect=AssertionError('must not route usage'))
        with patch('usage_status.read_usage',return_value={'rateLimits':{'primary':{'usedPercent':25}}}) as read:
            await self.bridge.ingest(self.message)
            self.message.content='!usage'
            await self.bridge.ingest(self.message)
        read.assert_called_once()
        self.assertEqual(self.store.rows(('pending',)),[])
        self.assertEqual(self.store.outputs()[0]['id'],'100:usage')
        self.assertIn('75% remaining',self.store.outputs()[0]['payload'])

    async def test_usage_shortcut_unauthorized_does_not_read_account(self):
        self.message.content='!u'; self.message.author.id=999
        with patch('usage_status.read_usage') as read:
            await self.bridge.ingest(self.message)
        read.assert_not_called()

    async def test_usage_unauthorized_does_not_read_account(self):
        self.message.content='!usage'; self.message.author.id=999
        with patch('usage_status.read_usage') as read:
            await self.bridge.ingest(self.message)
        read.assert_not_called()

    async def test_usage_lookup_failure_reports_error_not_allowance(self):
        from usage_status import UsageError
        self.message.content='!usage'; self.message.reference=None
        with patch('usage_status.read_usage',side_effect=UsageError('Usage lookup timed out. Try !usage again shortly.')):
            await self.bridge.ingest(self.message)
        payload=json.loads(self.store.outputs()[0]['payload'])
        self.assertEqual(payload['embeds'][0]['color'],0xE74C3C)
        self.assertIn('timed out',payload['embeds'][0]['description'])
        self.assertNotIn('% remaining',payload['embeds'][0]['description'])


if __name__ == '__main__':
    unittest.main()
