"""Persistent categorical colors; red belongs exclusively to errors."""
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from uuid import UUID

ERROR_COLOR = 0xE74C3C
# One swatch per category: never generate lighter/darker variants.
PALETTE = (("blue",0x3973E8),("yellow",0xF4D03F),("green",0x28A745),
           ("purple",0x9B59B6),("cyan",0x00D4D4),("orange",0xF39C12),
           ("white",0xFFFFFF))
REGISTRY = Path(__file__).resolve().parent/'.state'/'thread-colors.sqlite'


def thread_color(thread_id, fallback=0x3498DB, *, registry=None, active_ids=None):
    try:
        identity=str(UUID(str(thread_id)))
    except (ValueError,TypeError,AttributeError):
        return fallback
    if active_ids is None:
        from jobs_status import read_jobs,JobsError
        try: active_ids={job['id'] for job in read_jobs() if not job['stale']}
        except JobsError: active_ids=None  # Unknown status must not revoke a lease.
    path=Path(registry or REGISTRY)
    try:
        path.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(path,timeout=5)) as db:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS colors (thread_id TEXT PRIMARY KEY,color INTEGER UNIQUE NOT NULL,used REAL NOT NULL)')
                db.execute('BEGIN IMMEDIATE')
                row=db.execute('SELECT color FROM colors WHERE thread_id=?',(identity,)).fetchone()
                if row:
                    db.execute('UPDATE colors SET used=? WHERE thread_id=?',(time.time(),identity))
                    return row[0]
                leases=db.execute('SELECT thread_id,color,used FROM colors ORDER BY used').fetchall()
                occupied={row[1] for row in leases}
                color=next((value for _,value in PALETTE if value not in occupied),None)
                if color is None and active_ids is not None:
                    victim=next((row for row in leases if row[0] not in active_ids),None)
                    if victim:
                        color=victim[1]
                        db.execute('DELETE FROM colors WHERE thread_id=?',(victim[0],))
                if color is None: return 0  # Discord default/uncolored; no duplicate swatch.
                db.execute('INSERT INTO colors VALUES (?,?,?)',(identity,color,time.time()))
                return color
    except (sqlite3.Error,OSError):
        return 0  # Never silently fall back to an ambiguous hashed color.

def color_embeds(payload, thread_id, error=False):
    for embed in payload.get('embeds',[]):
        embed['color']=(ERROR_COLOR if error or embed.get('color')==ERROR_COLOR
                        else thread_color(thread_id,embed.get('color',0x3498DB)))
    return payload
