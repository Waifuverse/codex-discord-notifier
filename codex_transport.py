"""Codex live app transport. Local SQLite is used read-only for identity checks."""
from __future__ import annotations

import os
import json
import shutil
import sqlite3
import app_transport
from contextlib import closing
from pathlib import Path
from uuid import UUID

HIDDEN = app_transport.HIDDEN
CODEX_HOME = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))


def executable():
    path = shutil.which('codex')
    if path:
        return path
    path = Path(os.environ['LOCALAPPDATA']) / 'Programs/OpenAI/Codex/bin/codex.exe'
    if not path.is_file():
        raise FileNotFoundError('Codex CLI is not installed')
    return str(path)


def lookup_thread(thread_id):
    try:
        if str(UUID(thread_id)) != thread_id:
            return None
    except (ValueError, TypeError):
        return None
    # Pinned to the installed Codex schema; fail closed if it changes.
    path = CODEX_HOME / 'state_5.sqlite'
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT id,cwd,archived,source FROM threads WHERE id=?', (thread_id,)).fetchone()
        return dict(row) if row else None


def dispatch(row):
    try:
        thread = lookup_thread(row['thread_id'])
    except (sqlite3.Error, OSError):
        return 'pending', 'Codex task storage is unavailable; the reply is saved for retry.', None
    if not thread:
        return 'failed', 'Codex task could not be found.', None
    if thread['archived']:
        return 'failed', 'This task is archived. Unarchive it in Codex, then send a new reply.', None
    if not Path(thread['cwd']).is_dir():
        return 'failed', 'The task working directory no longer exists.', None
    images = json.loads(row.get('images_json') or '[]')
    if images and (len(images) > 4 or not all(Path(p).is_file() for p in images)):
        return 'failed', 'An attached image is missing. Please resend the images.', None
    prompt = '[Discord reply ' + row['message_id'] + ']\n' + row['text'] + (
        '\n\n[Discord delivery guidance: If you need my answer before continuing, '
        'ask the complete question in your final response with any choices, include '
        'a discord-status block with state: needs_input, and end the turn so I can '
        'reply in Discord. Prefer this to a blocking desktop question widget. '
        'For optional questions while working, request_user_input_async is relayed '
        'to Discord. Clearly report blocked or failed outcomes using the matching '
        'status. Native permission, credential, and approval dialogs still require '
        'action in Codex; do not treat a queued chat reply as resolving such a dialog.]')
    media_python = str(Path(__file__).resolve().parent / '.venv/Scripts/python.exe')
    media_cli = str(Path(__file__).resolve().parent / 'media_cli.py')
    prompt += ('\n[Media tools available for this Discord task: run the executable ' + json.dumps(media_python) +
        ' with script ' + json.dumps(media_cli) + ' and one of: screenshot --thread ' + row['thread_id'] +
        '; clip --thread ' + row['thread_id'] + ' --seconds 10; send --thread ' + row['thread_id'] +
        ' --file <local image or short video>. Optional --monitor 1 and --region X Y WIDTH HEIGHT '
        'select the capture area. Use windows to list windows, then --window <exact title or handle> '
        'for isolated game-window capture with no desktop fallback. Capture/send only when requested or needed for the user-authorized '
        'game test. The helper compresses below 9 MiB and queues upload to this task. '
        'Use status --id <returned output_id> to verify delivery.]')
    if images:
        prompt += ('\n\nThe user sent the following image files with this Discord reply. '
            'Open each image using view_image (or the available equivalent image-viewing tool) '
            'before answering about its contents. Do not infer contents from filenames. '
            'If viewing fails, say so rather than guessing. The paths are:\n' +
            '\n'.join(str(Path(p).resolve()) for p in images))
    return app_transport.deliver(row['thread_id'], prompt)
