"""Age-based managed-file cleanup, preserving unfinished delivery/input references."""
import json
import os
from pathlib import Path
import time


def cleanup(store, root, days=7, now=None, dry_run=False):
    if not 0 <= days <= 3650: raise ValueError('Retention must be 0–3650 days (0 disables cleanup).')
    result={'deleted':0,'bytes':0,'protected':0,'errors':0,'disabled':days==0}
    if days==0: return result
    now=time.time() if now is None else now
    root=Path(root).resolve()
    if not root.is_dir(): return result
    protected=set()
    with store.connect() as db:
        for row in db.execute('SELECT payload FROM outbox WHERE sent_id IS NULL'):
            path=json.loads(row[0]).get('_media_path')
            if path: protected.add(Path(path).resolve())
        for row in db.execute("SELECT images_json FROM inbox WHERE state NOT IN ('completed','failed','rejected') OR updated>?",(now-days*86400,)):
            protected.update(Path(p).resolve() for p in json.loads(row[0]))
    # os.walk does not follow symlinks/junctions; additionally reject reparse points.
    for directory,dirs,files in os.walk(root,followlinks=False):
        dirs[:]=[d for d in dirs if not (Path(directory)/d).is_symlink() and not (getattr((Path(directory)/d).stat(),'st_file_attributes',0)&1024)]
        for name in files:
            path=Path(directory)/name
            try:
                if path.is_symlink() or not path.resolve().is_relative_to(root): continue
                info=path.stat()
                if getattr(info,'st_file_attributes',0)&1024: continue
                if path.resolve() in protected:
                    result['protected']+=1; continue
                if info.st_mtime>now-days*86400: continue
                if not dry_run: path.unlink()
                result['deleted']+=1; result['bytes']+=info.st_size
            except OSError: result['errors']+=1
    return result
