"""Long embedding inputs must not multiply through overlapping recursion."""
import asyncio
from collections import Counter
from statistics import mean
import unittest
from unittest.mock import AsyncMock

from memory.embedding_parts import balanced_split


class EmbeddingPartsTests(unittest.TestCase):
    def engine(self, limit=16):
        class Engine:
            parts = None

            async def embed_text(self, texts):
                if len(texts) == 1 and len(texts[0]) <= limit:
                    self.parts.append(texts[0])
                    return [[mean(map(ord, texts[0]))]]
                return await self.split(texts)

        engine = Engine()
        engine.parts = []
        engine.split = balanced_split(AsyncMock(side_effect=AssertionError('unexpected fallback'))).__get__(engine)
        return engine

    def test_large_text_is_covered_once_with_linear_embedding_work(self):
        engine = self.engine()
        text = 'x' * 512
        self.assertEqual(asyncio.run(engine.embed_text([text])), [[float(ord('x'))]])
        self.assertEqual(len(engine.parts), 32)
        self.assertEqual(Counter(''.join(engine.parts)), Counter(text))

    def test_unicode_and_uneven_halves_preserve_all_text_and_weighted_embedding(self):
        engine = self.engine(limit=7)
        text = 'abcdefé🧪' * 9 + '終'
        result = asyncio.run(engine.embed_text([text]))
        self.assertAlmostEqual(result[0][0], mean(map(ord, text)))
        self.assertEqual(Counter(''.join(engine.parts)), Counter(text))
        self.assertTrue(all(len(part) <= 7 for part in engine.parts))

    def test_batches_and_unsplittable_inputs_keep_native_behavior(self):
        original = AsyncMock(return_value=[[1.0], [2.0]])
        engine = object()
        split = balanced_split(original)
        for texts in (['left', 'right'], ['ab'], ['x'], ['']):
            self.assertEqual(asyncio.run(split(engine, texts)), [[1.0], [2.0]])
            self.assertEqual(original.await_args.args, (engine, texts))
        error = ValueError('native failure')
        original.side_effect = error
        pending = split(engine, ['x'])
        with self.assertRaises(ValueError) as caught:
            asyncio.run(pending)
        self.assertIs(caught.exception, error)

    def test_cancellation_closes_both_embedding_branches(self):
        async def run():
            ready = asyncio.Event()
            active = set()
            class Engine:
                async def embed_text(self, texts):
                    active.add(texts[0])
                    if len(active) == 2:
                        ready.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        active.remove(texts[0])
            engine = Engine()
            split = balanced_split(AsyncMock())
            task = asyncio.create_task(split(engine, ['abcd']))
            await asyncio.wait_for(ready.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(active, set())
        asyncio.run(run())
