import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import notifier
from bridge_store import Store
from completion_monitor import CompletionMonitor
from jobs_status import read_jobs
from rollout_status import latest_status, bootstrap_offset
import configure


class CompletionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root/'bridge.sqlite')
        self.store.put('channel_id', '2'); self.store.put('bot_id', '3')
        self.env = {'DISCORD_SEND_TURN_COMPLETE': 'true', 'DISCORD_INCLUDE_GIT': 'false'}
        self.monitor = CompletionMonitor(self.store, self.env, self.root)
        self.thread = {'id': 'task', 'cwd': '.'}
        self.path = self.root/'rollout.jsonl'
        self.now = 2000000
        self.body = 'Finished.\n<!-- discord-status\nstate: turn_complete\nsummary: Done\n-->'
        self.sent = patch.object(notifier, 'already_sent', return_value=False)
        self.sent.start(); self.addCleanup(self.sent.stop)

    def record(self, typ, **kwargs):
        return {'timestamp': datetime.fromtimestamp(self.now, timezone.utc).isoformat(),
                'type': 'event_msg', 'payload': {'type': typ, **kwargs}}

    def append(self, *records):
        with self.path.open('a', encoding='utf8') as f:
            for record in records: f.write(json.dumps(record)+'\n')

    def completion(self, turn):
        return self.record('task_complete', turn_id=turn, last_agent_message=self.body)

    def scan(self):
        self.monitor.scan(self.thread, self.path, self.now)

    def test_bootstrap_recovers_latest_only_then_each_new_turn_once(self):
        self.append(self.completion('older'), self.completion('latest'))
        self.scan(); self.scan()
        self.assertEqual([r['id'] for r in self.store.outputs()], ['notification:task:latest'])
        self.append(self.completion('next'), self.completion('another'))
        CompletionMonitor(Store(self.store.path), self.env).scan(self.thread, self.path, self.now)
        self.assertEqual(len(self.store.outputs()), 3)

    def test_long_input_recovers_footerless_reply_with_original_route(self):
        self.store.enqueue('100', '2', {'thread_id': 'task', 'cwd': '.'}, 'question')
        self.store.update('100', 'submitted')
        self.append(self.record('task_started', turn_id='t'),
                    self.record('user_message', message='[Discord reply 100]\n'+'x'*100000))
        self.scan()  # Persist the active turn through a bridge restart.
        self.body = 'Actual final answer without footer'
        self.append(self.completion('t'))
        CompletionMonitor(Store(self.store.path), self.env).scan(self.thread, self.path, self.now)
        self.assertEqual(self.store.outputs()[0]['message_id'], '100')
        self.assertEqual(self.store.rows(('completed',))[0]['final_turn'], 't')

    def test_response_item_input_and_quoted_marker(self):
        self.store.enqueue('100', '2', {'thread_id': 'task', 'cwd': '.'}, 'question')
        self.store.update('100', 'submitted')
        self.append(self.record('task_started', turn_id='t'),
                    {'timestamp': self.record('x')['timestamp'], 'type': 'response_item',
                     'payload': {'type': 'message', 'role': 'user', 'content':
                                 [{'type': 'input_text', 'text': '[Discord reply 100] hi\n[Discord reply 200]'}]}},
                    self.completion('t'))
        self.scan()
        self.assertEqual(len(self.store.rows(('completed',))), 1)

    def test_hook_and_fallback_race_share_one_outbox_claim(self):
        event = {'type': 'agent-turn-complete', 'thread-id': 'task', 'turn-id': 't',
                 'last-assistant-message': self.body}
        with patch('bridge_store.Store', return_value=self.store), patch.object(notifier, 'post_discord') as send:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(notifier.handle_event, event, self.env),
                           pool.submit(self.monitor.deliver, event)]
                for future in futures: future.result()
        send.assert_not_called()
        self.assertEqual(len(self.store.outputs()), 1)

    def test_legacy_delivery_and_unstructured_desktop_are_not_resent(self):
        self.store.map_message({'id': '90', 'channel_id': '2', 'author': {'id': '3'}},
                               {'thread-id': 'task', 'turn-id': 't'})
        self.append(self.completion('t')); self.scan()
        self.body = 'Ordinary desktop completion without status'
        self.append(self.completion('other')); self.scan()
        self.assertEqual(self.store.outputs(), [])

    def test_partial_line_and_failed_enqueue_retry_without_advancing(self):
        data = json.dumps(self.completion('t'))
        self.path.write_text(data)
        self.scan(); self.assertEqual(self.store.outputs(), [])
        with self.path.open('a') as f: f.write('\n')
        with patch.object(self.monitor, 'deliver', side_effect=OSError('unavailable')):
            with self.assertRaises(OSError): self.scan()
        self.scan(); self.assertEqual(len(self.store.outputs()), 1)

    def test_old_completions_and_quiet_setting(self):
        old = self.completion('old'); old['timestamp'] = '1970-01-01T00:00:00+00:00'
        self.append(old); self.scan()
        self.monitor.env = {}
        self.append(self.completion('quiet')); self.scan()
        self.assertEqual(self.store.outputs(), [])

    def test_bad_record_does_not_block_later_completion(self):
        bad = self.completion('bad'); bad['timestamp'] = {}
        self.append(bad, self.completion('good'))
        self.scan()
        self.assertEqual(self.store.outputs()[0]['id'], 'notification:task:good')

    def test_poll_excludes_subagents_and_unmapped_threads(self):
        self.append(self.completion('t'))
        with sqlite3.connect(self.root/'state_5.sqlite') as db:
            db.execute('CREATE TABLE threads(id,cwd,rollout_path,archived,source,updated_at)')
            db.executemany('INSERT INTO threads VALUES (?,?,?,?,?,?)',
                          [('child', '.', str(self.path), 0, 'subAgent', self.now),
                           ('unmapped', '.', str(self.path), 0, 'vscode', self.now)])
        db.close()
        self.store.map_message({'id': '90', 'channel_id': '2', 'author': {'id': '3'}},
                               {'thread-id': 'child'})
        with patch('completion_monitor.time.time', return_value=self.now): self.monitor.poll()
        self.assertEqual(self.store.outputs(), [])

    def test_jobs_falls_back_when_projection_has_no_turn(self):
        self.append(self.record('task_started', turn_id='t'))
        # A long running turn with many non-lifecycle records crosses reverse chunks.
        self.append(self.record('agent_message', message='x'*300000))
        with sqlite3.connect(self.root/'state_5.sqlite') as db:
            db.execute('CREATE TABLE threads(id,updated_at,archived,source,rollout_path)')
            db.execute('INSERT INTO threads VALUES (?,?,?,?,?)', ('task', self.now, 0, 'vscode', str(self.path)))
        db.close()
        with sqlite3.connect(self.root/'thread_history_1.sqlite') as db:
            db.execute('CREATE TABLE thread_turns(thread_id,status,started_at,completed_at,rollout_ordinal)')
        db.close()
        self.assertEqual(read_jobs(self.root, self.now)[0]['id'], 'task')
        self.append(self.completion('t'))
        self.assertEqual(latest_status(self.path)[0], 'completed')
        self.assertEqual(read_jobs(self.root, self.now), [])

    def test_status_recognizes_wrapper_without_altering_config(self):
        wrapper = ['codex-computer-use.exe', 'turn-ended', '--previous-notify',
                   json.dumps(['python.exe', str(configure.NOTIFIER)])]
        with patch('configure.Path.is_file', return_value=True):
            self.assertTrue(configure.contains_notifier(wrapper))
            self.assertFalse(configure.contains_notifier(['unrelated.exe', '--previous-notify', wrapper[-1]]))

    def test_bootstrap_offset_keeps_previous_completion_and_current_reply_context(self):
        self.append(self.record('task_started', turn_id='ancient'),
                    self.record('agent_message', message='x'*300000), self.completion('ancient'))
        expected = self.path.stat().st_size
        self.append(self.record('task_started', turn_id='finished'), self.completion('finished'),
                    self.record('task_started', turn_id='active'),
                    self.record('user_message', message='[Discord reply 123] question'))
        self.assertEqual(bootstrap_offset(self.path), expected)
        self.scan()
        self.assertEqual(self.store.outputs()[0]['id'], 'notification:task:finished')
