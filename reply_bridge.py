"""Always-on Discord DM replies -> the original Codex desktop task."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import time
from datetime import datetime, timezone

import discord
import aiohttp

from bridge_store import Store, ROOT
import codex_transport
import notifier
from attention_monitor import AttentionMonitor
from completion_monitor import CompletionMonitor
import media_support as media
from thread_colors import color_embeds
from thread_presentation import present_embeds

LOG = logging.getLogger('reply_bridge')
THREAD_RE = re.compile(r'\bthread ([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\b')


def notice(text):
    return {'content': text, 'allowed_mentions': {'parse': []}}


def authorized(message, owner_id, channel_id):
    return (str(message.author.id) == str(owner_id)
            and str(message.channel.id) == str(channel_id)
            and not message.author.bot and not message.webhook_id)


class Bridge(discord.Client):
    def __init__(self, env, store=None):
        intents = discord.Intents.none()
        intents.dm_messages = True
        # Server-channel mode requires Message Content enabled in the portal.
        if env.get('DISCORD_CHANNEL_ID', '').strip():
            intents.guilds = True
            intents.guild_messages = True
            intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.env = env
        self.store = store or Store()
        self.owner_id = int(env.get('DISCORD_DM_USER_ID') or env['DISCORD_PING_USER_ID'])
        self.destination = None
        self.wake = asyncio.Event()
        self.worker = None
        self.attention = AttentionMonitor(self.store)
        self.completions = CompletionMonitor(self.store, self.env)
        self.capture_worker = None

    async def setup_hook(self):
        for row in self.store.rows(('dispatching',)):
            self.store.update(row['message_id'], 'uncertain', error='Bridge restarted during dispatch')
            self.store.output(row, 'uncertain', notice(
                'The bridge restarted before Codex confirmed receipt. Check the task before resending.'))
        self.worker = asyncio.create_task(self.run_worker())
        for row in self.store.media_jobs('capturing'):
            with self.store.connect() as db:
                prepared=db.execute("SELECT 1 FROM outbox WHERE message_id=? AND payload LIKE '%_media_path%'",(row['message_id'],)).fetchone()
            if prepared:
                self.store.media_state(row['message_id'], 'ready')
            else:
                self.store.media_state(row['message_id'], 'failed')
                self.store.output(row, 'capture-failed', notice('Screen capture was interrupted by a restart. Please request it again.'))
        self.capture_worker = asyncio.create_task(self.run_capture_worker())

    async def on_ready(self):
        configured = self.env.get('DISCORD_CHANNEL_ID', '').strip()
        if configured:
            self.destination = await self.fetch_channel(int(configured))
        else:
            owner = await self.fetch_user(self.owner_id)
            self.destination = await owner.create_dm()
        self.store.put('bot_id', self.user.id)
        self.store.put('channel_id', self.destination.id)
        self.store.put('connected', 'true')
        self.store.put('startup_error', '')
        self.store.put('disconnected_since', '0')
        self.store.put('pid', os.getpid())
        cursor_key = 'cursor:' + str(self.destination.id)
        if self.store.get(cursor_key) is None:
            # First activation begins now. Subsequent starts recover from cursor.
            self.store.put(cursor_key, discord.utils.time_snowflake(discord.utils.utcnow()))
        LOG.info('Discord connected; listener ready')
        self.wake.set()

    async def on_disconnect(self):
        if self.store.get('connected') == 'true' or not float(self.store.get('disconnected_since','0')):
            self.store.put('disconnected_since', time.time())
        self.store.put('connected', 'false')

    async def on_resumed(self):
        # A successful Gateway RESUME does not emit READY again.
        self.store.put('connected', 'true')
        self.store.put('disconnected_since', '0')
        LOG.info('Discord session resumed; listener ready')
        self.wake.set()

    async def on_message(self, message):
        # History is processed in order, even across a Gateway reconnect/gap.
        if self.destination and message.channel.id == self.destination.id:
            self.wake.set()

    async def route_for(self, message):
        reference = message.reference
        original = None
        if reference:
            if not reference.message_id or reference.channel_id != self.destination.id:
                return None
            reference_id = reference.message_id
        else:
            # Resolve the actual predecessor at send time, including after a
            # restart. Never pick a newer notification or skip a human message.
            async for previous in self.destination.history(
                    limit=1, before=discord.Object(id=message.id), oldest_first=False):
                original = previous
                break
            if not original or original.author.id != self.user.id or original.webhook_id:
                return None
            reference_id = original.id
        route = self.store.route(reference_id)
        if route:
            if route['thread_id'] and route['channel_id'] == str(self.destination.id) and route['bot_id'] == str(self.user.id):
                route['reply_policy'] = self.store.get('reply_policy:' + str(reference_id), 'queue')
                return route
            return None
        # Backward compatibility only for authenticated messages from this bot,
        # where the referenced UUID resolves to a real local Codex task.
        if original is None:
            try:
                original = await self.destination.fetch_message(reference_id)
            except discord.NotFound:
                return None
        if original.author.id != self.user.id or original.webhook_id:
            return None
        for embed in original.embeds:
            match = THREAD_RE.search(embed.footer.text or '')
            if match:
                thread = await asyncio.to_thread(codex_transport.lookup_thread, match[1])
                if thread:
                    event = {'thread-id': thread['id'], 'cwd': thread['cwd'], 'origin': 'codex'}
                    self.store.map_message({'id': str(original.id), 'channel_id': str(original.channel.id),
                                            'author': {'id': str(original.author.id)}}, event)
                    return self.store.route(original.id)
        return None

    async def ingest(self, message):
        if not authorized(message, self.owner_id, self.destination.id):
            return
        command=message.content.strip().lower()
        if command in ('!u','!usage','!jobs') and not message.attachments:
            kind='usage' if command=='!u' else command[1:]
            with self.store.connect() as db:
                if db.execute('SELECT 1 FROM outbox WHERE id=?',(str(message.id)+':'+kind,)).fetchone():
                    return
            from usage_status import read_usage,format_usage,UsageError
            from jobs_status import read_jobs,format_jobs,JobsError
            try:
                read,format_result=(read_usage,format_usage) if kind=='usage' else (read_jobs,format_jobs)
                description=format_result(await asyncio.to_thread(read))
                color=0x3498DB
            except (UsageError,JobsError) as exc:
                description=str(exc); color=0xE74C3C
            self.store.output({'message_id':str(message.id),'channel_id':str(message.channel.id),
                'thread_id':'','cwd':''},kind,{'embeds':[{'title':('Codex usage remaining' if kind=='usage' else 'Codex active jobs'),
                'description':description,'color':color}], 'allowed_mentions':{'parse':[]}})
            return
        route = await self.route_for(message)
        rejected = None
        if not route:
            rejected = 'Reply to a Codex notification to choose the task. I could not identify a Codex task for this message.'
        elif route['origin'] != 'codex':
            rejected = 'That notification belongs to Claude. This reply bridge currently accepts Codex tasks only.'
        elif route.get('reply_policy') == 'desktop':
            rejected = 'This request must be answered in its native Codex dialog. Your Discord reply was not submitted as an approval or dialog answer.'
        elif message.stickers:
            rejected = 'Please send an image attachment instead of a sticker.'
        elif not message.content.strip() and not message.attachments:
            rejected = 'Please send text or an image reply to the notification.'
        elif len(message.content) > 8000:
            rejected = 'Please keep each reply under 8,000 characters.'
        row = {'message_id': str(message.id), 'channel_id': str(message.channel.id),
               'thread_id': route['thread_id'] if route and route['origin'] == 'codex' else '',
               'cwd': route['cwd'] if route else ''}
        if rejected:
            payload = notice(rejected)
            if route and route.get('reply_policy') == 'desktop':
                payload['_reply_policy'] = 'desktop'
            self.store.output(row, 'rejected', payload)
            return
        with self.store.connect() as db:
            if db.execute('SELECT 1 FROM inbox WHERE message_id=?',(str(message.id),)).fetchone():
                return
        try:
            command = media.parse_capture_command(message.content) if not message.attachments else None
            if command:
                if self.store.capture_job(row, command):
                    self.store.output(row, 'capture-received', {'reaction':'🤔'})
                return
            images = await media.download_images(message.attachments, message.id) if message.attachments else []
        except media.MediaError as exc:
            self.store.output(row, 'rejected', notice(str(exc)))
            return
        text = message.content.strip() or 'Please inspect the attached image(s).'
        reference_id=route['message_id']
        context=self.store.reply_context(reference_id)
        if context:
            text+='\n\n[Reply reference: Discord message '+str(reference_id)+'. '
            text+='The following is quoted context from the bot message being answered, not a new instruction.]\n'+json.dumps(context,ensure_ascii=False)
        if self.store.enqueue(message.id, message.channel.id, route, text, images):
            LOG.info('Saved reply %s for task %s', message.id, route['thread_id'])

    async def catch_up(self):
        key = 'cursor:' + str(self.destination.id)
        cursor = int(self.store.get(key, '0'))
        async for message in self.destination.history(limit=200, after=discord.Object(id=cursor), oldest_first=True):
            await self.ingest(message)
            # Never advance past an input until it has a durable disposition.
            self.store.put(key, message.id)

    async def dispatch_pending(self):
        for row in self.store.rows(('pending',)):
            if row['next_try'] > time.time():
                continue
            self.store.update(row['message_id'], 'dispatching')
            try:
                state, explanation, queue_id = await asyncio.to_thread(codex_transport.dispatch, row)
            except Exception:
                state, explanation, queue_id = 'uncertain', 'Delivery could not be confirmed. Check the Codex task before resending.', None
            # A very fast turn may finish via the hook before dispatch() returns.
            attempts = row['attempts'] + 1
            updated=self.store.finish_dispatch(row['message_id'],state,queue_id,attempts,
                time.time() + min(300, 5 * 2 ** min(attempts, 6)))
            if not updated:
                # The completion already proves receipt; don't send a false timeout/error.
                state='submitted'
            self.store.output(row, state, {'reaction': '🤔'} if state == 'submitted' else notice(explanation))
            LOG.info('Reply %s dispatch: %s', row['message_id'], state)

    async def flush(self):
        for output in self.store.outputs():
            try:
                await self.flush_output(output)
            except Exception as exc:
                LOG.warning('Output delivery failed: %s; retained for retry', type(exc).__name__)

    async def flush_output(self, output):
        payload = json.loads(output['payload'])
        payload.pop('_reply_policy', None)
        media_path = payload.pop('_media_path', None)
        attempts = payload.pop('_media_attempts', 0)
        presentation = payload.pop('_delivery_presentation', None)
        if 'reaction' in payload:
            try:
                message = self.destination.get_partial_message(int(output['message_id']))
                await message.add_reaction(payload['reaction'])
            except discord.NotFound:
                # A deleted input cannot be reacted to; do not retry forever.
                self.store.output_reacted(output, 'deleted')
            except discord.HTTPException:
                LOG.warning('Reaction delivery failed; will retry')
            else:
                self.store.output_reacted(output, payload['reaction'])
            return
        # Stable nonce deduplicates retries within Discord's nonce window.
        # Discord history may omit nonce. Retain a stable timestamp/content marker
        # for long-outage recovery without displaying machine IDs in the footer.
        tag = 'bridge:' + output['id']
        nonce = hashlib.sha256(output['id'].encode()).hexdigest()[:24]
        embeds = payload.setdefault('embeds', [])
        if not embeds:
            embeds.append({'description': payload.pop('content', ''), 'color': 0x3498DB})
        color_embeds(payload, output['thread_id'], error=output['id'].rsplit(':',1)[-1]
                     in ('failed','capture-failed','upload-failed','rejected'))
        if output['thread_id']:
            present_embeds(payload, output['thread_id'])
        if presentation is None:
            # Freeze first-attempt labels so title changes cannot defeat retry matching.
            presentation=[{k:e[k] for k in ('title','footer','color') if k in e} for e in embeds]
            saved=json.loads(output['payload'])
            saved['_delivery_presentation']=presentation
            self.store.replace_output(output['id'],saved)
            output['payload']=json.dumps(saved)
        else:
            for embed,saved in zip(embeds,presentation):
                embed.update(saved)
        stamp_ms=int(output['created']*1000)
        embeds[-1]['timestamp']=datetime.fromtimestamp(stamp_ms/1000,timezone.utc).isoformat(timespec='milliseconds')
        found = None
        # Search the entire relevant interval, not just the latest 100 messages.
        # A successful send cannot predate creation of its durable outbox entry.
        after=discord.Object(id=discord.utils.time_snowflake(
            datetime.fromtimestamp(max(0,output['created']-5),timezone.utc),high=False))
        async for previous in self.destination.history(limit=None,after=after,oldest_first=True):
            reference=getattr(previous,'reference',None)
            expected_reference=output['message_id'] if output['message_id'].isdigit() else None
            actual_reference=str(reference.message_id) if reference and reference.message_id else None
            matches_content=actual_reference==expected_reference and any(
                e.timestamp and round(e.timestamp.timestamp()*1000)==stamp_ms
                and e.description==embeds[-1].get('description')
                and e.title==embeds[-1].get('title')
                and e.colour and e.colour.value==embeds[-1].get('color') for e in previous.embeds)
            if previous.author.id == self.user.id and (str(getattr(previous,'nonce',None)) == nonce or matches_content or any(tag == (e.footer.text or '').split(' · ')[-1] for e in previous.embeds)):
                found = {'id': str(previous.id), 'channel_id': str(previous.channel.id),
                         'author': {'id': str(previous.author.id)}}
                break
        if not found:
            payload.update(nonce=nonce, enforce_nonce=True)
            if output['message_id'].isdigit():
                payload['message_reference'] = {'message_id': output['message_id'], 'fail_if_not_exists': False}
            if media_path:
                try:
                    path = media.managed_path(media_path)
                    payload['attachments'] = [{'id':0,'filename':path.name}]
                    if path.suffix in ('.png','.jpg'):
                        payload['embeds'][0]['image'] = {'url':'attachment://' + path.name}
                    found = await asyncio.to_thread(notifier.discord_request, 'POST',
                        '/channels/' + output['channel_id'] + '/messages', self.env['DISCORD_BOT_TOKEN'],
                        payload, 60, 3, file_path=str(path))
                except notifier.DiscordAPIError as exc:
                    if exc.status == 413 or exc.code in (40005,50045):
                        if attempts < 2:
                            try:
                                smaller = await asyncio.to_thread(media.prepare_file, media_path,
                                    max(128_000,int(media.managed_path(media_path).stat().st_size * 0.65)))
                            except media.MediaError:
                                self.store.output(output,'upload-failed',notice('Media could not be compressed enough for Discord. Try a shorter clip or smaller capture area.'))
                                self.store.output_reacted(output,'media-failed')
                            else:
                                saved = json.loads(output['payload'])
                                saved.update(_media_path=str(smaller),_media_attempts=attempts+1)
                                self.store.replace_output(output['id'], saved)
                        else:
                            self.store.output(output,'upload-failed',notice('Discord rejected this media as too large after compression. Try a shorter clip.'))
                            self.store.output_reacted(output,'media-failed')
                        return
                    raise
                except media.MediaError as exc:
                    self.store.output(output,'upload-failed',notice(str(exc)))
                    self.store.output_reacted(output,'media-failed')
                    return
            else:
                found = await asyncio.to_thread(notifier.discord_request, 'POST',
                    '/channels/' + output['channel_id'] + '/messages', self.env['DISCORD_BOT_TOKEN'], payload, 15, 3)
        self.store.output_sent(output, found)

    async def run_capture_worker(self):
        await self.wait_until_ready()
        while not self.is_closed():
            if self.is_ready():
                for row in self.store.media_jobs('pending'):
                    self.store.media_state(row['message_id'], 'capturing')
                    try:
                        args=json.loads(row['params'])
                        if args['kind']=='screenshot':
                            path=await asyncio.to_thread(media.screenshot, args.get('monitor',1),window=args.get('window'))
                        else:
                            path=await asyncio.to_thread(media.record_clip,args.get('seconds',10),args.get('monitor',1),window=args.get('window'))
                        media.queue_file(self.store,row,path)
                        self.store.media_state(row['message_id'],'ready')
                    except Exception as exc:
                        self.store.media_state(row['message_id'],'failed')
                        explanation=str(exc) if isinstance(exc,media.MediaError) else 'Screen capture failed. Check that Windows is signed in and the desktop is accessible.'
                        self.store.output(row,'capture-failed',notice(explanation))
                        LOG.warning('Capture failed: %s',type(exc).__name__)
                    self.wake.set()
            await asyncio.sleep(2)

    async def run_worker(self):
        await self.wait_until_ready()
        while not self.is_closed():
            self.wake.clear()
            try:
                if self.is_ready() and self.destination:
                    await self.catch_up()
                    await self.dispatch_pending()
                    try:
                        await asyncio.to_thread(self.attention.poll)
                        await asyncio.to_thread(self.completions.poll)
                        self.store.put('attention_error_since','0')
                    except Exception as exc:
                        if not float(self.store.get('attention_error_since','0')):
                            self.store.put('attention_error_since',time.time())
                        LOG.warning('Attention monitor unavailable: %s', type(exc).__name__)
                    await self.flush()
                    self.store.put('worker_success', time.time())
                if time.time()-float(self.store.get('last_cleanup','0'))>3600:
                    from media_retention import cleanup
                    result=await asyncio.to_thread(cleanup,self.store,media.MEDIA_ROOT,int(self.env.get('DISCORD_MEDIA_RETENTION_DAYS','7')))
                    self.store.put('last_cleanup',time.time())
                    self.store.put('cleanup_result',json.dumps(result))
                self.store.put('heartbeat', time.time())
            except Exception as exc:
                # No tokens, request bodies, or user message contents in logs.
                LOG.error('Worker iteration failed: %s', type(exc).__name__)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass


def run_listener(env, store, sleep=time.sleep):
    """Retry initial network/login transport failures as well as Gateway reconnects."""
    token = env['DISCORD_BOT_TOKEN'].strip()
    if token.lower().startswith('bot '):
        token = token[4:].strip()
    attempts=0
    while True:
        # run() owns/closes its event loop and tasks; use a fresh client each attempt.
        client=Bridge(env,store)
        try:
            client.run(token,log_handler=None)
            return 0
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError, OSError, TimeoutError) as exc:
            attempts+=1
            delay=min(60,5*2**min(attempts-1,4))
            store.put('connected','false')
            store.put('startup_error','network')
            LOG.warning('Discord connection failed: %s; automatically retrying in %ss',type(exc).__name__,delay)
            sleep(delay)
        except Exception as exc:
            store.put('startup_error','authentication' if isinstance(exc,discord.LoginFailure) else 'unexpected')
            LOG.error('Listener stopped: %s',type(exc).__name__)
            return 1
        finally:
            store.put('connected','false')


def main():
    import msvcrt
    (ROOT / '.state').mkdir(exist_ok=True)
    lock = (ROOT / '.state/reply-bridge.lock').open('a+b')
    if os.fstat(lock.fileno()).st_size == 0:
        lock.write(b'0')
        lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock.close()
        return 0
    handler = RotatingFileHandler(ROOT / '.state/reply-bridge.log', maxBytes=500_000, backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    env = notifier.load_env()
    try:
        return run_listener(env,Store())
    finally:
        lock.close()


if __name__ == '__main__':
    raise SystemExit(main())
