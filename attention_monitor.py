"""Read-only incremental watcher for questions and terminal errors in mapped tasks.

Only explicit user-facing event types are extracted. Reasoning, arbitrary tool
arguments, command output, and credentials are never forwarded.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import time
import logging
from contextlib import closing
from datetime import datetime

from codex_transport import CODEX_HOME
from error_reporting import summarize


def question_text(arguments):
    parts = []
    for index, q in enumerate(arguments.get('questions', []), 1):
        title = q.get('question') or q.get('title')
        if not title:
            continue
        parts.append((str(index) + '. ' if len(arguments['questions']) > 1 else '') + str(title))
        for n, option in enumerate(q.get('options') or [], 1):
            if isinstance(option, dict):
                label = str(option.get('label') or '')
                detail = str(option.get('description') or '')
                option = label + (' — ' + detail if detail else '')
            parts.append('  ' + str(n) + ') ' + str(option))
    return '\n'.join(parts)


def classify(record):
    """Return (kind, description, reply policy), never arbitrary tool content."""
    p = record.get('payload') or {}
    if not isinstance(p,dict): return None
    typ = p.get('type', '')
    if record.get('type') == 'response_item' and typ == 'function_call':
        name = p.get('name', '').split('.')[-1]
        if name in ('request_user_input', 'request_user_input_async'):
            try:
                args = json.loads(p.get('arguments') or '{}')
            except (ValueError, TypeError):
                return None
            if not isinstance(args,dict) or not isinstance(args.get('questions'),list):
                return None
            if not all(isinstance(q,dict) and isinstance(q.get('options',[]),(list,type(None))) for q in args['questions']):
                return None
            if any(q.get('is_secret') or q.get('isSecret') for q in args.get('questions', [])):
                return 'desktop', 'Codex is asking for confidential input. Provide it in Codex, not in Discord.', 'desktop'
            text = question_text(args)
            if text:
                if name.endswith('_async'):
                    return 'question', text + '\n\nReply here with your answer; it will be sent directly to this task, steering its active turn.', 'queue'
                return 'desktop', text + '\n\nThis question is waiting in a native Codex dialog. Please answer it in Codex.', 'desktop'
        if name == 'request_permissions':
            return 'desktop', 'Codex is requesting additional permissions. Review the exact request in Codex.', 'desktop'
    if record.get('type') != 'event_msg':
        return None
    if typ in ('exec_approval_request', 'apply_patch_approval_request',
               'request_permissions', 'elicitation_request'):
        return 'desktop', 'Codex needs an approval or secure input. Open the original task in Codex to review it.', 'desktop'
    if typ in ('error', 'turn_failed'):
        if p.get('will_retry') is True or p.get('willRetry') is True:
            return None
        return 'error', summarize(p)[1], 'queue'
    if typ == 'turn_aborted':
        return 'interrupted', 'This Codex turn was interrupted. Reply here if you want to continue.', 'queue'
    if typ == 'thread_goal_updated':
        goal=p.get('goal') or {}
        if not isinstance(goal,dict): return None
        status=goal.get('status')
        if not isinstance(status,str): return None
        states={
            'paused': 'This goal is paused. It is not complete. Resume it in Codex when you want it to continue.',
            'budgetLimited': 'This goal stopped at its configured token budget. This is separate from account usage limits. Review the budget in Codex before resuming.',
            'usageLimited': 'This goal stopped because Codex usage is limited. Check account usage/reset information in Codex, then resume when usage is available. This event does not supply a reset time.'}
        if status in states:
            text=states[status]
            used,budget=goal.get('tokensUsed'),goal.get('tokenBudget')
            if status=='budgetLimited' and all(type(x) is int and x>=0 for x in (used,budget)):
                text+='\nReported task tokens: '+format(used,',')+' / '+format(budget,',')+'.'
            return 'goal_'+status,text,'desktop'
    return None


class AttentionMonitor:
    def __init__(self, store, home=CODEX_HOME):
        self.store = store
        self.home = Path(home)
        if self.store.get('attention_since') is None:
            self.store.put('attention_since', time.time())

    def emit(self, route, record):
        if not isinstance(record,dict): return
        p = record.get('payload') or {}
        if not isinstance(p,dict): return
        if p.get('type')=='thread_goal_updated':
            goal=p.get('goal') or {}
            if not isinstance(goal,dict): return
            status=goal.get('status')
            if not isinstance(status,str): return
            state_key='attention_goal_state:'+route['thread_id']+':'+str(goal.get('createdAt',''))
            if self.store.get(state_key)==status:
                return
        result = classify(record)
        if not result:
            if p.get('type')=='thread_goal_updated' and isinstance(status,str):
                self.store.put(state_key,status)
            return
        kind, text, policy = result
        identity = p.get('call_id') or str(record.get('ordinal')) + ':' + record.get('timestamp', '')
        if kind.startswith('goal_'):
            identity='goal:'+str(goal.get('createdAt',''))+':'+str(goal.get('updatedAt',record.get('timestamp','')))+':'+status
        if kind in ('error', 'interrupted') and p.get('turn_id'):
            identity = 'terminal:' + p['turn_id']
        key = 'attention:' + hashlib.sha256((route['thread_id'] + identity).encode()).hexdigest()[:20]
        titles = {'question': 'Codex has a question', 'desktop': 'Action needed in Codex',
                  'error': 'Codex encountered an error', 'interrupted': 'Codex was interrupted',
                  'goal_paused':'Goal paused', 'goal_budgetLimited':'Goal token budget reached',
                  'goal_usageLimited':'Goal stopped by usage limit'}
        if kind == 'error':
            titles['error'] = summarize(p)[0]
        payload = {'_reply_policy': policy, 'embeds': [{
            'title': titles[kind], 'description': text[:3900],
            'color': 0xE74C3C if kind == 'error' else 0xF39C12}],
            'allowed_mentions': {'parse': []}}
        self.store.output(route, key, payload)
        # Enrich a pending fallback once detailed history arrives, without sending twice.
        if kind == 'error' and titles['error'] != 'Codex encountered an error':
            self.store.replace_output(route['message_id'] + ':' + key, payload)
        if kind.startswith('goal_'):
            self.store.put(state_key,status)

    def poll(self):
        with self.store.connect() as db:
            routes = [dict(r) for r in db.execute('''SELECT * FROM routes
                WHERE origin='codex' AND thread_id<>'' ORDER BY rowid ASC''')]
        # Watch only tasks already connected to this owner's notification stream.
        by_thread = {}
        for route in routes:
            if (route['bot_id'] == self.store.get('bot_id') and
                    route['channel_id'] == self.store.get('channel_id')):
                by_thread.setdefault(route['thread_id'], route)
        if not by_thread:
            return
        state = self.home / 'state_5.sqlite'
        unreadable=0
        with closing(sqlite3.connect(state.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
            for tid, route in by_thread.items():
                record = db.execute('SELECT rollout_path FROM threads WHERE id=?', (tid,)).fetchone()
                if record and record[0]:
                    try:
                        self.scan(route, Path(record[0]))
                    except OSError as exc:
                        unreadable+=1
                        logging.getLogger('reply_bridge').warning('Task attention file unavailable: %s',type(exc).__name__)
        self.failed_turns(by_thread)
        if unreadable:
            raise OSError('One or more task attention files could not be read')

    def failed_turns(self, routes):
        path = self.home / 'thread_history_1.sqlite'
        if not path.is_file():
            return
        since = float(self.store.get('attention_since'))
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
            columns={r[1] for r in db.execute('PRAGMA table_info(thread_turns)')}
            error_column='error_json' if 'error_json' in columns else 'NULL'
            for tid, route in routes.items():
                rows = db.execute('''SELECT turn_id,status,completed_at,''' + error_column + ''' FROM thread_turns
                    WHERE thread_id=? AND status IN ('failed','interrupted') AND completed_at>=?
                    ORDER BY completed_at DESC LIMIT 20''', (tid, since)).fetchall()
                for turn_id, status, stamp, raw_error in rows:
                    try:
                        error=json.loads(raw_error) if raw_error else {}
                    except (TypeError,ValueError):
                        error={}
                    self.emit(route, {'type': 'event_msg', 'payload': {
                        'type': 'turn_failed' if status == 'failed' else 'turn_aborted', 'turn_id': turn_id, 'error': error}})

    def scan(self, route, path):
        if not path.is_file():
            return
        cursor_key = 'attention_cursor:' + route['thread_id'] + ':' + hashlib.sha256(str(path).encode()).hexdigest()[:16]
        offset = int(self.store.get(cursor_key, '0'))
        if path.stat().st_size < offset:
            offset = 0
        since = float(self.store.get('attention_since', str(time.time())))
        with path.open('rb') as handle:
            handle.seek(offset)
            end = offset + 4 * 1024 * 1024
            while handle.tell() < end:
                before = handle.tell()
                line = handle.readline()
                if not line or not line.endswith(b'\n'):
                    handle.seek(before)
                    break
                try:
                    record = json.loads(line)
                    if not isinstance(record,dict): continue
                    raw_stamp=record.get('timestamp')
                    if not isinstance(raw_stamp,str): continue
                    stamp = datetime.fromisoformat(raw_stamp.replace('Z', '+00:00')).timestamp()
                except (ValueError, TypeError):
                    continue
                if stamp >= since:
                    self.emit(route, record)
            # Save only after every extracted event is durably recorded.
            self.store.put(cursor_key, handle.tell())
