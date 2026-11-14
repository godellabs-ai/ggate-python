"""Store facade that gates writes and withholds poisoned reads before delivery."""
import json
from ..core.client import get_client


class GuardedStore:
    """Wrap put/get and aput/aget. Namespace/key/value arguments pass through unchanged.

    Batch and delete APIs are intentionally not exposed by this facade.
    """
    def __init__(self, store, *, sdk_client=None, **metadata):
        self._store = store
        self._sdk = sdk_client or get_client()
        self._metadata = {"framework": "memory", **metadata, "enforce": True}

    def put(self, *args, **kwargs):
        self._sdk.scan_memory_write({"args": args, "kwargs": kwargs}, operation="put", **self._metadata)
        return self._store.put(*args, **kwargs)

    async def aput(self, *args, **kwargs):
        await self._sdk.scan_memory_write_async({"args": args, "kwargs": kwargs}, operation="put", **self._metadata)
        return await self._store.aput(*args, **kwargs)

    def get(self, *args, **kwargs):
        result = self._store.get(*args, **kwargs)
        self._sdk.scan_tool_result("memory.get", json.dumps(result, default=str), before_delivery=True, **self._metadata)
        return result

    async def aget(self, *args, **kwargs):
        result = await self._store.aget(*args, **kwargs)
        await self._sdk.scan_tool_result_async("memory.get", json.dumps(result, default=str), before_delivery=True, **self._metadata)
        return result


def guard_store(store, *, sdk_client=None, **metadata):
    return GuardedStore(store, sdk_client=sdk_client, **metadata)
