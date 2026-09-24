"""Read-only local task-history snapshot; never starts a model turn."""
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from codex_transport import CODEX_HOME
from thread_presentation import task_title


class JobsError(Exception): pass


def read_jobs(home=None, now=None):
    home=Path(home or CODEX_HOME)
    now=time.time() if now is None else now
    try:
        with closing(sqlite3.connect((home/'state_5.sqlite').as_uri()+'?mode=ro',uri=True,timeout=5)) as db:
            tasks={row[0]:row[1] for row in db.execute(
                "SELECT id,updated_at FROM threads WHERE archived=0 AND source IN ('cli','vscode','exec','appServer')")}
            columns={r[1] for r in db.execute('PRAGMA table_info(threads)')}
            paths=dict(db.execute('SELECT id,rollout_path FROM threads')) if 'rollout_path' in columns else {}
        with closing(sqlite3.connect((home/'thread_history_1.sqlite').as_uri()+'?mode=ro',uri=True,timeout=5)) as db:
            rows=db.execute('''SELECT thread_id,status,started_at,completed_at FROM
                (SELECT *,ROW_NUMBER() OVER (PARTITION BY thread_id ORDER BY rollout_ordinal DESC,started_at DESC) AS position
                 FROM thread_turns) WHERE position=1''').fetchall()
        projected={row[0] for row in rows}
        from rollout_status import latest_status
        for identity in tasks.keys()-projected:
            if paths.get(identity):
                rows.append((identity,*latest_status(paths[identity])))
        result=[]
        for identity,status,started,completed in rows:
            if identity not in tasks or status!='inProgress' or completed is not None: continue
            recent=max(started or 0,tasks[identity] or 0)
            result.append({'id':identity,'title':task_title(identity,home),'started':started,
                           'stale':now-recent>86400})
        return sorted(result,key=lambda x:(x['stale'],-(x['started'] or 0),x['id']))
    except (sqlite3.Error,OSError,TypeError,ValueError) as exc:
        raise JobsError('Task status is unavailable from local Codex storage. No empty or idle result was assumed. Try !jobs again after opening Codex.') from exc


def format_jobs(jobs):
    import discord
    lines=['Local saved status snapshot; running or waiting turns are recorded as in progress. This is not a live heartbeat.']
    current=[j for j in jobs if not j['stale']]
    lines.append(f'\n**{len(current)} task(s) recorded in progress**')
    for job in current:
        line='• '+discord.utils.escape_markdown(job['title']).replace('@','@\u200b')
        if job['started']: line+=f" · started <t:{int(job['started'])}:R>"
        if len('\n'.join(lines))+len(line)>3500:
            lines.append('Additional tasks omitted to fit Discord.');break
        lines.append(line)
    stale=sum(j['stale'] for j in jobs)
    if stale: lines.append(f'\n{stale} unfinished record(s) older than 24 hours excluded; their current activity is unconfirmed.')
    lines.append('\nChecked <t:'+str(int(time.time()))+':T>. Reply to a task notification to continue that task.')
    return '\n'.join(lines)
