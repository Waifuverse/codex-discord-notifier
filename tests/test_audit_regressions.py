import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock,patch
import unittest
from datetime import datetime,timezone

from bridge_store import Store,input_reply_ids
from attention_monitor import AttentionMonitor
from reply_bridge import Bridge

TID='22222222-2222-4222-8222-222222222222'


class AuditTests(unittest.TestCase):
    def test_quoted_reply_marker_is_not_an_input(self):
        self.assertEqual(input_reply_ids(['[Discord reply 100] Yes\nQuoted: [Discord reply 200]',
            'Here is an example: [Discord reply 300]',{'role':'assistant','content':'[Discord reply 400]'}]),['100'])

    def test_completion_cannot_be_overwritten_by_late_dispatch_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'db'); route={'thread_id':TID,'cwd':'.'}
            store.enqueue('100','2',route,'yes'); store.update('100','dispatching')
            store.complete_event({'thread-id':TID,'turn-id':'turn','input-messages':['[Discord reply 100] yes'],
                'last-assistant-message':'Done'})
            self.assertFalse(store.finish_dispatch('100','uncertain',None,1,1000))
            self.assertEqual(len(store.rows(('completed',))),1)
            self.assertEqual(store.rows(('uncertain',)),[])

    def test_malformed_question_does_not_block_following_valid_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); store=Store(root/'db'); monitor=AttentionMonitor(store)
            route={'thread_id':TID,'cwd':'.','message_id':'1','channel_id':'2'}
            stamp=datetime.fromtimestamp(time.time()+1,timezone.utc).isoformat()
            records=[[],{'payload':[],'type':'event_msg','timestamp':stamp}]
            for i,args in enumerate(('[]','{"questions":[null]}','{"questions":[{"title":"Continue?"}]}')):
                records.append({'timestamp':stamp,'type':'response_item','payload':{'type':'function_call',
                    'name':'request_user_input_async','call_id':str(i),'arguments':args}})
            path=root/'rollout'; path.write_text('\n'.join(json.dumps(r) for r in records)+'\n')
            monitor.scan(route,path)
            self.assertEqual(len(store.outputs()),1)
            self.assertIn('Continue?',store.outputs()[0]['payload'])


class DeliveryRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_after_rename_keeps_original_presentation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'db')
            row={'thread_id':TID,'cwd':'.','message_id':'1','channel_id':'2'}
            store.output(row,'final',{'embeds':[{'title':'Codex replied','description':'Done','color':123}]})
            bridge=Bridge({'DISCORD_PING_USER_ID':'1','DISCORD_BOT_TOKEN':'test'},store)
            bridge._connection.user=NS(id=3)
            async def history(**kwargs):
                if False: yield None
            bridge.destination=NS(id=2,history=history)
            with patch('thread_presentation.task_title',return_value='Original sidebar title'), \
                 patch('reply_bridge.notifier.discord_request',side_effect=OSError('lost response')):
                with self.assertRaises(OSError): await bridge.flush_output(store.outputs()[0])
            with patch('thread_presentation.task_title',return_value='Renamed sidebar title'), \
                 patch('reply_bridge.notifier.discord_request',return_value={'id':'9','channel_id':'2','author':{'id':'3'}}) as send:
                await bridge.flush_output(store.outputs()[0])
            self.assertEqual(send.call_args.args[3]['embeds'][0]['title'],'Original sidebar title')
            self.assertNotIn('_delivery_presentation',send.call_args.args[3])


if __name__=='__main__': unittest.main()
