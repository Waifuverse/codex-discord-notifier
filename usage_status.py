"""Read account usage through the local Codex protocol without starting a model turn."""
import json
import math
import queue
import subprocess
import threading
import time

from codex_transport import executable,HIDDEN


class UsageError(Exception): pass


def read_usage(timeout=25):
    process=None
    try:
        process=subprocess.Popen([executable(),'app-server','--stdio'],stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf-8',
            creationflags=HIDDEN)
        messages=queue.Queue()
        def read():
            try:
                for line in process.stdout:
                    try:
                        value=json.loads(line)
                        if isinstance(value,dict): messages.put(value)
                    except ValueError: pass
            finally:
                messages.put(None)
        reader=threading.Thread(target=read,daemon=True); reader.start()
        deadline=time.monotonic()+timeout
        def send(value):
            process.stdin.write(json.dumps(value)+'\n'); process.stdin.flush()
        def receive(identity):
            while True:
                try: value=messages.get(timeout=max(0.01,deadline-time.monotonic()))
                except queue.Empty: raise UsageError('Usage lookup timed out. Try !usage again shortly.')
                if value is None: raise UsageError('Codex usage service closed before responding. Check Codex sign-in and try !usage again.')
                if value.get('id')==identity:
                    if 'error' in value:
                        raise UsageError('Codex could not read account usage. Check sign-in and network access in Codex, then try !usage again. No allowance was assumed.')
                    return value.get('result')
                if time.monotonic()>=deadline: raise UsageError('Usage lookup timed out. Try !usage again shortly.')
        send({'id':1,'method':'initialize','params':{'clientInfo':{'name':'discord_usage','version':'1.0'},'capabilities':{'experimentalApi':True}}})
        receive(1)
        send({'method':'initialized','params':{}})
        send({'id':2,'method':'account/rateLimits/read','params':{}})
        result=receive(2)
        if not isinstance(result,dict): raise UsageError('Codex returned no usage information. Try !usage again later.')
        return result
    except (OSError,subprocess.SubprocessError) as exc:
        raise UsageError('Could not open the local Codex usage service. Check that Codex is installed and signed in.') from exc
    finally:
        if process:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
            if process.stdin: process.stdin.close()
            if process.stdout: process.stdout.close()


def format_usage(result):
    buckets=result.get('rateLimitsByLimitId')
    if not isinstance(buckets,dict) or not buckets:
        legacy=result.get('rateLimits')
        buckets={legacy.get('limitId') or 'codex':legacy} if isinstance(legacy,dict) else {}
    buckets={key:value for key,value in buckets.items() if isinstance(value,dict)
             and key not in ('codex_bengalfox','spark')
             and 'spark' not in str(value.get('limitName','')).lower()}
    lines=['Account-wide allowance, shared across tasks.']
    for key,bucket in list(buckets.items())[:8]:
        if not isinstance(bucket,dict): continue
        name=bucket.get('limitName') or ('Codex' if key=='codex' else str(key))
        lines.append('\n**'+str(name).replace('@','@\u200b').replace('`','')[:100]+'**')
        found=False
        for slot in ('primary','secondary'):
            window=bucket.get(slot)
            if not isinstance(window,dict): continue
            found=True
            duration=window.get('windowDurationMins')
            label={300:'5-hour',10080:'Weekly',1440:'Daily'}.get(duration)
            if not label: label=str(duration)+'-minute' if type(duration) is int and duration>0 else slot.capitalize()
            used=window.get('usedPercent')
            if type(used) in (int,float) and math.isfinite(used):
                remaining=max(0,min(100,100-used))
                value=f'{remaining:g}% remaining'
            else: value='remaining allowance unavailable'
            reset=window.get('resetsAt')
            if type(reset) in (int,float) and math.isfinite(reset) and 0<reset<253402300800:
                value+=f' · resets <t:{int(reset)}:f> (<t:{int(reset)}:R>)'
            else: value+=' · reset time unavailable'
            lines.append(label+': '+value)
        if not found: lines.append('Usage windows unavailable.')
        if bucket.get('spendControlReached') is True: lines.append('Spending limit reached; review usage settings in Codex.')
    if not buckets: lines.append('Usage information unavailable; no remaining allowance can be inferred.')
    lines.append('\nChecked <t:'+str(int(time.time()))+':T>. This command does not spend credits or redeem a reset.')
    return '\n'.join(lines)[:3900]
