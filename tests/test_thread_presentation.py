import json
from pathlib import Path
import tempfile
import unittest
from thread_presentation import task_title

TID='11111111-1111-4111-8111-111111111111'


class SidebarTitleTests(unittest.TestCase):
    def test_sidebar_name_rename_partial_append_and_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'session_index.jsonl'
            original=json.dumps({'id':TID,'thread_name':'Find Discord notification bot'})+'\n'
            path.write_text(original,encoding='utf-8')
            self.assertEqual(task_title(TID,tmp),'Find Discord notification bot')
            path.write_text(original+json.dumps({'id':TID,'thread_name':'Renamed task'})+'\n{"id":',encoding='utf-8')
            self.assertEqual(task_title(TID,tmp),'Renamed task')
            path.write_text(json.dumps({'id':TID,'thread_name':'A'*100})+'\n',encoding='utf-8')
            self.assertEqual(task_title(TID,tmp),'A'*64)

    def test_missing_name_is_explicit_not_original_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(task_title(TID,tmp),'Codex task (title unavailable)')


if __name__=='__main__': unittest.main()
