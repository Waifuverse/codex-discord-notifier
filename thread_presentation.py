"""Human-readable task headers; task IDs remain in routing storage."""
import os
from pathlib import Path
import json
from uuid import UUID


def task_title(thread_id, home=None):
    try:
        identity=str(UUID(str(thread_id)))
        home=Path(home or os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))
        # Desktop sidebar names live in the session index. threads.title can be
        # the original user prompt and must not be used as a sidebar-title fallback.
        title=None
        with (home/'session_index.jsonl').open(encoding='utf-8') as handle:
            for line in handle:
                try:
                    entry=json.loads(line)
                except ValueError:
                    continue  # Ignore a partial append without losing older titles.
                if isinstance(entry,dict) and entry.get('id')==identity:
                    name=entry.get('thread_name')
                    if isinstance(name,str) and name.strip():
                        title=name.strip()
        if title:
            return title[:64]
    except (ValueError,TypeError,OSError):
        pass
    return 'Codex task (title unavailable)'


def present_embeds(payload,thread_id):
    title=task_title(thread_id)
    for embed in payload.get('embeds',[]):
        status=embed.get('title') or 'Codex update'
        embed['title']=title
        embed['footer']={'text':status[:256]}
    return payload
