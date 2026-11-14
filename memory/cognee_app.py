"""Local adapters for the pinned Cognee app, preserving vendor sources."""
from cognee.infrastructure.databases.vector.embeddings.FastembedEmbeddingEngine import FastembedEmbeddingEngine

from embedding_parts import balanced_split

FastembedEmbeddingEngine._embed_in_parts = balanced_split(FastembedEmbeddingEngine._embed_in_parts)

from session_api import app
