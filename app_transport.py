"""Deliver through the running Windows Codex app, preserving its live steering.

The app-tools named-pipe protocol is internal and may change with Codex updates.
Only read_thread and send_message_to_thread are used. Never fall back to queue
after a possibly accepted write. A short-lived child bounds blocked pipe I/O.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import struct
import subprocess
import sys
import threading
from uuid import uuid4

PREFIX = '\\\\.\\pipe\\codex-browser-use-'
CACHE = Path(__file__).resolve().parent / '.state/app-pipe.json'
MAX_FRAME = 16 * 1024 * 1024
HIDDEN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def read_exact(stream, count):
    data = bytearray()
    while len(data) < count:
        chunk = stream.read(count - len(data))
        if not chunk:
            raise OSError('App connection closed')
        data.extend(chunk)
    return bytes(data)


def rpc(pipe, tool, thread_id, arguments):
    request = {'id': 1, 'jsonrpc': '2.0', 'method': 'tools/call', 'params': {
        'namespace': 'codex_app', 'tool': tool, 'threadId': thread_id,
        'callId': 'discord-' + str(uuid4()), 'turnId': 'discord-bridge',
        'arguments': arguments}}
    data = json.dumps(request).encode('utf-8')
    if len(data) > MAX_FRAME:
        raise ValueError('Request too large')
    with open(pipe, 'r+b', buffering=0) as stream:
        stream.write(struct.pack('<I', len(data)) + data)
        size = struct.unpack('<I', read_exact(stream, 4))[0]
        if size > MAX_FRAME:
            raise ValueError('Response too large')
        response = json.loads(read_exact(stream, size))
    if response.get('id') != 1 or 'error' in response:
        raise ValueError('App rejected request')
    result = response.get('result', {})
    if result.get('success') is not True:
        raise ValueError('App did not confirm success')
    for item in result.get('contentItems', []):
        if item.get('type') == 'inputText':
            value = json.loads(item['text'])
            if isinstance(value, dict):
                return value
    raise ValueError('App returned no confirmation')


def candidates():
    paths = [os.environ.get('CODEX_APP_TOOLS_PIPE_PATH', '')]
    try:
        paths.append(json.loads(CACHE.read_text())['pipe'])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        paths.extend('\\\\.\\pipe\\' + name for name in os.listdir('\\\\.\\pipe\\')
                     if name.startswith('codex-browser-use-'))
    except OSError:
        pass
    return list(dict.fromkeys(p for p in paths if isinstance(p, str) and p.startswith(PREFIX)))


def inspect(pipe, thread_id):
    # Timed-out discovery only performs reads; its daemon thread dies with child.
    replies = queue.Queue()
    def run():
        try:
            replies.put(rpc(pipe, 'read_thread', thread_id,
                            {'threadId': thread_id, 'turnLimit': 1, 'maxOutputCharsPerItem': 0}))
        except Exception:
            replies.put(None)
    threading.Thread(target=run, daemon=True).start()
    try:
        result = replies.get(timeout=0.5)
    except queue.Empty:
        return False
    thread = (result or {}).get('thread', {})
    return (thread.get('id') == thread_id and thread.get('kind') == 'codex'
            and thread.get('hostId') == 'local')


def worker(payload, emit):
    thread_id = payload['threadId']
    for pipe in candidates():
        if not inspect(pipe, thread_id):
            continue
        try:
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(json.dumps({'pipe': pipe}))
        except OSError:
            pass
        # Persisted stdout marker precedes any side effect; timeout thereafter is
        # uncertain, never a retry or a second send through another pipe.
        emit({'state': 'sending'})
        try:
            result = rpc(pipe, 'send_message_to_thread', thread_id,
                         {'threadId': thread_id, 'prompt': payload['prompt']})
            if result.get('threadId') != thread_id:
                raise ValueError('Wrong task confirmation')
        except Exception:
            emit({'state': 'uncertain'})
            return
        emit({'state': 'submitted', 'threadId': thread_id})
        return
    emit({'state': 'pending'})


def deliver(thread_id, prompt):
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--deliver'],
            input=json.dumps({'threadId': thread_id, 'prompt': prompt}),
            capture_output=True, text=True, encoding='utf-8', timeout=30,
            creationflags=HIDDEN)
        output = result.stdout
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ''
    except OSError:
        return 'pending', 'Codex app connection is unavailable; your reply is saved for retry.', None
    if isinstance(output, bytes):
        output = output.decode('utf-8', errors='replace')
    try:
        records = [json.loads(line) for line in output.splitlines()]
    except ValueError:
        records = [{'state': 'uncertain'}]
    if records and records[-1].get('state') == 'submitted' and records[-1].get('threadId') == thread_id:
        return 'submitted', 'Sent directly to the original Codex task.', None
    if any(r.get('state') in ('sending', 'uncertain', 'submitted') for r in records):
        return 'uncertain', 'Codex did not confirm receipt. Check the task before resending.', None
    return 'pending', 'Open Codex to receive this reply; it is saved for automatic retry, not queued behind the active turn.', None


if __name__ == '__main__' and sys.argv[1:] == ['--deliver']:
    worker(json.load(sys.stdin), lambda value: print(json.dumps(value), flush=True))
