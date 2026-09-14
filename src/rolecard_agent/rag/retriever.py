"""Chunk -> embed -> Chroma -> similarity search.

Deliberately one module, not an abstraction layer. Exposed to the agent as the
search_knowledge tool. Same code serves every domain via the collection argument.
"""
