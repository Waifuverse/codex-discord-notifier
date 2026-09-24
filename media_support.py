"""Bounded, on-demand screen media and validated Discord image downloads."""
from __future__ import annotations

import asyncio
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import uuid
import warnings
import threading
from urllib.parse import urlparse

import aiohttp
from PIL import Image, ImageOps, UnidentifiedImageError
import imageio_ffmpeg
import mss

from bridge_store import ROOT

MEDIA_ROOT = ROOT / '.state' / 'media'
INCOMING = MEDIA_ROOT / 'incoming'
OUTGOING = MEDIA_ROOT / 'outgoing'
UPLOAD_LIMIT = 9 * 1024 * 1024  # Conservative headroom below the 10 MiB bot baseline.
INBOUND_LIMIT = 20 * 1024 * 1024
INBOUND_TOTAL = 40 * 1024 * 1024
MAX_IMAGES = 4
MAX_PIXELS = 40_000_000
CDN_HOSTS = {'cdn.discordapp.com', 'media.discordapp.net', 'cdn.discord.com'}
HIDDEN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


class MediaError(ValueError):
    pass


def managed_path(path):
    path = Path(path).resolve()
    if not path.is_relative_to(MEDIA_ROOT.resolve()) or not path.is_file():
        raise MediaError('Media file is missing or outside the managed media directory.')
    return path


def output_path(suffix):
    OUTGOING.mkdir(parents=True, exist_ok=True)
    return OUTGOING / (uuid.uuid4().hex + suffix)


