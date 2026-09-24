"""Exercise two-task question -> answer -> final routing across stored state."""
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,patch

from attention_monitor import AttentionMonitor
from bridge_store import Store
from reply_bridge import Bridge

TASK_A='22222222-2222-4222-8222-222222222222'
TASK_B='33333333-3333-4333-8333-333333333333'


class QuestionRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/'bridge.sqlite'
        self.store=Store(self.path)
        self.bridge=Bridge({'DISCORD_PING_USER_ID':'1'},self.store)
        self.bridge._connection.user=NS(id=3)
        self.bridge.destination=NS(id=2,fetch_message=AsyncMock())

    async def asyncTearDown(self): self.tmp.cleanup()

    def deliver_question(self,tid,mid,text,kind='request_user_input_async'):
        route={'message_id':'seed-'+tid,'thread_id':tid,'channel_id':'2','cwd':'.'}
        monitor=AttentionMonitor(self.store)
        monitor.emit(route,{'type':'response_item','payload':{'type':'function_call',
            'name':kind,'call_id':'same-call-id-in-two-tasks',
            'arguments':json.dumps({'questions':[{'title':text,'options':['Yes','No']}]})}})
        output=next(o for o in self.store.outputs() if o['thread_id']==tid)
        self.store.output_sent(output,{'id':str(mid),'channel_id':'2','author':{'id':'3'}})
        return output

    def reply(self,mid,reference,text):
        return NS(id=mid,channel=NS(id=2),author=NS(id=1,bot=False),webhook_id=None,
            content=text,attachments=[],stickers=[],reference=NS(message_id=reference,channel_id=2))

    async def test_two_questions_reverse_order_restart_and_final_reply_chain(self):
        self.deliver_question(TASK_A,101,'Use blue for the car?')
        self.deliver_question(TASK_B,102,'Delete the temporary test scene?')
        # Reopen persisted routing. Titles/colors are not routing inputs.
        self.bridge.store=Store(self.path)
        await self.bridge.ingest(self.reply(201,102,'No'))
        await self.bridge.ingest(self.reply(202,101,'Yes'))
        await self.bridge.ingest(self.reply(202,101,'Yes'))
        rows=self.store.rows(('pending',))
        self.assertEqual(len(rows),2)
        by_id={r['message_id']:r for r in rows}
        self.assertEqual(by_id['201']['thread_id'],TASK_B)
        self.assertIn('Delete the temporary test scene?',by_id['201']['text'])
        self.assertNotIn('blue for the car',by_id['201']['text'])
        self.assertEqual(by_id['202']['thread_id'],TASK_A)
        self.assertIn('Use blue for the car?',by_id['202']['text'])
        with patch('reply_bridge.codex_transport.queue',return_value=('submitted','queued','queue-id')) as queue:
            await self.bridge.dispatch_pending()
        self.assertEqual({c.args[0]['thread_id'] for c in queue.call_args_list},{TASK_A,TASK_B})
        wrong={'thread-id':TASK_A,'turn-id':'turn-b','input-messages':['[Discord reply 201]'],
            'last-assistant-message':'Kept the scene.'}
        self.assertFalse(self.store.complete_event(wrong))
        wrong['thread-id']=TASK_B
        self.assertTrue(self.store.complete_event(wrong))
        final=next(o for o in self.store.outputs() if o['id']=='201:final')
        self.store.output_sent(final,{'id':'301','channel_id':'2','author':{'id':'3'}})
        await self.bridge.ingest(self.reply(401,301,'Thanks, continue'))
        pending=self.store.rows(('pending',))
        self.assertEqual(pending[0]['thread_id'],TASK_B)
        self.assertIn('Kept the scene.',pending[0]['text'])

    async def test_desktop_question_remains_desktop_after_restart(self):
        self.deliver_question(TASK_A,101,'Approve access?',kind='request_user_input')
        self.bridge.store=Store(self.path)
        await self.bridge.ingest(self.reply(201,101,'Yes'))
        self.assertEqual(self.store.rows(('pending',)),[])
        self.assertIn('native Codex dialog',self.store.outputs()[0]['payload'])

    async def test_route_policy_and_delivery_commit_atomically(self):
        row=dict(message_id='seed',thread_id=TASK_A,channel_id='2',cwd='.')
        self.store.output(row,'question',{'_reply_policy':'desktop'})
        output=self.store.outputs()[0]
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_delivery BEFORE UPDATE ON outbox BEGIN SELECT RAISE(ABORT,'simulated disk failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.output_sent(output,{'id':'101','channel_id':'2','author':{'id':'3'}})
        self.assertIsNone(self.store.route('101'))
        self.assertIsNone(self.store.get('reply_policy:101'))
        self.assertEqual(len(self.store.outputs()),1)

    async def test_cross_channel_reference_rejected(self):
        self.deliver_question(TASK_A,101,'Continue?')
        message=self.reply(201,101,'Yes'); message.reference.channel_id=999
        await self.bridge.ingest(message)
        self.assertEqual(self.store.rows(('pending',)),[])


if __name__=='__main__': unittest.main()
