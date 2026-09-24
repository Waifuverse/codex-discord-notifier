"""Read the newest saved lifecycle event when the history projection is missing."""
import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path


def latest_status(path):
    path = Path(path)
    stat = path.stat()
    return _read(str(path), stat.st_size, stat.st_mtime_ns)


def reverse_lines(path):
    """Yield (byte offset, complete line) from newest to oldest."""
    with Path(path).open('rb') as handle:
        position, suffix = handle.seek(0, 2), b''
        while position:
            start = max(0, position - 256 * 1024)
            handle.seek(start)
            chunk = handle.read(position - start) + suffix
            lines = chunk.splitlines(keepends=True)
            suffix = lines.pop(0) if start and lines else b''
            cursor = start + len(chunk)
            for line in reversed(lines):
                cursor -= len(line)
                if line.endswith(b'\n'):
                    yield cursor, line
            position = start


def bootstrap_offset(path):
    """The latest completed turn plus any active turn are enough for bootstrap."""
    starts = 0
    for offset, line in reverse_lines(path):
        if b'"task_started"' not in line:
            continue
        try:
            record = json.loads(line)
            if record.get('type') == 'event_msg' and record.get('payload', {}).get('type') == 'task_started':
                starts += 1
                if starts == 2:
                    return offset
        except (ValueError, AttributeError):
            continue
    return 0


@lru_cache(maxsize=128)
def _read(path, size, mtime_ns):
    # Walk backwards in chunks; avoid loading an entire long task into memory.
    for _, line in reverse_lines(path):
        try:
            record = json.loads(line)
            p = record.get('payload', {})
            if record.get('type') != 'event_msg' or not isinstance(p, dict):
                continue
            typ = p.get('type')
            if typ not in ('task_started', 'task_complete', 'turn_aborted', 'turn_failed'):
                continue
            stamp = datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00')).timestamp()
            return ('inProgress', stamp, None) if typ == 'task_started' else ('completed', None, stamp)
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    raise ValueError('No saved task lifecycle event')
