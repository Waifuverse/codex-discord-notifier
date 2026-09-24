import io
import json
from pathlib import Path
import struct
import tempfile
import subprocess
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch
import zlib

from PIL import Image
import media_support as media
import codex_transport
from bridge_store import Store


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_image_compression_enforces_actual_byte_limit(self):
        image=Image.effect_noise((1000,1000),100).convert('RGB')
        result=media.save_image(image,self.root/'image.png',200_000)
        self.assertLessEqual(result.stat().st_size,200_000)
        with Image.open(result) as actual:
            actual.verify()

    def test_non_image_payload_is_rejected(self):
        with self.assertRaises(media.MediaError):
            media.normalize_image(io.BytesIO(b'<svg><script>bad</script></svg>'),self.root/'bad')

    def test_large_dimensions_rejected_before_decoding_pixels(self):
        def chunk(name,data):
            return struct.pack('>I',len(data))+name+data+struct.pack('>I',zlib.crc32(name+data))
        source=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',7000,6000,8,2,0,0,0))+chunk(b'IDAT',b'')+chunk(b'IEND',b'')
        with self.assertRaisesRegex(media.MediaError,'dimensions'):
            media.normalize_image(io.BytesIO(source),self.root/'bad')

    def test_image_exif_metadata_is_removed(self):
        image=Image.new('RGB',(20,20),'orange'); exif=Image.Exif(); exif[270]='private metadata'
        source=self.root/'source.jpg'; image.save(source,exif=exif)
        result=media.normalize_image(source,self.root/'clean')
        with Image.open(result) as cleaned:
            self.assertEqual(dict(cleaned.getexif()),{})

    def test_monitor_and_region_bounds(self):
        screen=NS(monitors=[{},dict(left=3440,top=0,width=1920,height=1080)])
        self.assertEqual(media.screen_region(screen,1,(20,30,400,300)),dict(left=3460,top=30,width=400,height=300))
        for monitor,region in [(0,None),(2,None),(1,(-1,0,100,100)),(1,(1800,0,400,100))]:
            with self.assertRaises(media.MediaError): media.screen_region(screen,monitor,region)

    def test_clip_duration_rejected_before_capture(self):
        for seconds in (0,31,100):
            with self.assertRaises(media.MediaError): media.record_clip(seconds)

    def test_video_transcode_meets_byte_budget(self):
        source=self.root/'motion.mp4'
        subprocess.run([media.encoder(),'-hide_banner','-loglevel','error','-y',
            '-f','lavfi','-i','testsrc2=size=320x240:rate=15','-t','1',
            '-c:v','libx264','-b:v','2000000',str(source)],check=True,capture_output=True,timeout=20,creationflags=media.HIDDEN)
        with patch.object(media,'OUTGOING',self.root/'output'):
            result=media.fit_video(source,80_000)
        self.assertLessEqual(result.stat().st_size,80_000)
        self.assertAlmostEqual(media.video_duration(result),1,delta=0.15)

    def test_capture_command_parsing(self):
        self.assertEqual(media.parse_capture_command('!screenshot 2'),{'kind':'screenshot','monitor':2})
        self.assertEqual(media.parse_capture_command('!clip 15 1'),{'kind':'clip','seconds':15,'monitor':1})
        self.assertIsNone(media.parse_capture_command('please show game'))
        for text in ('!clip 999','!clip abc','!screenshot 1 arbitrary'):
            with self.assertRaises(media.MediaError): media.parse_capture_command(text)

    def test_image_paths_survive_inbox_restart(self):
        store=Store(self.root/'store.sqlite')
        store.enqueue('1','2',{'thread_id':'task','cwd':'.'},'inspect',['C:/one.png','C:/two.jpg'])
        self.assertEqual(json.loads(Store(store.path).rows(('pending',))[0]['images_json']),['C:/one.png','C:/two.jpg'])

    def test_codex_queue_instructs_task_to_view_validated_image(self):
        path=self.root/'image.png'; Image.new('RGB',(5,5)).save(path)
        tid='22222222-2222-4222-8222-222222222222'
        row={'thread_id':tid,'message_id':'1','text':'inspect','images_json':json.dumps([str(path)])}
        with patch.object(codex_transport,'lookup_thread',return_value={'cwd':str(self.root),'archived':0}), \
             patch('app_transport.deliver',return_value=('submitted','sent',None)) as deliver:
            self.assertEqual(codex_transport.dispatch(row)[0],'submitted')
        self.assertIn(str(path.resolve()),deliver.call_args.args[1])
        self.assertIn('Open each image using view_image',deliver.call_args.args[1])

    def test_managed_output_does_not_allow_arbitrary_file_upload(self):
        file=self.root/'secret.txt'; file.write_text('private')
        with self.assertRaises(media.MediaError): media.managed_path(file)


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_untrusted_url_is_never_requested(self):
        for url in ('http://cdn.discordapp.com/x','https://evil.example/a.png','https://cdn.discordapp.com.evil.example/a'):
            attachment=NS(id=1,size=1,url=url)
            with self.assertRaises(media.MediaError): await media.download_image(attachment,'2',None)

    async def test_count_and_total_limits_before_download(self):
        with self.assertRaises(media.MediaError): await media.download_images([NS(size=1)]*5,'1')
        with self.assertRaises(media.MediaError): await media.download_images([NS(size=media.INBOUND_TOTAL+1)],'1')

    async def test_stream_byte_limit_cannot_be_bypassed_by_declared_size(self):
        class Content:
            async def iter_chunked(self,n):
                yield b'123'; yield b'456'
        class Response:
            status=200; content_length=None; content=Content()
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
        with tempfile.TemporaryDirectory() as tmp:
            attachment=NS(id=1,size=1,url='https://cdn.discordapp.com/attachments/a.png')
            session=NS(get=lambda *args,**kwargs:Response())
            with patch.object(media,'INCOMING',Path(tmp)),patch.object(media,'INBOUND_LIMIT',4):
                with self.assertRaises(media.MediaError): await media.download_image(attachment,'2',session)
            self.assertEqual(list(Path(tmp).rglob('*.download')),[])


if __name__=='__main__': unittest.main()
