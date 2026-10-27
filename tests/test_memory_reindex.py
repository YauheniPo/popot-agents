import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from memory.reindex import apply_archive, main


class ReindexTests(unittest.TestCase):
    def test_reindex_uses_safe_api_routes_and_resumes_completed_archive(self):
        client = MagicMock(api_url='http://127.0.0.1:8000', token='test-token')
        client._request.side_effect = [[{'id': 'dataset-id', 'name': 'p'}], {},
                                       [{'id': 'document-id', 'name': 'memory'}]]
        client.opener.open.return_value.__enter__.return_value.read.return_value = b'note'
        client.remember.return_value = {'status': 'completed'}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'backup.json'
            with patch('memory.reindex.CogneeMemory', return_value=client), \
                 patch.dict(os.environ, {'DEFAULT_USER_PASSWORD': 'test-password'}), \
                 patch('sys.argv', ['reindex', '--dataset', 'p', '--backup', str(path), '--apply']):
                main()
                requests = [call.args[0] for call in client.opener.open.call_args_list]
                self.assertEqual([(req.full_url, req.get_method()) for req in requests], [
                    ('http://127.0.0.1:8000/api/v1/datasets/dataset-id/data/document-id/raw', 'GET'),
                    ('http://127.0.0.1:8000/api/v1/datasets/dataset-id/data/document-id', 'DELETE'),
                ])
                self.assertTrue(json.loads(path.read_text())['datasets'][0]['documents'][0]['imported'])
                client._request.side_effect = [[{'id': 'dataset-id', 'name': 'p'}]]
                main()
                self.assertEqual(client.opener.open.call_count, 2)
                client.remember.assert_called_once_with('note', 'p', run_in_background=False)

    def test_unsafe_api_identifiers_are_rejected_before_followup_requests(self):
        for field in ('dataset', 'document'):
            for value in ('../other', 'id/other', 'id?query=1', '%2e%2e', '', None):
                with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as folder:
                    client = MagicMock(api_url='http://127.0.0.1:8000', token='test-token')
                    client.opener.open.return_value.__enter__.return_value.read.return_value = b'note'
                    client._request.side_effect = [
                        [{'id': value if field == 'dataset' else 'dataset-id', 'name': 'p'}],
                        {},
                        [{'id': value, 'name': 'memory'}],
                    ]
                    with patch('memory.reindex.CogneeMemory', return_value=client), \
                         patch.dict(os.environ, {'DEFAULT_USER_PASSWORD': 'test-password'}), \
                         patch('sys.argv', ['reindex', '--dataset', 'p', '--backup', str(Path(folder) / 'backup.json')]):
                        with self.assertRaisesRegex(ValueError, 'identifier'):
                            main()
                    self.assertEqual(client._request.call_count, 1 if field == 'dataset' else 3)
                    client.opener.open.assert_not_called()

    def test_invalid_backup_identifier_prevents_all_deletes(self):
        for field in ('dataset', 'document'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as folder:
                archive = {'datasets': [{'id': '../other' if field == 'dataset' else 'd',
                                        'name': 'p', 'documents': [
                    {'row': {'id': '../other' if field == 'document' else 'a'}, 'text': 'note'}]}]}
                delete = Mock()
                client = Mock()
                client.remember.return_value = {'status': 'completed'}
                path = Path(folder) / 'backup.json'
                with self.assertRaisesRegex(ValueError, 'identifier'):
                    apply_archive(archive, path, client, delete)
                delete.assert_not_called()
                client.remember.assert_not_called()
                self.assertFalse(path.exists())

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
