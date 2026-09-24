"""Recover completed mapped root tasks when Windows cannot launch the notify hook.

Read Codex rollouts only. Never replay prompts, run models, or infer completion
from silence. Bootstrap considers just the latest completed turn per task in the
last day; ongoing polling preserves every new completion. Delivery shares the
hook's transactional claim and retrying outbox.
"""
import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

import notifier
from bridge_store import input_reply_ids
from codex_transport import CODEX_HOME
from rollout_status import bootstrap_offset


def observe(state, record, thread):
    p = record.get('payload')
    if not isinstance(p, dict):
        return None
    kind = record.get('type')
    if kind == 'event_msg' and p.get('type') == 'task_started':
        state.update(turn=p.get('turn_id'), inputs=[])
    text = None
    # App steering is recorded as a named tool delivery, not a user_message.
    # Accept only the app's exact envelope for this same task; arbitrary tool
    # output and quoted Discord markers remain non-authoritative.
    if kind == 'event_msg' and p.get('type') == 'item_completed':
        item = p.get('item') or {}
        if (p.get('thread_id') == thread['id'] and item.get('type') == 'FunctionCallOutput'
                and item.get('namespace') == 'codex_app'
                and item.get('name') == 'send_message_to_thread'):
            match = re.fullmatch(r'<codex_delegation>\s*<source_thread_id>([^<]+)</source_thread_id>\s*<input>(.*)</input>\s*</codex_delegation>',
                                 item.get('output', ''), re.S)
            if match and match[1] == thread['id']:
                text = match[2]
    if kind == 'event_msg' and p.get('type') == 'user_message':
        text = p.get('message')
    elif kind == 'response_item' and p.get('type') == 'message' and p.get('role') == 'user':
        content = p.get('content')
        if isinstance(content, list):
            text = ''.join(c.get('text', '') for c in content
                           if isinstance(c, dict) and isinstance(c.get('text'), str))
    for mid in input_reply_ids(text):
        if mid not in state.setdefault('inputs', []):
            state['inputs'].append(mid)
    if kind != 'event_msg' or p.get('type') != 'task_complete':
        return None
    turn = p.get('turn_id')
    body = p.get('last_agent_message')
    if not isinstance(turn, str) or not turn or not isinstance(body, str) or not body.strip():
        return None
    inputs = state.get('inputs', []) if state.get('turn') == turn else []
    return {'type': 'agent-turn-complete', 'thread-id': thread['id'],
            'turn-id': turn, 'cwd': thread['cwd'], 'last-assistant-message': body,
            'input-messages': ['[Discord reply ' + mid + ']' for mid in inputs]}


class CompletionMonitor:
    def __init__(self, store, env, home=CODEX_HOME):
        self.store, self.env, self.home = store, env, Path(home)

    def deliver(self, event):
        # Same route/claim/policy as the native hook, without a command-line JSON.
        if notifier.already_sent(notifier.event_key(event)):
            return
        if self.store.complete_event(event):
            return
        status = notifier.parse_status(event['last-assistant-message'])
        if not status.has_contract:
            return
        if status.state == 'turn_complete' and not notifier.bool_value(
                self.env.get('DISCORD_SEND_TURN_COMPLETE'), False):
            return
        payload = notifier.make_payload(event, status, self.env)
        if not self.store.queue_notification(event, payload):
            raise RuntimeError('Completion destination unavailable')

    def scan(self, thread, path, now=None):
        now = time.time() if now is None else now
        key = 'completion_cursor:' + thread['id'] + ':' + hashlib.sha256(
            str(path).encode()).hexdigest()[:16]
        raw = self.store.get(key)
        state = json.loads(raw) if raw else {'offset': bootstrap_offset(path), 'inputs': [], 'bootstrap': True}
        if path.stat().st_size < state['offset']:
            state = {'offset': 0, 'inputs': [], 'bootstrap': True}
        with path.open('rb') as handle:
            handle.seek(state['offset'])
            end = state['offset'] + 4 * 1024 * 1024
            while handle.tell() < end:
                before = handle.tell()
                line = handle.readline()
                if not line or not line.endswith(b'\n'):
                    handle.seek(before)
                    break
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        continue
                    stamp = datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00')).timestamp()
                    event = observe(state, record, thread)
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue
                if event and stamp >= now - 86400:
                    if state.get('bootstrap'):
                        state['latest'] = event
                    else:
                        self.deliver(event)
            state['offset'] = handle.tell()
            if handle.tell() == path.stat().st_size and state.get('bootstrap'):
                latest = state.pop('latest', None)
                if latest:
                    self.deliver(latest)
                state['bootstrap'] = False
        # Commit the cursor only after durable output creation; crash replay is safe.
        self.store.put(key, json.dumps(state))

    def poll(self):
        with self.store.connect() as db:
            tids = [r[0] for r in db.execute('''SELECT DISTINCT thread_id FROM routes
                WHERE origin='codex' AND bot_id=? AND channel_id=?''',
                (self.store.get('bot_id'), self.store.get('channel_id')))]
        with closing(sqlite3.connect((self.home / 'state_5.sqlite').as_uri() + '?mode=ro',
                                    uri=True, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            for tid in tids:
                row = db.execute('''SELECT id,cwd,rollout_path FROM threads WHERE id=?
                    AND archived=0 AND source IN ('cli','vscode','exec','appServer')
                    AND updated_at>=?''', (tid, time.time() - 86400)).fetchone()
                if row and row['rollout_path']:
                    self.scan(dict(row), Path(row['rollout_path']))
