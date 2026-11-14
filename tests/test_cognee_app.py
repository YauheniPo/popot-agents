"""The pinned Cognee app adapter must bound vendor embedding calls."""
import importlib.util
import inspect
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from memory.embedding_parts import balanced_split

MODULE = Path(__file__).resolve().parents[1] / 'memory' / 'cognee_app.py'


class CogneeAppAdapterTests(unittest.TestCase):
    def test_adapter_binds_balanced_split_into_the_pinned_engine(self):
        engine = type('FastembedEmbeddingEngine', (), {
            '_embed_in_parts': staticmethod(lambda parts: list(parts))})
        vendor_method = engine._embed_in_parts
        engine_module = types.ModuleType(
            'cognee.infrastructure.databases.vector.embeddings.FastembedEmbeddingEngine')
        engine_module.FastembedEmbeddingEngine = engine
        session_api = types.ModuleType('session_api')
        session_api.app = object()
        stubs = {'embedding_parts': sys.modules['memory.embedding_parts'],
                 'session_api': session_api,
                 'cognee': types.ModuleType('cognee')}
        for name in ('cognee.infrastructure', 'cognee.infrastructure.databases',
                     'cognee.infrastructure.databases.vector',
                     'cognee.infrastructure.databases.vector.embeddings'):
            stubs[name] = types.ModuleType(name)
        stubs[engine_module.__name__] = engine_module
        with patch.dict(sys.modules, stubs):
            spec = importlib.util.spec_from_file_location('cognee_app_under_test', MODULE)
            loaded = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(loaded)
        self.assertIs(loaded.app, session_api.app)
        self.assertIsNot(engine._embed_in_parts, vendor_method)
        self.assertIs(engine._embed_in_parts.__wrapped__, vendor_method)
        self.assertTrue(inspect.iscoroutinefunction(engine._embed_in_parts))
        self.assertIs(balanced_split(vendor_method).__wrapped__, vendor_method)
