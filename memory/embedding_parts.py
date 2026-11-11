"""Bound long-input Fastembed work without discarding text or editing vendor files."""
import asyncio
from functools import wraps


def balanced_split(original):
    @wraps(original)
    async def split(engine, original_texts):
        if len(original_texts) != 1 or len(original_texts[0]) < 3:
            return await original(engine, original_texts)
        text = original_texts[0]
        middle = len(text) // 2
        (left,), (right,) = await asyncio.gather(
            engine.embed_text([text[:middle]]),
            engine.embed_text([text[middle:]]),
        )
        return [[(a * middle + b * (len(text) - middle)) / len(text)
                 for a, b in zip(left, right, strict=True)]]
    return split
