"""Capture/send media to the configured Discord destination for one Codex task."""
from __future__ import annotations

import argparse
import json
import uuid

import mss

from bridge_store import Store
from codex_transport import lookup_thread
from media_support import MediaError, screenshot, record_clip, prepare_file, queue_file


def task_destination(store, thread_id):
    task = lookup_thread(thread_id)
    if not task:
        raise MediaError('Codex task does not exist.')
    channel = store.get('channel_id')
    bot = store.get('bot_id')
    if not channel or not bot:
        raise MediaError('The Discord listener has not connected yet.')
    with store.connect() as db:
        reference = db.execute('''SELECT message_id FROM routes WHERE thread_id=?
            AND origin='codex' AND channel_id=? AND bot_id=? ORDER BY rowid DESC LIMIT 1''',
            (thread_id,channel,bot)).fetchone()
    return {'message_id': reference[0] if reference else 'media-' + uuid.uuid4().hex,
            'channel_id':channel,'thread_id':thread_id,'cwd':task['cwd']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('monitors')
    commands.add_parser('windows')
    cleanup_parser=commands.add_parser('cleanup')
    cleanup_parser.add_argument('--dry-run',action='store_true')
    status=commands.add_parser('status'); status.add_argument('--id',required=True)
    for name in ('screenshot','clip','send'):
        p=commands.add_parser(name)
        p.add_argument('--thread',required=True)
        p.add_argument('--caption',default='')
        if name=='send':
            p.add_argument('--file',required=True)
        else:
            p.add_argument('--monitor',type=int,default=1)
            p.add_argument('--window',help='Exact window title, unique title substring, or numeric handle; no desktop fallback')
            p.add_argument('--region',type=int,nargs=4,metavar=('X','Y','WIDTH','HEIGHT'))
        if name=='clip':
            p.add_argument('--seconds',type=int,default=10)
            p.add_argument('--fps',type=int,default=15)
    args=parser.parse_args()
    try:
        store=Store()
        if args.command=='windows':
            from window_capture import windows
            print(json.dumps(windows(),ensure_ascii=False)); return 0
        if args.command=='cleanup':
            import notifier
            from media_retention import cleanup
            from media_support import MEDIA_ROOT
            days=int(notifier.load_env().get('DISCORD_MEDIA_RETENTION_DAYS','7'))
            print(json.dumps(cleanup(store,MEDIA_ROOT,days,dry_run=args.dry_run))); return 0
        if args.command=='monitors':
            with mss.MSS() as screen:
                print(json.dumps([{'monitor':i,**{k:m[k] for k in ('left','top','width','height')}} for i,m in enumerate(screen.monitors) if i]))
            return 0
        if args.command=='status':
            with store.connect() as db:
                row=db.execute('SELECT sent_id FROM outbox WHERE id=?',(args.id,)).fetchone()
            state = 'missing' if not row else 'queued' if not row[0] else 'sent' if row[0].isdigit() else 'failed'
            print(json.dumps({'output_id':args.id,'status':state,
                              'discord_message_id':row[0] if state=='sent' else None}))
            return 0
        row=task_destination(store,args.thread)
        if args.command=='screenshot':
            path=screenshot(args.monitor,args.region,window=args.window)
        elif args.command=='clip':
            path=record_clip(args.seconds,args.monitor,args.region,args.fps,window=args.window)
        else:
            path=prepare_file(args.file)
        output_id=queue_file(store,row,path,args.caption)
        print(json.dumps({'status':'queued','output_id':output_id,'path':str(path),'bytes':path.stat().st_size}))
        return 0
    except (MediaError,OSError) as exc:
        print(json.dumps({'status':'failed','error':str(exc)}))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
