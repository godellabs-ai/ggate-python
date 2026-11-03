"""SDK identity boundaries for Console AI access policies, using controlled scan responses."""
import asyncio
from types import SimpleNamespace

import pytest

import ggate
from ggate.adapters.openai import wrap_client
from ggate.core.client import Client
from ggate.core.config import Config
from ggate.core.decision import Decision


POLICY = {"id": "access-123", "name": "Engineering only", "description": "Application access"}


class Console:
    def __init__(self):
        self.events, self.locks, self.down, self.verdict = [], set(), False, "pass"

    def request(self, request):
        if request.get("op") != "scan_text":
            return {"kind": "ok"}
        if self.down:
            raise ConnectionError("console down")
        event = request["event"]
        self.events.append(event)
        if (event["identity"].get("user"), event["source"]["client"]) in self.locks:
            return {"verdict": "block" if event["phase"] == "pre" else "warn", "reason_codes": ["agent_contained"]}
        return {"verdict": self.verdict, "message": "Application access policy", "reason_codes": ["ai_access_restricted"],
                "policy_source": POLICY, "matched_policies": [POLICY]}

    async def request_async(self, request):
        await asyncio.sleep(0)
        return self.request(request)


def client(console, mode="sync"):
    return Client(Config.from_values(agent_name="Operations Assistant", team="Platform", user="service@example.com",
                                    mode=mode, cooldown=0), transport=console)


def test_concurrent_scopes_cover_every_surface_and_preserve_sdk_targets():
    console = Console()
    sdk = client(console)

    async def run(user):
        with ggate.session(user, user=user):
            await asyncio.sleep(0)
            options = {"framework": "langgraph", "enforce": True}
            await sdk.scan_prompt_async("q", **options)
            await sdk.scan_response_async("a", **options)
            await sdk.scan_tool_call_async("search", **options)
            await sdk.scan_tool_result_async("search", "found", **options)
            await sdk.scan_shell_async("pwd", **options)
            await sdk.scan_file_async("/tmp/report.txt", **options)
            await sdk.scan_web_async("https://example.com", **options)

    async def concurrent():
        await asyncio.gather(run("alice@example.com"), run("bob@example.com"))

    asyncio.run(concurrent())
    assert len(console.events) == 14
    for event in console.events:
        assert event["identity"]["user"] == event["session"]["session_id"]
        assert event["identity"]["agent_name"] == "Operations Assistant"
        assert event["identity"]["agent_source"] == "agent-framework"
        assert event["source"]["client"] == event["collector"]["labels"]["framework"] == "langgraph"
    sdk.scan_prompt("outside")
    assert console.events[-1]["identity"]["user"] == "service@example.com"


def test_override_anonymous_and_nested_scope_restoration():
    console = Console()
    sdk = client(console)
    with ggate.event_context(user=" alice@example.com "):
        sdk.scan_prompt("override", user="bob@example.com")
        sdk.scan_prompt("anonymous", user=None)
        sdk.scan_prompt("anonymous", user=" ")
        with pytest.raises(RuntimeError), ggate.event_context(user=None):
            sdk.scan_prompt("anonymous")
            raise RuntimeError("app error")
        sdk.scan_prompt("restored")
    sdk.scan_prompt("outside")
    assert [e["identity"].get("user") for e in console.events] == [
        "bob@example.com", None, None, None, "alice@example.com", "service@example.com"]
    with pytest.raises(TypeError):
        sdk.scan_prompt("bad", user=123)


def test_shared_openai_adapter_and_queued_responses_keep_user():
    console = Console()
    sdk = client(console)

    async def create(**kwargs):
        await asyncio.sleep(0)
        return {"choices": [{"message": {"content": "answer"}}]}

    api = wrap_client(SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), sdk_client=sdk)

    async def run(user):
        with ggate.event_context(user=user):
            await api.chat.completions.create(model="test", messages=[{"role": "user", "content": user}])

    async def concurrent():
        await asyncio.gather(run("alice@example.com"), run("bob@example.com"))

    asyncio.run(concurrent())
    assert sdk.flush()
    for user in ("alice@example.com", "bob@example.com"):
        assert [e["payload"]["surface"] for e in console.events if e["identity"].get("user") == user] == ["prompt", "response"]


@pytest.mark.parametrize("verdict", ["pass", "warn", "block"])
def test_access_decisions_retain_attribution_without_latching(verdict):
    console = Console()
    console.verdict = verdict
    sdk = client(console, "async")
    if verdict == "block":
        with pytest.raises(ggate.GgateBlockedError) as error:
            sdk.scan_prompt("q", user="alice@example.com", enforce=True)
        assert not isinstance(error.value, ggate.GgateAgentContainedError)
        decision = error.value.decision
    else:
        decision = sdk.scan_prompt("q", user="alice@example.com", enforce=True)
    assert decision.policy_source["id"] == POLICY["id"]
    assert decision.matched_policies == [POLICY]
    assert Decision.from_response(decision.to_json()).matched_policies == [POLICY]
    assert not sdk.contained
    console.verdict = "pass"
    assert sdk.scan_prompt("retry", user="alice@example.com", enforce=True).allowed


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("async_calls", [False, True])
def test_kill_switch_isolated_by_user_and_framework(mode, async_calls):
    console = Console()
    console.locks.add(("alice@example.com", "openai"))
    sdk = client(console, mode)

    async def scan(**options):
        if async_calls:
            return await sdk.scan_prompt_async("q", **options)
        return sdk.scan_prompt("q", **options)

    async def run():
        alice = {"user": "alice@example.com", "framework": "openai"}
        if mode == "sync":
            with pytest.raises(ggate.GgateAgentContainedError):
                await scan(**alice)
        else:
            with ggate.event_context(user=alice["user"]):
                await scan(framework="openai")
            assert sdk.flush()
        assert sdk.contained
        await scan(**{**alice, "user": "bob@example.com", "enforce": True})
        await scan(**{**alice, "framework": "langchain", "enforce": True})
        assert sdk.contained
        console.down = True
        with pytest.raises(ggate.GgateAgentContainedError):
            await scan(**{**alice, "user": "ALICE@example.com"})
        assert (await scan(**{**alice, "user": "bob@example.com", "enforce": True})).fail_open
        assert (await scan(**{**alice, "framework": "langchain", "enforce": True})).fail_open
        console.down = False
        console.locks.clear()
        await scan(**alice)
        assert not sdk.contained

    asyncio.run(run())


def test_old_console_compatibility_and_held_lock_attribution():
    assert Decision.from_response({"verdict": "pass"}).matched_policies == []
    lock = Decision.from_response({"verdict": "block", "reason_codes": ["agent_contained"], "matched_policies": [POLICY]})
    assert Decision.kill_switch_held(lock, "offline").matched_policies == [POLICY]
