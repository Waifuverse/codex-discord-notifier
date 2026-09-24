"""Durable reply routing shared by the short-lived hook and the listener."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / '.state' / 'reply-bridge.sqlite'
MARKER = re.compile(r'\[Discord reply (\d+)\]')


def input_reply_ids(inputs):
    """Only a queued input's leading marker is authoritative, never quoted context."""
    if isinstance(inputs,str): inputs=[inputs]
    if not isinstance(inputs,list): return []
    ids=[]
    for item in inputs:
        if isinstance(item,dict):
            item=item.get('content','') if item.get('role')=='user' else ''
        if isinstance(item,str):
            match=MARKER.match(item.lstrip())
            if match and match[1] not in ids: ids.append(match[1])
    return ids


class Store:
    def __init__(self, path=DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS routes (
                    message_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL,
                    bot_id TEXT NOT NULL, origin TEXT NOT NULL, thread_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL DEFAULT '', cwd TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS inbox (
                    message_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL, cwd TEXT NOT NULL, text TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', queue_id TEXT,
                    created REAL NOT NULL, updated REAL NOT NULL, attempts INTEGER DEFAULT 0,
                    next_try REAL DEFAULT 0, final_turn TEXT, error TEXT);
                CREATE TABLE IF NOT EXISTS outbox (
                    id TEXT PRIMARY KEY, message_id TEXT NOT NULL, channel_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL, cwd TEXT NOT NULL, payload TEXT NOT NULL,
                    sent_id TEXT, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS media_jobs (
                    message_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, thread_id TEXT NOT NULL,
                    cwd TEXT NOT NULL, params TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    created REAL NOT NULL);
            ''')
            if 'images_json' not in {r[1] for r in db.execute('PRAGMA table_info(inbox)')}:
                try:
                    db.execute("ALTER TABLE inbox ADD COLUMN images_json TEXT NOT NULL DEFAULT '[]'")
                except sqlite3.OperationalError:
                    if 'images_json' not in {r[1] for r in db.execute('PRAGMA table_info(inbox)')}:
                        raise

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def route(self, message_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM routes WHERE message_id=?', (str(message_id),)).fetchone()
            return dict(row) if row else None

    def map_message(self, message, event):
        if not message or not str(message.get('id', '')).isdigit():
            return
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO routes VALUES (?,?,?,?,?,?,?)', (
                str(message['id']), str(message['channel_id']), str(message['author']['id']),
                event.get('origin', 'codex'), event['thread-id'], event.get('turn-id', ''),
                event.get('cwd', '')))

    def get(self, key, default=None):
        with self.connect() as db:
            r = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
            return r[0] if r else default

    def put(self, key, value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, str(value)))

    def enqueue(self, message_id, channel_id, route, text, images=None):
        now = time.time()
        with self.connect() as db:
            return bool(db.execute('''INSERT OR IGNORE INTO inbox
                (message_id,channel_id,thread_id,cwd,text,created,updated,images_json)
                VALUES (?,?,?,?,?,?,?,?)''', (str(message_id), str(channel_id), route['thread_id'],
                route['cwd'], text, now, now, json.dumps(images or []))).rowcount)

    def capture_job(self, row, params):
        with self.connect() as db:
            return bool(db.execute('''INSERT OR IGNORE INTO media_jobs
                (message_id,channel_id,thread_id,cwd,params,created) VALUES (?,?,?,?,?,?)''',
                (row['message_id'],row['channel_id'],row['thread_id'],row['cwd'],json.dumps(params),time.time())).rowcount)

    def media_jobs(self, state):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM media_jobs WHERE state=? ORDER BY created', (state,))]

    def media_state(self, mid, state):
        with self.connect() as db:
            db.execute('UPDATE media_jobs SET state=? WHERE message_id=?', (state,mid))

    def replace_output(self, output_id, payload):
        with self.connect() as db:
            db.execute('UPDATE outbox SET payload=? WHERE id=? AND sent_id IS NULL', (json.dumps(payload), output_id))

    def rows(self, states):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                'SELECT * FROM inbox WHERE state IN (' + ','.join('?' for _ in states) +
                ') ORDER BY created,message_id', states)]

    def update(self, message_id, state, **fields):
        allowed = {'queue_id', 'error', 'next_try', 'attempts', 'final_turn'}
        if not fields.keys() <= allowed:
            raise ValueError('Unknown inbox field')
        values = {'state': state, 'updated': time.time(), **fields}
        with self.connect() as db:
            db.execute('UPDATE inbox SET ' + ','.join(k+'=?' for k in values) +
                       ' WHERE message_id=?', (*values.values(), str(message_id)))

    def finish_dispatch(self, message_id, state, queue_id, attempts, next_try):
        """Do not overwrite a completion that raced with the queue subprocess."""
        with self.connect() as db:
            return bool(db.execute('''UPDATE inbox SET state=?,queue_id=?,attempts=?,next_try=?,updated=?
                WHERE message_id=? AND state='dispatching' ''',
                (state,queue_id,attempts,next_try,time.time(),str(message_id))).rowcount)

    def output(self, row, kind, payload):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO outbox VALUES (?,?,?,?,?,?,NULL,?)', (
                row['message_id'] + ':' + kind, row['message_id'], row['channel_id'],
                row['thread_id'], row['cwd'], json.dumps(payload), time.time()))

    def outputs(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM outbox WHERE sent_id IS NULL ORDER BY created')]

    def output_sent(self, output, message):
        policy = json.loads(output['payload']).get('_reply_policy', 'queue')
        # Commit route, policy, and delivery together: a crash must not turn a
        # desktop-only approval into a normally answerable Discord question.
        with self.connect() as db:
            if output['thread_id']:
                db.execute('INSERT OR IGNORE INTO routes VALUES (?,?,?,?,?,?,?)',(
                    str(message['id']),str(message['channel_id']),str(message['author']['id']),
                    'codex',output['thread_id'],'',output['cwd']))
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('reply_policy:'+str(message['id']),policy))
            db.execute('UPDATE outbox SET sent_id=? WHERE id=?', (str(message['id']), output['id']))

    def reply_context(self, message_id):
        """Read the exact previously delivered bot question/answer, not a latest-task guess."""
        with self.connect() as db:
            row=db.execute('SELECT payload FROM outbox WHERE sent_id=?',(str(message_id),)).fetchone()
        if not row:
            return ''
        payload=json.loads(row[0])
        return '\n'.join(str(e.get('description') or '') for e in payload.get('embeds',[]))[:3900]

    def output_reacted(self, output, reaction):
        with self.connect() as db:
            db.execute('UPDATE outbox SET sent_id=? WHERE id=?', ('reaction:' + reaction, output['id']))

    def complete_event(self, event):
        """One final delivery per task/turn, even when several inputs were consumed."""
        if event.get('origin', 'codex') != 'codex':
            return False
        tid,turn=event.get('thread-id'),event.get('turn-id')
        if not tid or not turn:
            return False
        ids=input_reply_ids(event.get('input-messages', []))
        identity='final-delivery:'+json.dumps([tid,turn],separators=(',',':'))
        with self.connect() as db:
            # Serialize concurrent notification hooks before selecting/claiming inputs.
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM meta WHERE key=?',(identity,)).fetchone():
                return True
            matched=[]
            for mid in ids:
                row=db.execute('SELECT * FROM inbox WHERE message_id=? AND thread_id=?',(mid,tid)).fetchone()
                if row and (row['state'] in ('dispatching','submitted','uncertain') or
                            (row['state']=='completed' and row['final_turn']==turn)):
                    matched.append(dict(row))
            if not matched:
                return False
            # Preserve already-generated legacy finals if an old hook is replayed.
            existing=db.execute("SELECT o.id FROM outbox o JOIN inbox i ON i.message_id=o.message_id WHERE i.thread_id=? AND i.final_turn=? AND o.id=i.message_id || ':final' LIMIT 1",(tid,turn)).fetchone()
            row=max(matched,key=lambda r:(r['created'],int(r['message_id'])))
            output_id=existing[0] if existing else row['message_id']+':final'
            if not existing:
                from notifier import parse_status,STATE_STYLE
                status=parse_status(event.get('last-assistant-message',''))
                body=re.sub(r'<!--\s*discord-status\s*.*?-->','',event.get('last-assistant-message',''),flags=re.S|re.I).strip()
                titles={'needs_input':'Codex has a question','blocked':'Codex is blocked','failed':'Codex encountered an error'}
                payload={'content':'','embeds':[{'title':titles.get(status.state,'Codex replied'),
                    'description':body[:3900] or status.summary,'color':STATE_STYLE[status.state][1]}],
                    'allowed_mentions':{'parse':[]}}
                if len(body)>3900:
                    payload['embeds'][0]['description']+='\n\n[Full response is in the Codex task.]'
                db.execute('INSERT OR IGNORE INTO outbox VALUES (?,?,?,?,?,?,NULL,?)',
                    (output_id,row['message_id'],row['channel_id'],tid,row['cwd'],json.dumps(payload),time.time()))
            for item in matched:
                db.execute("UPDATE inbox SET state='completed',updated=?,final_turn=? WHERE message_id=?",
                    (time.time(),turn,item['message_id']))
            db.execute('INSERT INTO meta VALUES (?,?)',(identity,output_id))
        return True

    def queue_notification(self, event, payload):
        """Share one durable task/turn claim between the hook and rollout fallback."""
        tid, turn = event.get('thread-id'), event.get('turn-id')
        channel = self.get('channel_id')
        if not tid or not turn or not channel or not self.get('bot_id'):
            return False
        identity = 'final-delivery:' + json.dumps([tid, turn], separators=(',', ':'))
        output_id = 'notification:' + tid + ':' + turn
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM meta WHERE key=?', (identity,)).fetchone():
                return True
            # Preserve deliveries made by the pre-outbox notification hook.
            if db.execute('SELECT 1 FROM routes WHERE thread_id=? AND turn_id=?',
                          (tid, turn)).fetchone():
                return True
            db.execute('INSERT OR IGNORE INTO outbox VALUES (?,?,?,?,?,?,NULL,?)',
                       (output_id, '', channel, tid, event.get('cwd', ''),
                        json.dumps(payload), time.time()))
            db.execute('INSERT INTO meta VALUES (?,?)', (identity, output_id))
        return True
