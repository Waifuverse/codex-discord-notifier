import json
import tempfile
import sqlite3
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from attention_monitor import AttentionMonitor, classify
from bridge_store import Store


def question(name='request_user_input_async'):
    return {'type':'response_item','timestamp':datetime.fromtimestamp(time.time()+1,timezone.utc).isoformat(),
            'payload':{'type':'function_call','name':name,'call_id':'call-test',
                       'arguments':json.dumps({'questions':[{'title':'Which color?', 'options':['Blue','Green']}]})}}


class AttentionTests(unittest.TestCase):
    def test_goal_limits_and_pauses_are_transitions_not_progress_spam(self):
        def event(status,stamp):
            return {'type':'event_msg','payload':{'type':'thread_goal_updated','goal':{
                'status':status,'createdAt':10,'updatedAt':stamp,'tokensUsed':1250,
                'tokenBudget':1200,'objective':'private task detail'}}}
        self.monitor.emit(self.route,event('active',11))
        self.assertEqual(self.store.outputs(),[])
        self.monitor.emit(self.route,event('budgetLimited',12))
        self.monitor.emit(self.route,event('budgetLimited',13))
        self.assertEqual(len(self.store.outputs()),1)
        payload=json.loads(self.store.outputs()[0]['payload'])
        self.assertIn('1,250 / 1,200',payload['embeds'][0]['description'])
        self.assertNotIn('private task detail',json.dumps(payload))
        self.assertEqual(payload['_reply_policy'],'desktop')
        self.monitor.emit(self.route,event('active',14))
        self.monitor.emit(self.route,event('budgetLimited',15))
        self.assertEqual(len(self.store.outputs()),2)
        for status in ('complete','blocked','active'):
            self.monitor.emit(self.route,event(status,16))
        self.assertEqual(len(self.store.outputs()),2)

    def test_usage_limited_goal_does_not_guess_a_reset_time(self):
        result=classify({'type':'event_msg','payload':{'type':'thread_goal_updated','goal':{'status':'usageLimited'}}})
        self.assertIn('does not supply a reset time',result[1])

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.store=Store(self.root/'state.sqlite')
        self.monitor=AttentionMonitor(self.store)
        self.route={'message_id':'123','channel_id':'456','thread_id':'task-1','cwd':'.'}

    def tearDown(self):
        self.tmp.cleanup()

    def test_async_question_is_answerable_and_includes_choices(self):
        kind,text,policy=classify(question())
        self.assertEqual((kind,policy),('question','queue'))
        self.assertIn('1) Blue',text)
        self.assertIn('2) Green',text)

    def test_native_question_is_not_misrepresented_as_remote_answerable(self):
        kind,text,policy=classify(question('request_user_input'))
        self.assertEqual(policy,'desktop')
        self.assertIn('native Codex dialog',text)

    def test_never_forward_reasoning_or_arbitrary_tool_arguments(self):
        for record in [{'type':'response_item','payload':{'type':'reasoning','text':'private'}},
                       {'type':'response_item','payload':{'type':'function_call','name':'exec_command','arguments':'secret'}}]:
            self.assertIsNone(classify(record))

    def test_failure_does_not_expose_raw_error_details(self):
        result=classify({'type':'event_msg','payload':{'type':'error','message':'token=secret'}})
        self.assertEqual(result[0],'error')
        self.assertNotIn('secret',result[1])

    def test_confidential_async_input_requires_codex(self):
        record=question()
        record['payload']['arguments']=json.dumps({'questions':[{'title':'Credential?', 'is_secret':True}]})
        self.assertEqual(classify(record)[2],'desktop')

    def test_restart_cursor_and_partial_line(self):
        path=self.root/'rollout.jsonl'
        raw=json.dumps(question()).encode()
        path.write_bytes(raw[:40])
        self.monitor.scan(self.route,path)
        self.assertEqual(self.store.outputs(),[])
        path.write_bytes(raw+b'\n')
        self.monitor.scan(self.route,path)
        self.assertEqual(len(self.store.outputs()),1)
        AttentionMonitor(Store(self.store.path)).scan(self.route,path)
        self.assertEqual(len(self.store.outputs()),1)

    def test_historical_questions_are_not_replayed(self):
        record=question(); record['timestamp']='2020-01-01T00:00:00Z'
        path=self.root/'rollout.jsonl'; path.write_text(json.dumps(record)+'\n')
        self.monitor.scan(self.route,path)
        self.assertEqual(self.store.outputs(),[])

    def test_terminal_failure_without_hook_is_reported_once(self):
        db=sqlite3.connect(self.root/'thread_history_1.sqlite')
        db.execute('CREATE TABLE thread_turns (thread_id TEXT,turn_id TEXT,status TEXT,completed_at REAL)')
        db.execute('INSERT INTO thread_turns VALUES (?,?,?,?)',('task-1','failed-turn','failed',time.time()))
        db.commit(); db.close()
        monitor=AttentionMonitor(self.store,self.root)
        monitor.failed_turns({'task-1':self.route})
        monitor.failed_turns({'task-1':self.route})
        self.assertEqual(len(self.store.outputs()),1)
        self.assertIn('Codex encountered an error',self.store.outputs()[0]['payload'])

    def test_desktop_alert_reply_policy_survives_delivery(self):
        self.monitor.emit(self.route,question('request_user_input'))
        output=self.store.outputs()[0]
        self.store.output_sent(output,{'id':'789','channel_id':'456','author':{'id':'999'}})
        self.assertEqual(self.store.get('reply_policy:789'),'desktop')

    def test_question_and_failure_final_statuses(self):
        for state,title in [('needs_input','Codex has a question'),('blocked','Codex is blocked'),('failed','Codex encountered an error')]:
            mid=str(len(self.store.rows(('completed',)))+1)
            self.store.enqueue(mid,'456',{'thread_id':'task-1','cwd':'.'},'test')
            self.store.update(mid,'submitted')
            self.store.complete_event({'thread-id':'task-1','turn-id':mid,'input-messages':['[Discord reply '+mid+']'],
                'last-assistant-message':'What next?\n<!-- discord-status\nstate: '+state+'\n-->'})
            payload=json.loads([r for r in self.store.outputs() if r['message_id']==mid][0]['payload'])
            self.assertEqual(payload['embeds'][0]['title'],title)


if __name__=='__main__':
    unittest.main()
