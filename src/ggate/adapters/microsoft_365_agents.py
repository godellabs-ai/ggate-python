"""Microsoft 365 Agents SDK middleware (public TurnContext hooks only).

The middleware covers message/invoke/event content and outbound sends/updates.
Model calls and tools inside the handler require their own framework adapter or
explicit scans. Remote attachment URLs are never fetched by this middleware.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from ..core.client import get_client
from ..core.event import event_context


def _get(value: Any, key: str, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)
    return json.dumps(value, ensure_ascii=False, default=str)


def activity_content(activity):
    """Extract visible text, card/invoke data, and attachment metadata without network I/O."""
    parts = [str(_get(activity, key)) for key in ("text", "speak") if _get(activity, key)]
    value = _get(activity, "value")
    if value is not None:
        parts.append(_json(value))
    attachments = []
    for attachment in _get(activity, "attachments", []) or []:
        content = _get(attachment, "content")
        if content is not None:
            parts.append(_json(content))
        attachments.append({
            "filename": _get(attachment, "name") or "attachment",
            "mime_type": _get(attachment, "content_type"),
            "source": "upload", "capture": "metadata_only",
        })
    return "\n".join(parts), attachments


class Microsoft365AgentsMiddleware:
    def __init__(self, *, sdk_client=None):
        self.sdk = sdk_client or get_client()

    async def on_turn(self, context, logic):
        activity = context.activity
        conversation = _get(activity, "conversation")
        conversation_id = _get(conversation, "id")
        channel = _get(activity, "channel_id") or "unknown"
        tenant = _get(conversation, "tenant_id") or ""
        turn_id = _get(activity, "id") or str(uuid.uuid4())
        metadata = {
            "framework": "microsoft-365-agents",
            "session_id": f"m365:{channel}:{tenant}:{conversation_id or turn_id}",
            "conversation_id": conversation_id,
            "correlation_id": turn_id,
            "request_id": turn_id,
            "source_detail": {
                "channel_id": channel, "tenant_id": tenant,
                "activity_type": _get(activity, "type"),
                "from_id": _get(_get(activity, "from_property"), "id"),
            },
        }
        enforce = self.sdk.config.mode == "sync"

        async def scan_outbound(outgoing):
            text, attachments = activity_content(outgoing)
            if text or attachments:
                await self.sdk.scan_response_async(
                    text, attachments=attachments, enforce=enforce, **metadata,
                )

        async def on_send(_context, activities, next_send):
            # Check the whole batch before delivering any item.
            for outgoing in activities:
                await scan_outbound(outgoing)
            return await next_send()

        async def on_update(_context, outgoing, next_update):
            await scan_outbound(outgoing)
            return await next_update()

        # Turn-local callbacks and ContextVar scope avoid cross-conversation metadata leaks.
        context.on_send_activities(on_send)
        context.on_update_activity(on_update)
        with event_context(**metadata):
            text, attachments = activity_content(activity)
            if text or attachments:
                await self.sdk.scan_prompt_async(
                    text, attachments=attachments, enforce=enforce, **metadata,
                )
            await logic(context)


def instrument(*, adapter=None, sdk_client=None):
    middleware = Microsoft365AgentsMiddleware(sdk_client=sdk_client)
    if adapter is not None:
        adapter.use(middleware)
    return middleware
