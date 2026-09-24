import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from attention_monitor import AttentionMonitor,classify
from bridge_store import Store
from error_reporting import summarize


class ErrorReportingTests(unittest.TestCase):
    def test_all_installed_protocol_error_variants_have_specific_messages(self):
        # codex 0.149.0 app-server generate-json-schema --experimental
        codes=('contextWindowExceeded','sessionBudgetExceeded','usageLimitExceeded',
            'serverOverloaded','cyberPolicy','misalignmentPolicyViolation','internalServerError',
            'unauthorized','badRequest','threadRollbackFailed','sandboxError',
            'httpConnectionFailed','responseStreamConnectionFailed','responseStreamDisconnected',
            'responseTooManyFailedAttempts','activeTurnNotSteerable')
        for code in codes:
            with self.subTest(code=code):
                self.assertNotEqual(summarize({'codexErrorInfo':code})[0],'Codex encountered an error')

    def test_http_and_busy_details_are_allowlisted(self):
        text=summarize({'codexErrorInfo':{'httpConnectionFailed':{'httpStatusCode':429,'secret':'private'}}})[1]
        self.assertIn('429',text); self.assertIn('does not identify',text); self.assertNotIn('private',text)
        text=summarize({'codexErrorInfo':{'activeTurnNotSteerable':{'turnKind':'compact'}}})[1]
        self.assertIn('context compaction',text)

    def test_usage_limit_preserves_reported_time_without_inventing_timezone(self):
        title,text=summarize({'error':{'codexErrorInfo':'usageLimitExceeded',
            'message':"You've hit your usage limit. try again at Sep 12th, 2026 11:05 PM. token=secret"}})
        self.assertEqual(title,'Codex usage limit reached')
        self.assertIn('Sep 12th, 2026 11:05 PM',text)
        self.assertIn('timezone not specified',text)
        self.assertNotIn('secret',text)

    def test_limits_are_distinct(self):
        titles=[summarize({'codexErrorInfo':code})[0] for code in
            ('usageLimitExceeded','contextWindowExceeded','rateLimitExceeded','responseStreamLimitExceeded')]
        self.assertEqual(len(set(titles)),4)
        self.assertIn('Not supplied',summarize({'codexErrorInfo':'usageLimitExceeded'})[1])

    def test_capacity_and_structured_connection_error(self):
        self.assertIn('capacity',summarize({'codexErrorInfo':'serverOverloaded'})[0])
        self.assertIn('connection',summarize({'codexErrorInfo':{'responseStreamConnectionFailed':{'httpStatusCode':502}}})[0])

    def test_unknown_error_does_not_leak_or_guess(self):
        title,text=summarize({'message':'token=secret C:/private.txt tokens gone','additionalDetails':'password'})
        self.assertEqual(title,'Codex encountered an error')
        for value in ('secret','private.txt','password'): self.assertNotIn(value,text)

    def test_only_recognized_service_message_fallback(self):
        self.assertEqual(summarize({'message':'Selected model is at capacity. Please try a different model.'})[0],
            'Selected model is at capacity')

    def test_retrying_error_not_marked_terminal(self):
        self.assertIsNone(classify({'type':'event_msg','payload':{'type':'error','willRetry':True}}))

    def test_real_history_shape_enriches_pending_fallback_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); store=Store(root/'bridge.sqlite')
            monitor=AttentionMonitor(store,root)
            route=dict(message_id='123',channel_id='456',thread_id='task',cwd='.')
            monitor.emit(route,{'type':'event_msg','payload':{'type':'turn_failed','turn_id':'turn'}})
            with sqlite3.connect(root/'thread_history_1.sqlite') as db:
                db.execute('CREATE TABLE thread_turns (thread_id TEXT,turn_id TEXT,status TEXT,completed_at REAL,error_json TEXT)')
                db.execute('INSERT INTO thread_turns VALUES (?,?,?,?,?)',('task','turn','failed',time.time(),
                    json.dumps({'codexErrorInfo':'usageLimitExceeded','message':"You've hit your usage limit."})))
            db.close()
            monitor.failed_turns({'task':route}); monitor.failed_turns({'task':route})
            outputs=store.outputs(); self.assertEqual(len(outputs),1)
            self.assertEqual(json.loads(outputs[0]['payload'])['embeds'][0]['title'],'Codex usage limit reached')
            store.output_sent(outputs[0],{'id':'789','channel_id':'456','author':{'id':'999'}})
            monitor.failed_turns({'task':route})
            self.assertEqual(store.outputs(),[])


if __name__=='__main__': unittest.main()
