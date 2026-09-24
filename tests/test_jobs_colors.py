import sqlite3
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4
from jobs_status import read_jobs,format_jobs,JobsError
from thread_colors import thread_color,PALETTE,ERROR_COLOR,color_embeds

class JobsColorsTests(unittest.TestCase):
    def test_palette_unique_persistent_recycling_and_exhaustion(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'colors.sqlite'
            ids=[str(uuid4()) for _ in range(len(PALETTE)+1)]
            active=set(ids)
            values=[thread_color(i,registry=path,active_ids=active) for i in ids[:-1]]
            self.assertEqual(len(set(values)),len(PALETTE))
            self.assertNotIn(ERROR_COLOR,values)
            self.assertEqual(thread_color(ids[0],registry=path,active_ids=active),values[0])
            self.assertEqual(thread_color(ids[-1],registry=path,active_ids=active),0)
            self.assertEqual(thread_color(ids[-1],registry=path,active_ids=active-{ids[0]}),values[0])
            self.assertEqual(thread_color(ids[0],registry=path,active_ids=active),0)

    def test_error_override(self):
        self.assertEqual(color_embeds({'embeds':[{}]},str(uuid4()),error=True)['embeds'][0]['color'],ERROR_COLOR)

    def test_latest_turn_not_any_old_inprogress(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp)
            ids=[str(uuid4()) for _ in range(5)]
            with sqlite3.connect(home/'state_5.sqlite') as db:
                db.execute('CREATE TABLE threads(id,updated_at,archived,source)')
                db.executemany('INSERT INTO threads VALUES (?,?,?,?)',[(ids[0],100000,0,'cli'),(ids[1],100000,0,'vscode'),(ids[2],1,0,'cli'),(ids[3],100000,1,'cli'),(ids[4],100000,0,'subAgent')])
            db.close()
            with sqlite3.connect(home/'thread_history_1.sqlite') as db:
                db.execute('CREATE TABLE thread_turns(thread_id,status,started_at,completed_at,rollout_ordinal)')
                db.executemany('INSERT INTO thread_turns VALUES (?,?,?,?,?)',[(i,'inProgress',1,None,1) for i in ids])
                db.execute('INSERT INTO thread_turns VALUES (?,?,?,?,?)',(ids[1],'completed',99999,100000,2))
            db.close()
            jobs=read_jobs(home,now=100001)
            self.assertEqual([j['id'] for j in jobs],[ids[0],ids[2]])
            self.assertFalse(jobs[0]['stale']);self.assertTrue(jobs[1]['stale'])
            self.assertIn('1 task(s)',format_jobs(jobs))
            self.assertIn('1 unfinished',format_jobs(jobs))

    def test_missing_storage_is_error_not_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(JobsError):read_jobs(tmp)

class JobsCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_query_deduplicates_without_routing_or_model(self):
        from types import SimpleNamespace as NS
        from unittest.mock import patch,AsyncMock
        from reply_bridge import Bridge
        from bridge_store import Store
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'bridge.sqlite')
            bridge=Bridge({'DISCORD_DM_USER_ID':'1'},store)
            bridge.destination=NS(id=2)
            bridge.route_for=AsyncMock(side_effect=AssertionError('must not route'))
            message=NS(id=3,content=' !JOBS ',attachments=[],author=NS(id=1,bot=False),channel=NS(id=2),webhook_id=None)
            with patch('jobs_status.read_jobs',return_value=[]) as read:
                await bridge.ingest(message)
                await bridge.ingest(message)
                self.assertEqual(read.call_count,1)
            outputs=store.outputs()
            self.assertEqual(len(outputs),1)
            self.assertEqual(outputs[0]['thread_id'],'')
            bridge.route_for.assert_not_called()
            message.id=4;message.author.id=99
            with patch('jobs_status.read_jobs') as read:
                await bridge.ingest(message)
                read.assert_not_called()
            await bridge.close()
