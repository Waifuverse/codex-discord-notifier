"""Independent bridge health alerts; survives a stopped or wedged reply worker."""
import argparse
import hashlib
import json
import os
import time
from bridge_store import Store, ROOT
import notifier


def health(store, now=None):
    now=time.time() if now is None else now
    issues=[]
    if now-float(store.get('heartbeat','0'))>180:
        reason=store.get('startup_error','')
        if reason=='network':
            issues.append('The Discord listener has no fresh heartbeat because its connection failed (network/DNS/timeout). It automatically retries at intervals up to 60 seconds. Check the PC internet connection; opening Codex is not required to connect the bot.')
        elif reason=='authentication':
            issues.append('The Discord listener stopped because Discord rejected the bot login. Check the bot credentials on the PC, then restart the listener. Do not send the token in Discord.')
        elif reason=='unexpected':
            issues.append('The Discord listener stopped after an unexpected error. Check reply-bridge.log on the PC and restart the listener; opening Codex does not resolve a listener crash.')
        else:
            issues.append('Listener heartbeat is over 3 minutes old. Run manage_bridge.ps1 status, then start if stopped.')
    elif store.get('connected')!='true' and now-float(store.get('disconnected_since','0'))>180:
        issues.append('Discord Gateway has been disconnected for over 3 minutes. Replies may be delayed until reconnection.')
    with store.connect() as db:
        stale=db.execute('SELECT COUNT(*) FROM outbox WHERE sent_id IS NULL AND created<?',(now-300,)).fetchone()[0]
    if stale:
        issues.append('Some Discord outputs have been pending for over 5 minutes. Check bridge logs and upload status; do not resubmit blindly.')
    attention_error=float(store.get('attention_error_since','0'))
    if attention_error and now-attention_error>300:
        issues.append('The question/error watcher has been failing for over 5 minutes. Replies may still work, but new questions or task failures may not reach Discord. Check the bridge log and Codex task storage on the PC.')
    success=float(store.get('worker_success',store.get('heartbeat','0')))
    if now-success>300 and now-float(store.get('heartbeat','0'))<=180:
        issues.append('The reply worker has not completed a successful cycle in over 5 minutes. Check bridge logs.')
    return issues


def check(store, env, send=None, now=None):
    now=time.time() if now is None else now
    issues=health(store,now)
    fingerprint=hashlib.sha256(json.dumps(issues).encode()).hexdigest() if issues else ''
    previous=store.get('health_alert','')
    if fingerprint==previous: return {'healthy':not issues,'notified':False,'issues':issues}
    title='Discord bridge needs attention' if issues else 'Discord bridge recovered'
    description='\n'.join(issues) if issues else 'The listener is connected and no overdue outputs remain.'
    payload={'embeds':[{'title':title,'description':description,'color':0xE67E22 if issues else 0x2ECC71}],
             'allowed_mentions':{'parse':[]}}
    # Direct REST delivery deliberately does not depend on the listener/outbox.
    (send or notifier.post_discord)(payload,env)
    store.put('health_alert',fingerprint)
    store.put('health_alert_sent',now)
    return {'healthy':not issues,'notified':True,'issues':issues}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once',action='store_true',help='Read status without sending an alert')
    args=parser.parse_args(); store=Store()
    if args.once:
        print(json.dumps({'issues':health(store)},ensure_ascii=False)); return
    import msvcrt
    lock=(ROOT/'.state/health-watchdog.lock').open('a+b')
    if os.fstat(lock.fileno()).st_size==0: lock.write(b'0'); lock.flush()
    lock.seek(0)
    try: msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    except OSError: lock.close(); return
    try:
        # Allow startup/reconnect time before evaluating stale persisted heartbeat.
        time.sleep(180)
        while True:
            try:
                check(store,notifier.load_env())
                store.put('watchdog_success',time.time())
            except Exception as exc:
                # Offline/invalid credentials: retain incident for retry after recovery.
                notifier.log('Health alert check failed: '+type(exc).__name__)
            time.sleep(60)
    finally: lock.close()


if __name__=='__main__': main()