def save_image(image, destination, limit=UPLOAD_LIMIT):
    """Strip metadata and fit an image below the byte cap; keep PNG when possible."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    image = ImageOps.exif_transpose(image).convert('RGB')
    image.thumbnail((2560, 2560), Image.Resampling.LANCZOS)
    png = destination.with_suffix('.png')
    image.save(png, 'PNG', optimize=True)
    if png.stat().st_size <= limit:
        return png
    png.unlink()
    jpeg = destination.with_suffix('.jpg')
    for size in (2560, 1920, 1280, 960, 640):
        image.thumbnail((size, size), Image.Resampling.LANCZOS)
        for quality in (85, 70, 55, 40):
            image.save(jpeg, 'JPEG', quality=quality, optimize=True)
            if jpeg.stat().st_size <= limit:
                return jpeg
    jpeg.unlink(missing_ok=True)
    raise MediaError('Image could not be compressed below the upload limit.')


def normalize_image(source, destination, limit=UPLOAD_LIMIT):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(source) as image:
                if image.format not in ('PNG', 'JPEG', 'WEBP', 'GIF'):
                    raise MediaError('Supported image formats are PNG, JPEG, WebP, and GIF (first frame).')
                if image.width * image.height > MAX_PIXELS:
                    raise MediaError('Image dimensions exceed the 40-megapixel limit.')
                image.load()
                return save_image(image, destination, limit)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise MediaError('Attachment is not a valid supported image, or its dimensions are too large.') from exc


async def download_image(attachment, message_id, session):
    if not str(message_id).isdigit() or not str(attachment.id).isdigit():
        raise MediaError('Invalid attachment identity.')
    if attachment.size > INBOUND_LIMIT:
        raise MediaError('Each incoming image must be 20 MiB or smaller.')
    url = urlparse(attachment.url)
    if url.scheme != 'https' or url.hostname not in CDN_HOSTS or url.username or url.password:
        raise MediaError('Attachment URL is not a Discord CDN URL.')
    destination = INCOMING / str(message_id) / str(attachment.id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.download')
    try:
        async with session.get(attachment.url, allow_redirects=False) as response:
            if response.status != 200:
                raise MediaError('Could not download the image. Please send it again.')
            if response.content_length and response.content_length > INBOUND_LIMIT:
                raise MediaError('Incoming image exceeds 20 MiB.')
            size = 0
            with temporary.open('wb') as handle:
                async for chunk in response.content.iter_chunked(64 * 1024):
                    size += len(chunk)
                    if size > INBOUND_LIMIT:
                        raise MediaError('Incoming image exceeds 20 MiB.')
                    handle.write(chunk)
        return str(await asyncio.to_thread(normalize_image, temporary, destination))
    finally:
        temporary.unlink(missing_ok=True)


async def download_images(attachments, message_id):
    if len(attachments) > MAX_IMAGES:
        raise MediaError('Send at most four images per reply.')
    if sum(a.size for a in attachments) > INBOUND_TOTAL:
        raise MediaError('Images in one reply must total 40 MiB or less.')
    paths = []
    try:
        timeout = aiohttp.ClientTimeout(total=60, connect=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for attachment in attachments:
                paths.append(await download_image(attachment, message_id, session))
        return paths
    except Exception:
        for path in paths:
            managed_path(path).unlink(missing_ok=True)
        raise


def screen_region(screen, monitor=1, region=None):
    if monitor < 1 or monitor >= len(screen.monitors):
        raise MediaError('Monitor not found. Use media_cli.py monitors to list monitors.')
    bounds = {k:screen.monitors[monitor][k] for k in ('left','top','width','height')}
    if region:
        x, y, width, height = map(int, region)
        if x < 0 or y < 0 or width < 2 or height < 2 or x + width > bounds['width'] or y + height > bounds['height']:
            raise MediaError('Capture region must fit inside the selected monitor.')
        bounds.update(left=bounds['left'] + x, top=bounds['top'] + y, width=width, height=height)
    return bounds


def screenshot(monitor=1, region=None, limit=UPLOAD_LIMIT, window=None, _in_worker=False):
    from window_capture import WindowCapture, run_capture
    if window and not _in_worker:
        return run_capture('screenshot',dict(monitor=monitor,region=region,limit=limit,window=window))
    try:
        with (WindowCapture(window) if window else mss.MSS()) as screen:
            capture = screen.grab(screen_region(screen, monitor, region))
            image = Image.frombytes('RGB', capture.size, capture.rgb)
            return save_image(image, output_path('.png'), limit)
    except mss.exception.ScreenShotError as exc:
        raise MediaError('Screen capture failed. Windows must have an accessible signed-in desktop.') from exc


def encoder():
    return imageio_ffmpeg.get_ffmpeg_exe()


def bitrate(seconds, limit):
    return max(100_000, min(2_500_000, int(limit * 8 * 0.8 / seconds)))


def video_duration(path):
    result = subprocess.run([encoder(), '-hide_banner', '-i', str(path)],
                            capture_output=True, timeout=15, creationflags=HIDDEN)
    match = re.search(rb'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)', result.stderr)
    if not match:
        raise MediaError('Could not read video duration.')
    return int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])


def fit_video(source, limit=UPLOAD_LIMIT):
    duration = video_duration(source)
    if not 0 < duration <= 30.2:
        raise MediaError('Video clips must be between 1 and 30 seconds.')
    target = output_path('.mp4')
    for factor in (1.0, 0.65, 0.4):
        rate = int(bitrate(duration, limit) * factor)
        result = subprocess.run([encoder(), '-hide_banner', '-loglevel', 'error', '-y',
            '-i', str(source), '-an', '-vf', "scale=1280:720:force_original_aspect_ratio=decrease:force_divisible_by=2", '-r', '15',
            '-c:v', 'libx264', '-preset', 'veryfast', '-b:v', str(rate),
            '-maxrate', str(rate), '-bufsize', str(rate * 2), '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart', str(target)], capture_output=True, timeout=90, creationflags=HIDDEN)
        if result.returncode == 0 and 0 < target.stat().st_size <= limit:
            return target
    target.unlink(missing_ok=True)
    raise MediaError('Video could not be compressed below the upload limit.')


def record_clip(seconds=10, monitor=1, region=None, fps=15, limit=UPLOAD_LIMIT, window=None, _in_worker=False):
    if not 1 <= seconds <= 30 or not 5 <= fps <= 30:
        raise MediaError('Clips support 1–30 seconds and 5–30 frames per second.')
    from window_capture import WindowCapture, run_capture
    if window and not _in_worker:
        return run_capture('clip',dict(seconds=seconds,monitor=monitor,region=region,fps=fps,limit=limit,window=window))
    target = output_path('.mp4')
    process = None
    watchdog = None
    successful = False
    try:
        with (WindowCapture(window) if window else mss.MSS()) as screen:
            bounds = screen_region(screen, monitor, region)
            scale = min(1, 1280 / bounds['width'], 720 / bounds['height'])
            width = max(2, int(bounds['width'] * scale) // 2 * 2)
            height = max(2, int(bounds['height'] * scale) // 2 * 2)
            rate = bitrate(seconds, limit)
            process = subprocess.Popen([encoder(), '-hide_banner', '-loglevel', 'error', '-y',
                '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{width}x{height}', '-r', str(fps),
                '-i', 'pipe:0', '-an', '-c:v', 'libx264', '-preset', 'veryfast',
                '-b:v', str(rate), '-maxrate', str(rate), '-bufsize', str(rate * 2),
                '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(target)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=HIDDEN)
            watchdog = threading.Timer(seconds + 25, lambda: process.kill() if process.poll() is None else None)
            watchdog.daemon = True
            watchdog.start()
            start = time.monotonic()
            for frame in range(math.ceil(seconds * fps)):
                if time.monotonic() - start > seconds + 10:
                    raise MediaError('Capture was too slow. Try a shorter clip or lower frame rate.')
                capture = screen.grab(bounds)
                image = Image.frombytes('RGB', capture.size, capture.rgb).resize((width, height), Image.Resampling.BILINEAR)
                process.stdin.write(image.tobytes())
                delay = start + (frame + 1) / fps - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
            process.stdin.close()
            process.wait(timeout=20)
            if process.returncode or not target.is_file() or target.stat().st_size == 0:
                raise MediaError('Video encoding failed.')
        if target.stat().st_size > limit:
            smaller = fit_video(target, limit)
            target.unlink()
            successful = True
            return smaller
        successful = True
        return target
    except (mss.exception.ScreenShotError, OSError, subprocess.SubprocessError) as exc:
        raise MediaError('Screen recording failed. Use an accessible signed-in desktop.') from exc
    finally:
        if watchdog:
            watchdog.cancel()
        if process:
            if process.poll() is None:
                process.kill()
                process.wait()
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            if process.stderr:
                process.stderr.close()
        if not successful:
            target.unlink(missing_ok=True)


def prepare_file(source, limit=UPLOAD_LIMIT):
    source = Path(source).resolve()
    if not source.is_file() or source.stat().st_size > 200 * 1024 * 1024:
        raise MediaError('Source media must exist and be 200 MiB or smaller.')
    if source.suffix.lower() in ('.mp4', '.webm', '.mov', '.mkv'):
        return fit_video(source, limit)
    return normalize_image(source, output_path('.png'), limit)


def queue_file(store, row, path, caption=''):
    path = managed_path(path)
    if path.stat().st_size > UPLOAD_LIMIT:
        raise MediaError('Prepared media exceeds the 9 MiB upload target.')
    payload = {'_media_path': str(path), 'embeds': [{
        'title': 'Screen recording' if path.suffix == '.mp4' else 'Image from Codex',
        'description': caption[:1500] or f'{path.stat().st_size / 1024 / 1024:.2f} MiB',
        'color': 0x3498DB}], 'allowed_mentions': {'parse': []}}
    kind = 'media:' + uuid.uuid4().hex
    store.output(row, kind, payload)
    return row['message_id'] + ':' + kind


def parse_capture_command(text):
    # Preserve spaces in window titles without requiring shell quoting in Discord.
    match=re.fullmatch(r'!(screenshot|clip)\s+window\s+(.+)',text.strip(),re.I)
    if match:
        kind,selector=match.groups()
        return {'kind':kind.lower(),'window':selector.strip()}
    words = text.strip().split()
    if not words or words[0].lower() not in ('!screenshot', '!clip'):
        return None
    try:
        if words[0].lower() == '!screenshot' and len(words) <= 2:
            return {'kind':'screenshot','monitor':int(words[1]) if len(words)>1 else 1}
        if words[0].lower() == '!clip' and len(words) <= 3:
            seconds = int(words[1]) if len(words)>1 else 10
            monitor = int(words[2]) if len(words)>2 else 1
            if 1 <= seconds <= 30:
                return {'kind':'clip','seconds':seconds,'monitor':monitor}
    except ValueError:
        pass
    raise MediaError('Use !screenshot [monitor] or !clip [1–30 seconds] [monitor].')
