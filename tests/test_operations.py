import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch,AsyncMock

from bridge_store import Store
from media_retention import cleanup
from health_watchdog import health,check
from window_capture import select_window,run_capture
from media_support import MediaError,parse_capture_command


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.media=self.root/'media'; self.media.mkdir()
        self.store=Store(self.root/'test.sqlite'); self.now=2_000_000
        self.row=dict(message_id='1',channel_id='2',thread_id='task',cwd='.')

    def tearDown(self): self.tmp.cleanup()

    def file(self,name,age=10):
        path=self.media/name; path.write_bytes(b'pixels')
        os.utime(path,(self.now-age*86400,self.now-age*86400)); return path

    def test_retention_deletes_old_keeps_recent_and_external(self):
        old=self.file('old.png'); recent=self.file('new.png',1)
        external=self.root/'external.png'; external.write_text('keep')
        result=cleanup(self.store,self.media,now=self.now)
        self.assertEqual(result['deleted'],1)
        self.assertFalse(old.exists()); self.assertTrue(recent.exists()); self.assertTrue(external.exists())

    def test_retention_protects_pending_upload_and_incoming_uncertain(self):
        upload=self.file('upload.png'); incoming=self.file('incoming.png')
        self.store.output(self.row,'media',{'_media_path':str(upload)})
        self.store.enqueue('1','2',self.row,'image',[str(incoming)])
        self.store.update('1','uncertain')
        result=cleanup(self.store,self.media,now=self.now)
        self.assertEqual(result['protected'],2)
        self.assertTrue(upload.exists()); self.assertTrue(incoming.exists())

    def test_retention_dry_run_disabled_and_bad_config(self):
        old=self.file('old.png')
        self.assertEqual(cleanup(self.store,self.media,now=self.now,dry_run=True)['deleted'],1)
        self.assertTrue(old.exists())
        self.assertTrue(cleanup(self.store,self.media,days=0)['disabled'])
        with self.assertRaises(ValueError): cleanup(self.store,self.media,days=-1)

    def test_retention_completed_input_kept_for_grace_period(self):
        incoming=self.file('incoming.png')
        self.store.enqueue('1','2',self.row,'image',[str(incoming)])
        self.store.update('1','completed')
        with self.store.connect() as db: db.execute('UPDATE inbox SET updated=?',(self.now-86400,))
        self.assertEqual(cleanup(self.store,self.media,now=self.now)['protected'],1)
        with self.store.connect() as db: db.execute('UPDATE inbox SET updated=?',(self.now-10*86400,))
        self.assertEqual(cleanup(self.store,self.media,now=self.now)['deleted'],1)

    def healthy(self):
        self.store.put('heartbeat',self.now); self.store.put('connected','true')
        self.store.put('worker_success',self.now)

    def test_alert_once_per_incident_and_once_on_recovery(self):
        send=Mock(); self.store.put('heartbeat',self.now-400)
        self.assertTrue(check(self.store,{},send,self.now)['notified'])
        self.assertFalse(check(self.store,{},send,self.now+10)['notified'])
        self.healthy()
        self.assertTrue(check(self.store,{},send,self.now)['notified'])
        self.assertFalse(check(self.store,{},send,self.now)['notified'])
        self.assertEqual(send.call_count,2)

    def test_failed_alert_delivery_retried_without_marking_delivered(self):
        with self.assertRaises(OSError): check(self.store,{},Mock(side_effect=OSError()),self.now)
        self.assertIsNone(self.store.get('health_alert'))
        self.assertTrue(check(self.store,{},Mock(),self.now)['notified'])

    def test_healthy_start_does_not_send(self):
        self.healthy(); send=Mock()
        self.assertFalse(check(self.store,{},send,self.now)['notified']); send.assert_not_called()

    def test_short_gateway_disconnect_is_quiet_but_persistent_one_alerts(self):
        self.healthy()
        self.store.put('connected','false')
        self.store.put('disconnected_since',self.now-60)
        self.assertEqual(health(self.store,self.now),[])
        self.store.put('disconnected_since',self.now-181)
        self.assertTrue(any('Gateway' in issue for issue in health(self.store,self.now)))

    def test_stuck_upload_detected_independently_of_fresh_heartbeat(self):
        self.healthy(); self.store.output(self.row,'media',{})
        with self.store.connect() as db: db.execute('UPDATE outbox SET created=?',(self.now-301,))
        self.assertTrue(any('outputs' in x for x in health(self.store,self.now)))

    def test_window_selection_rejects_ambiguity_and_minimized(self):
        candidates=[dict(handle=1,title='Example game',minimized=False),dict(handle=2,title='Example editor',minimized=False)]
        with self.assertRaises(MediaError): select_window('Example',candidates)
        self.assertEqual(select_window('1',candidates)['title'],'Example game')
        self.assertEqual(select_window('Example game',candidates)['handle'],1)
        candidates[0]['minimized']=True
        with self.assertRaises(MediaError): select_window('1',candidates)

    def test_window_command_preserves_title_with_spaces(self):
        self.assertEqual(parse_capture_command('!screenshot window Example game'),{'kind':'screenshot','window':'Example game'})
        self.assertEqual(parse_capture_command('!clip window 123'),{'kind':'clip','window':'123'})

    def test_window_hang_is_bounded_without_desktop_fallback(self):
        import subprocess
        with patch('window_capture.subprocess.run',side_effect=subprocess.TimeoutExpired('capture',45)):
            with self.assertRaisesRegex(MediaError,'timed out'): run_capture('screenshot',{'window':'Example'})


if __name__=='__main__': unittest.main()


class DeliveryIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_output_does_not_starve_later_messages(self):
        from reply_bridge import Bridge
        bridge=Mock()
        bridge.store.outputs.return_value=[{'id':'bad'},{'id':'good'}]
        bridge.flush_output=AsyncMock(side_effect=[OSError('network'),None])
        await Bridge.flush(bridge)
        self.assertEqual(bridge.flush_output.await_count,2)
