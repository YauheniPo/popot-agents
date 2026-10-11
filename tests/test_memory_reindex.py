import tempfile
import unittest
from pathlib import Path

from memory.reindex import apply_archive


class ReindexTests(unittest.TestCase):
    def test_validation_precedes_deletion_and_failed_import_is_resumable(self):
        calls = []
        class Client:
            def remember(self, text, dataset, **kwargs):
                calls.append(('import', text))
                if text == 'second' and len(calls) == 4:
                    raise RuntimeError('indexing failed')
                return {'status': 'completed'}
        archive = {'datasets': [{'id': 'd', 'name': 'p', 'documents': [
            {'row': {'id': 'a'}, 'text': 'first'}, {'row': {'id': 'b'}, 'text': 'second'}]}]}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'backup.json'
            def delete(dataset, doc): calls.append(('delete', doc))
            with self.assertRaises(RuntimeError):
                apply_archive(archive, path, Client(), delete)
            self.assertTrue(path.exists())
            self.assertEqual(calls[:2], [('delete', 'a'), ('delete', 'b')])
            apply_archive(archive, path, Client(), delete)
            self.assertEqual(calls.count(('delete', 'a')), 1)
            self.assertEqual(calls.count(('import', 'first')), 1)
            self.assertTrue(all(d['imported'] for d in archive['datasets'][0]['documents']))

    def test_invalid_legacy_record_prevents_all_deletes(self):
        archive = {'datasets': [{'id': 'd', 'name': 'p', 'documents': [
            {'row': {'id': 'a'}, 'text': '{"memories": []}'}]}]}
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                apply_archive(archive, Path(folder) / 'backup.json', None,
                              lambda *args: self.fail('deleted before validation'))
