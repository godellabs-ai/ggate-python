import asyncio
import json

import pytest
from microsoft_agents.activity import Activity, ConversationAccount, ChannelAccount, ResourceResponse
from microsoft_agents.hosting.core import TurnContext
from microsoft_agents.hosting.core.middleware_set import MiddlewareSet

import ggate
from ggate.core.client import Client
from ggate.core.config import Config
from ggate.core.decision import Decision


class Transport:
    def __init__(self):
        self.events = []

    def request(self, request):
        if request.get("op") == "collector_ready":
            return {}
        event = request["event"]
        self.events.append(event)
        # Gate only enforces SDK events while the caller still controls execution/delivery.
        blocked = "DENY" in json.dumps(event["payload"]) and event["phase"] == "pre"
        return {"verdict": "block" if blocked else "pass", "message": "policy decision"}

    async def request_async(self, request):
        await asyncio.sleep(0)
        return self.request(request)


class Adapter:
    def __init__(self):
        self.sent = []

    async def send_activities(self, context, activities):
        self.sent.extend(activities)
        return [ResourceResponse(id=str(i)) for i, _ in enumerate(activities)]

    async def update_activity(self, context, activity):
        self.sent.append(activity)
        return ResourceResponse(id=activity.id)


def setup(mode="sync"):
    transport = Transport()
    client = Client(Config.from_values(agent_name="M365 test", team="Tests", mode=mode), transport)
    middleware = ggate.instrument("microsoft-365-agents", sdk_client=client)
    return transport, client, middleware


def context(text="hello", conversation="conversation-1"):
    adapter = Adapter()
    activity = Activity(type="message", text=text, id="turn-1", channel_id="msteams", service_url="https://channel.example.test",
                        conversation=ConversationAccount(id=conversation, tenant_id="tenant-1"),
                        from_property=ChannelAccount(id="user-1"), recipient=ChannelAccount(id="agent-1"))
    return TurnContext(adapter, activity), adapter


def test_real_middleware_pipeline_and_turn_context_preserve_metadata():
    async def scenario():
        transport, client, middleware = setup()
        ctx, adapter = context()
        async def handler(ctx):
            await client.scan_tool_call_async("lookup", input_summary={"q": "ok"}, enforce=True)
            await ctx.send_activity("answer")
        await MiddlewareSet().use(middleware).receive_activity_with_status(ctx, handler)
        assert len(adapter.sent) == 1
        assert [e["payload"]["surface"] for e in transport.events] == ["prompt", "mcp_tool", "response"]
        assert all(e["phase"] == "pre" for e in transport.events)
        assert all(e["session"]["session_id"] == "m365:msteams:tenant-1:conversation-1" for e in transport.events)
        assert all(e["source"]["request_id"] == "turn-1" for e in transport.events)
        await client.scan_prompt_async("outside")
        assert transport.events[-1]["source"]["client"] == "generic"
    asyncio.run(scenario())


@pytest.mark.parametrize("incoming,outgoing,update", [("DENY", "ok", False), ("ok", "DENY", False), ("ok", "DENY", True)])
def test_block_stops_handler_send_and_update(incoming, outgoing, update):
    async def scenario():
        _, _, middleware = setup()
        ctx, adapter = context(incoming)
        entered = False
        async def handler(ctx):
            nonlocal entered
            entered = True
            if update:
                await ctx.update_activity(Activity(type="message", id="reply-1", text=outgoing))
            else:
                await ctx.send_activity(outgoing)
        with pytest.raises(ggate.GgateBlockedError):
            await middleware.on_turn(ctx, handler)
        assert adapter.sent == []
        assert entered == (incoming != "DENY")
    asyncio.run(scenario())


def test_batch_cards_and_invoke_values_cannot_bypass_checks():
    async def scenario():
        _, _, middleware = setup()
        ctx, adapter = context()
        async def handler(ctx):
            await ctx.send_activities([Activity(type="message", text="first"),
                                      Activity(type="message", value={"action": "DENY"})])
        with pytest.raises(ggate.GgateBlockedError):
            await middleware.on_turn(ctx, handler)
        assert adapter.sent == []
    asyncio.run(scenario())


def test_concurrent_turns_keep_tool_context_isolated():
    async def scenario():
        transport, client, middleware = setup()
        async def run(name):
            ctx, _ = context(conversation=name)
            async def handler(ctx):
                await asyncio.sleep(0)
                await client.scan_tool_call_async(name, enforce=True)
            await middleware.on_turn(ctx, handler)
        await asyncio.gather(run("one"), run("two"))
        tools = [e for e in transport.events if e["payload"]["surface"] == "mcp_tool"]
        assert all(e["session"]["session_id"].endswith(e["payload"]["tool"]) for e in tools)
    asyncio.run(scenario())


def test_explicit_enforcement_waits_even_when_default_is_audit():
    async def scenario():
        transport, client, _ = setup("async")
        for scan in [lambda: client.scan_prompt_async("DENY", enforce=True),
                     lambda: client.scan_response_async("DENY", enforce=True),
                     lambda: client.scan_tool_result_async("lookup", "DENY", enforce=True)]:
            with pytest.raises(ggate.GgateBlockedError):
                await scan()
        assert all(e["phase"] == "pre" for e in transport.events)
        await client.scan_response_async("old audit call", wait=True)
        assert transport.events[-1]["phase"] == "post"
    asyncio.run(scenario())


def test_media_gate_round_trip():
    value = {"verdict": "block", "media_gate": {"action": "block", "reason": "unsupported"}}
    assert Decision.from_response(value).to_json()["media_gate"] == value["media_gate"]


def test_framework_adapter_respects_audit_mode():
    _, client, _ = setup("async")
    handler = ggate.instrument("langchain", sdk_client=client)
    handler.on_llm_start({}, ["DENY"])
    handler.on_tool_start({"name": "tool"}, "DENY")
    assert client.flush()
