"""Explicit vector-store write facade; the application supplies the plaintext to scan."""
import inspect
from functools import wraps

from ..core.client import get_client


class GuardedVectorStore:
    """Gate a whole write before invoking the provider, preserving arguments and return values.

    content_of(operation, args, kwargs) must return the complete plaintext and metadata being
    persisted, as JSON-compatible data. Embeddings cannot be inspected for malicious instructions.
    Only the listed write methods are exposed; there is no unguarded attribute forwarding.
    """
    _writes = frozenset({"add", "upsert", "update", "add_texts", "add_documents",
                         "aadd", "aupsert", "aupdate", "aadd_texts", "aadd_documents"})

    def __init__(self, store, *, content_of, sdk_client=None, **metadata):
        self._store = store
        self._content_of = content_of
        self._sdk = sdk_client or get_client()
        self._metadata = {"framework": "memory", "store": "vector", **metadata, "enforce": True}

    def __getattr__(self, name):
        if name not in self._writes:
            raise AttributeError(name)
        method = getattr(self._store, name)
        if name in {"aadd", "aupsert", "aupdate", "aadd_texts", "aadd_documents"} or inspect.iscoroutinefunction(method):
            @wraps(method)
            async def async_write(*args, **kwargs):
                content = self._content_of(name, args, kwargs)
                await self._sdk.scan_memory_write_async(content, operation=name, **self._metadata)
                return await method(*args, **kwargs)
            return async_write

        @wraps(method)
        def write(*args, **kwargs):
            content = self._content_of(name, args, kwargs)
            self._sdk.scan_memory_write(content, operation=name, **self._metadata)
            return method(*args, **kwargs)
        return write


def guard_vector_store(store, *, content_of, sdk_client=None, **metadata):
    return GuardedVectorStore(store, content_of=content_of, sdk_client=sdk_client, **metadata)
