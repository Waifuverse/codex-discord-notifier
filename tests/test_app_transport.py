import json
import subprocess
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from unittest.mock import AsyncMock

import app_transport as app
from completion_monitor import observe

TID = '22222222-2222-4222-8222-222222222222'


class AppTransportTests(unittest.TestCase):
    def test_send_targets_exact_task_once_after_read_validation(self):
        events = []
        with patch.object(app, 'candidates', return_value=['unrelated', 'matching']), \
             patch.object(app, 'inspect', side_effect=[False, True]), \
             patch.object(app, 'CACHE') as cache, \
             patch.object(app, 'rpc', return_value={'threadId': TID}) as rpc:
            app.worker({'threadId': TID, 'prompt': 'answer'}, events.append)
        self.assertEqual([e['state'] for e in events], ['sending', 'submitted'])
        rpc.assert_called_once_with('matching', 'send_message_to_thread', TID,
                                    {'threadId': TID, 'prompt': 'answer'})

    def test_uncertain_send_never_tries_another_pipe(self):
        events = []
        with patch.object(app, 'candidates', return_value=['first', 'second']), \
             patch.object(app, 'inspect', return_value=True), patch.object(app, 'CACHE'), \
             patch.object(app, 'rpc', side_effect=OSError('lost response')) as rpc:
            app.worker({'threadId': TID, 'prompt': 'answer'}, events.append)
        self.assertEqual(events[-1]['state'], 'uncertain')
        self.assertEqual(rpc.call_count, 1)

    def test_app_closed_is_retryable_without_writing(self):
        events = []
        with patch.object(app, 'candidates', return_value=[]), patch.object(app, 'rpc') as rpc:
            app.worker({'threadId': TID, 'prompt': 'answer'}, events.append)
        self.assertEqual(events, [{'state': 'pending'}])
        rpc.assert_not_called()

    def test_partial_frame_is_reassembled_and_size_is_bounded(self):
        import io
        class Fragmented(io.BytesIO):
            def read(self, n): return super().read(min(n, 2))
        self.assertEqual(app.read_exact(Fragmented(b'abcde'), 5), b'abcde')
        with self.assertRaises(OSError): app.read_exact(Fragmented(b'ab'), 3)

    def test_discovery_rejects_other_task_host_and_kind(self):
        for field, value in [('id', 'wrong'), ('hostId', 'remote'), ('kind', 'chatgpt')]:
            thread = {'id': TID, 'hostId': 'local', 'kind': 'codex', field: value}
            with patch.object(app, 'rpc', return_value={'thread': thread}):
                self.assertFalse(app.inspect('pipe', TID))

    def test_timeout_before_send_retries_but_after_send_is_uncertain(self):
        for output, expected in [(b'', 'pending'), (b'{"state":"sending"}\n', 'uncertain')]:
            with patch.object(app.subprocess, 'run', side_effect=subprocess.TimeoutExpired('app', 30, output=output)):
                self.assertEqual(app.deliver(TID, 'hello')[0], expected)

    def test_delivery_confirmation_and_stdin_preserve_text(self):
        prompt = 'answer $(whoami) `literal`\nnext line'
        output = json.dumps({'state': 'submitted', 'threadId': TID})
        with patch.object(app.subprocess, 'run', return_value=NS(stdout=output)) as run:
            self.assertEqual(app.deliver(TID, prompt)[0], 'submitted')
        self.assertEqual(json.loads(run.call_args.kwargs['input'])['prompt'], prompt)
        self.assertNotIn(prompt, run.call_args.args[0])
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_wrong_confirmation_is_never_retried(self):
        with patch.object(app.subprocess, 'run', return_value=NS(stdout='{"state":"submitted","threadId":"wrong"}')):
            self.assertEqual(app.deliver(TID, 'hello')[0], 'uncertain')

    def test_app_steering_completion_tracks_only_exact_envelope(self):
        record = {'type': 'event_msg', 'payload': {'type': 'item_completed', 'thread_id': TID,
            'item': {'type': 'FunctionCallOutput', 'namespace': 'codex_app',
                     'name': 'send_message_to_thread', 'output':
                     '<codex_delegation>\n<source_thread_id>' + TID + '</source_thread_id>\n'
                     '<input>[Discord reply 123]\nYes</input>\n</codex_delegation>'}}}
        state = {'turn': 'active', 'inputs': []}
        observe(state, record, {'id': TID, 'cwd': '.'})
        self.assertEqual(state['inputs'], ['123'])
        record['payload']['item']['namespace'] = 'unrelated_tool'
        state['inputs'] = []
        observe(state, record, {'id': TID, 'cwd': '.'})
        self.assertEqual(state['inputs'], [])


class WorkerWakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_during_iteration_is_not_lost_before_wait(self):
        import asyncio
        import time
        from reply_bridge import Bridge
        from unittest.mock import Mock
        wake = asyncio.Event()
        fake = NS(wake=wake, destination=object(), env={}, store=Mock(),
                  wait_until_ready=AsyncMock(), is_ready=lambda: True,
                  is_closed=Mock(side_effect=[False, True]), catch_up=AsyncMock(),
                  dispatch_pending=AsyncMock(), attention=Mock(), completions=Mock())
        fake.store.get.return_value = str(time.time())
        async def incoming_message(): wake.set()
        fake.flush = incoming_message
        async def wait_without_delay(awaitable, timeout):
            self.assertTrue(wake.is_set())
            return await awaitable
        with patch('reply_bridge.asyncio.wait_for', side_effect=wait_without_delay):
            await Bridge.run_worker(fake)


if __name__ == '__main__': unittest.main()
