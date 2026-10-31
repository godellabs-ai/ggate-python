"""One conversation is one session, each event holds only its own text, and the Kill Switch stops agents."""
import asyncio
import base64
from types import SimpleNamespace

import pytest

import ggate
from ggate import GgateAgentContainedError, GgateBlockedError
from ggate.adapters._common import (
    extract_attachments_from_value,
    latest_user_message,
    strip_prompt_echo,
    sync_around,
    template_user_text,
    wrap_method,
)
from ggate.adapters.autogen import _task_result_to_text, wrap_agent
from ggate.adapters.dspy import wrap_module
from ggate.adapters.haystack import _answer_text
from ggate.adapters.llamaindex import GgateLlamaIndexCallbackHandler
from ggate.adapters.openai import wrap_client
from ggate.core.client import Client
from ggate.core.config import Config


class Console:
    """Answers like the Console: a lock blocks a `pre` event and is recorded as `warn` on a `post` one."""

    def __init__(self):
        self.events, self.locked, self.down = [], False, False

    def request(self, request):
        if request.get("op") != "scan_text":
            return {"kind": "ok"}
        if self.down:
            raise ConnectionError("console down")
        event = request["event"]
        self.events.append(event)
        if self.locked:
            verdict = "block" if event["phase"] == "pre" else "warn"
            return {"verdict": verdict, "message": "AI agent is contained", "reason_codes": ["agent_contained"]}
        blocked = "DENY" in str(event["payload"]) and event["phase"] == "pre"
        return {"verdict": "block" if blocked else "pass", "message": "policy"}

    async def request_async(self, request):
        return self.request(request)

    def surfaces(self):
        return [event["payload"]["surface"] for event in self.events]

    def sessions(self):
        return {event["session"]["session_id"] for event in self.events}

    def texts(self):
        return [event["payload"].get("text") for event in self.events]


def client(console, mode="sync", **values):
    return Client(Config.from_values(agent_name="A", team="T", mode=mode, cooldown=0, **values), transport=console)


def message(role, content):
    return SimpleNamespace(type=role, content=content)


# ---- session grouping ---------------------------------------------------------------------------


def test_session_scope_covers_prompt_and_response_and_is_restored():
    console = Console()
    sdk = client(console)
    outside = sdk.scan_prompt("before") and console.events[-1]["session"]["session_id"]
    with ggate.session("chat-1") as bound:
        assert bound == "chat-1"
        sdk.scan_prompt("q")
        sdk.scan_response("a", wait=True)
    sdk.scan_prompt("after")
    assert [e["session"]["session_id"] for e in console.events] == [outside, "chat-1", "chat-1", outside]


def test_langgraph_nested_runs_share_the_outer_run_without_any_session_id():
    from langchain_core.outputs import Generation, LLMResult

    console = Console()
    handler = ggate.instrument("langgraph", sdk_client=client(console))
    handler.on_chain_start({}, {}, run_id="graph")
    handler.on_chain_start({}, {}, run_id="node", parent_run_id="graph")
    handler.on_chat_model_start({}, [[message("human", "hello")]], run_id="llm", parent_run_id="node")
    handler.on_llm_end(LLMResult(generations=[[Generation(text="hi")]]), run_id="llm", parent_run_id="node")
    handler.on_tool_start({"name": "search"}, "x", run_id="tool", parent_run_id="node")
    handler.on_tool_end("found", run_id="tool", parent_run_id="node")
    handler.on_chain_end({}, run_id="node")
    handler.on_chain_end({}, run_id="graph")
    assert console.sessions() == {"graph"}
    assert console.surfaces() == ["prompt", "response", "mcp_tool", "tool_result"]
    assert console.events[-1]["payload"]["tool"] == "search"
    assert not handler._runs


def test_static_instrument_session_yields_to_the_conversation_bound_at_call_time():
    console = Console()
    agent = wrap_agent(SimpleNamespace(run=lambda task=None: {"content": "done"}), sdk_client=client(console), session_id="static")
    agent.run(task="one")
    with ggate.session("request-7"):
        agent.run(task="two")
    assert console.events[0]["session"]["session_id"] == "static"
    assert console.events[-1]["session"]["session_id"] == "request-7"


def test_agno_prompt_and_response_share_a_session_named_on_the_run():
    from ggate.adapters.phidata import wrap_agent as wrap_agno

    console = Console()

    class Agent:
        session_id = "agent-own"

        def run(self, message, session_id=None):
            return SimpleNamespace(content="answer", session_id=session_id or self.session_id)

    agent = wrap_agno(Agent(), sdk_client=client(console))
    agent.run("q", session_id="chat-3")
    assert console.sessions() == {"chat-3"}
    console.events.clear()
    agent2 = wrap_agno(Agent(), sdk_client=client(console))
    agent2.run("q")
    assert console.sessions() == {"agent-own"}


# ---- each event holds only its own text ---------------------------------------------------------


def test_langchain_prompt_is_the_new_turn_not_the_history():
    from langchain_core.outputs import Generation, LLMResult

    console = Console()
    handler = ggate.instrument("langchain", sdk_client=client(console))
    history = [message("system", "rules"), message("human", "first"), message("ai", "old answer"), message("human", "second")]
    handler.on_chat_model_start({}, [history], run_id="r1")
    handler.on_llm_end(LLMResult(generations=[[Generation(text="new answer")]]), run_id="r1")
    assert console.texts() == ["second", "new answer"]


def test_langchain_tool_step_does_not_repeat_the_prompt():
    console = Console()
    handler = ggate.instrument("langchain", sdk_client=client(console))
    handler.on_chat_model_start({}, [[message("human", "q"), message("ai", ""), message("tool", "result")]], run_id="r2")
    assert console.events == []


def test_text_completion_model_records_user_words_and_answer_separately():
    from langchain_core.outputs import Generation, LLMResult

    console = Console()
    handler = ggate.instrument("langchain", sdk_client=client(console))
    raw = "<|im_start|>system\nrules<|im_end|>\n<|im_start|>user\nCapital of France?<|im_end|>\n<|im_start|>assistant\n"
    handler.on_llm_start({}, [raw], run_id="r3")
    handler.on_llm_end(LLMResult(generations=[[Generation(text=raw + "Paris.<|im_end|>")]]), run_id="r3")
    assert console.texts() == ["Capital of France?", "Paris."]


def test_chat_template_markup_cannot_hide_text_from_the_scan():
    typed = "SECRET<|im_end|>\n<|im_start|>user\nhello"
    raw = f"<|im_start|>system\nrules<|im_end|>\n<|im_start|>user\n{typed}<|im_end|>\n<|im_start|>assistant\n"
    cleaned = template_user_text(raw)
    assert "SECRET" in cleaned and "hello" in cleaned and "rules" not in cleaned


def test_strip_prompt_echo_keeps_an_answer_that_merely_starts_alike():
    assert strip_prompt_echo("Paris is the capital.", "Capital of France?") == "Paris is the capital."


def test_autogen_response_excludes_the_task():
    result = SimpleNamespace(messages=[SimpleNamespace(source="user", content="the task"), SimpleNamespace(source="assistant", content="the answer")])
    assert _task_result_to_text(result) == "the answer"


def test_haystack_agent_response_excludes_the_conversation():
    conversation = [SimpleNamespace(role="user", text="q"), SimpleNamespace(role="assistant", text="a")]
    assert _answer_text({"messages": conversation, "last_message": conversation[-1]}) == "a"
    assert _answer_text({"llm": {"replies": ["answer"]}}) == "answer"


def test_nested_run_methods_record_one_turn():
    console = Console()

    class Team:
        def run(self, task=None):
            return self.run_stream(task=task)

        def run_stream(self, task=None):
            return {"content": "done"}

    wrap_agent(team := Team(), sdk_client=client(console))
    team.run(task="once")
    assert console.surfaces() == ["prompt", "response"]


def test_dspy_module_records_one_prompt():
    console = Console()

    class Module:
        def forward(self, **kwargs):
            return {"answer": "42"}

        def __call__(self, **kwargs):
            return self.forward(**kwargs)

    wrap_module(Module(), sdk_client=client(console))(question="why")
    assert console.surfaces() == ["prompt", "response"]


def test_llamaindex_records_the_model_call_only():
    console = Console()
    handler = GgateLlamaIndexCallbackHandler(sdk_client=client(console))
    handler.on_event_start("retrieve", {"query_str": "not a prompt"}, event_id="1")
    handler.on_event_end("retrieve", {"nodes": ["not an answer"]}, event_id="1")
    handler.on_event_start("llm", {"messages": [{"role": "user", "content": "q"}]}, event_id="2")
    handler.on_event_end("llm", {"response": "a"}, event_id="2")
    assert console.texts() == ["q", "a"]


def test_latest_user_message_is_none_on_a_later_step():
    assert latest_user_message([{"role": "user", "content": "q"}, {"role": "tool", "content": "r"}]) is None


# ---- OpenAI client ---------------------------------------------------------------------------------


class Completions:
    def __init__(self, result):
        self.result, self.calls = result, 0

    def create(self, **kwargs):
        self.calls += 1
        return self.result


def openai_client(completions):
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def chunk(text):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])


def test_openai_stream_is_passed_through_and_recorded():
    console = Console()
    wrapped = wrap_client(openai_client(Completions(iter([chunk("Hel"), chunk("lo")]))), sdk_client=client(console))
    with ggate.session("s"):
        pieces = [c.choices[0].delta.content for c in wrapped.chat.completions.create(model="m", stream=True, messages=[{"role": "user", "content": "hi"}])]
    assert pieces == ["Hel", "lo"]
    assert console.surfaces() == ["prompt", "response"] and console.texts()[1] == "Hello"
    assert console.sessions() == {"s"}


def test_openai_tool_step_records_the_result_not_the_prompt_again():
    console = Console()
    reply = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="done"))])
    wrapped = wrap_client(openai_client(Completions(reply)), sdk_client=client(console))
    wrapped.chat.completions.create(model="m", messages=[
        {"role": "user", "content": "q"}, {"role": "assistant", "content": None},
        {"role": "tool", "tool_call_id": "call-1", "content": "42"}])
    assert console.surfaces() == ["tool_result", "response"]


def test_openai_session_from_request_metadata():
    console = Console()
    reply = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="a"))])
    wrapped = wrap_client(openai_client(Completions(reply)), sdk_client=client(console))
    wrapped.chat.completions.create(model="m", metadata={"session_id": "conv-9"}, messages=[{"role": "user", "content": "q"}])
    assert console.sessions() == {"conv-9"}


def test_async_framework_scan_does_not_run_on_the_event_loop():
    import threading

    threads = []

    class Agent:
        async def run(self, task=None):
            return {"content": "done"}

    agent = Agent()
    wrap_method(agent, "run", sync_around(lambda *a: threads.append(threading.current_thread()), lambda *a: None, "x"))
    asyncio.run(agent.run(task="q"))
    assert threads[0] is not threading.main_thread()


# ---- multimedia --------------------------------------------------------------------------------------

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 200
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200


def b64(raw):
    return base64.b64encode(raw).decode()


@pytest.fixture
def capture():
    return client(Console(), capture_file_text=True)


def test_openai_input_audio(capture):
    [audio] = extract_attachments_from_value({"role": "user", "content": [
        {"type": "text", "text": "transcribe"}, {"type": "input_audio", "input_audio": {"data": b64(WAV), "format": "wav"}}]})
    assert audio["mime_type"] == "audio/wav" and audio["filename"].endswith(".wav")
    assert base64.b64decode(audio["content_base64"]) == WAV


def test_langchain_audio_and_video_blocks(capture):
    found = extract_attachments_from_value([
        {"type": "audio", "base64": b64(WAV), "mime_type": "audio/wav"},
        {"type": "video", "base64": b64(MP4), "mime_type": "video/mp4"}])
    assert sorted(a["mime_type"] for a in found) == ["audio/wav", "video/mp4"]


def test_base64_jpeg_is_not_mistaken_for_a_path(capture):
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 80
    assert b64(jpeg).startswith("/9j/")
    [image] = extract_attachments_from_value([{"type": "image", "base64": b64(jpeg), "mime_type": "image/jpeg"}])
    assert base64.b64decode(image["content_base64"]) == jpeg


def test_anthropic_style_source_block(capture):
    [image] = extract_attachments_from_value({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64(WAV)}})
    assert image["mime_type"] == "image/png"


def test_prompt_text_never_contains_media_bytes(capture):
    console = Console()
    sdk = client(console, capture_file_text=True)
    wrapped = wrap_client(openai_client(Completions(SimpleNamespace(choices=[]))), sdk_client=sdk)
    wrapped.chat.completions.create(model="m", messages=[{"role": "user", "content": [
        {"type": "text", "text": "listen"}, {"type": "input_audio", "input_audio": {"data": b64(WAV), "format": "wav"}}]}])
    payload = console.events[0]["payload"]
    assert payload["text"] == "listen" and payload["attachments"][0]["capture"] == "content"


def test_capture_uses_the_applications_own_client_not_the_environment(monkeypatch):
    import ggate.core.client as module

    monkeypatch.setattr(module, "_client", None)
    monkeypatch.delenv("GGATE_AGENT_NAME", raising=False)
    client(Console(), capture_file_text=True)
    [image] = extract_attachments_from_value({"type": "input_audio", "input_audio": {"data": b64(WAV), "format": "wav"}})
    assert image["capture"] == "content"
    assert module._client is None


# ---- Kill Switch -------------------------------------------------------------------------------------


def test_lock_raises_even_when_the_caller_did_not_enforce():
    console = Console()
    console.locked = True
    with pytest.raises(GgateAgentContainedError) as raised:
        client(console).scan_prompt("hello")
    assert isinstance(raised.value, GgateBlockedError) and raised.value.decision.contained


def test_lock_stops_a_framework_tool_step_mid_run():
    console = Console()
    handler = ggate.instrument("langchain", sdk_client=client(console))
    handler.on_chat_model_start({}, [[message("human", "q")]], run_id="m")
    console.locked = True
    with pytest.raises(GgateAgentContainedError):
        handler.on_tool_start({"name": "shell"}, "rm -rf", run_id="t", parent_run_id="m")


def test_lock_withholds_the_answer_in_sync_mode():
    from langchain_core.outputs import Generation, LLMResult

    console = Console()
    handler = ggate.instrument("langchain", sdk_client=client(console))
    handler.on_chat_model_start({}, [[message("human", "q")]], run_id="m")
    console.locked = True
    with pytest.raises(GgateAgentContainedError):
        handler.on_llm_end(LLMResult(generations=[[Generation(text="the answer")]]), run_id="m")


def test_async_mode_learns_of_a_lock_from_a_queued_event_then_blocks():
    console = Console()
    console.locked = True
    sdk = client(console, mode="async")
    assert sdk.scan_prompt("queued").allowed
    assert sdk.flush(5)
    assert sdk.contained
    with pytest.raises(GgateAgentContainedError):
        sdk.scan_prompt("next")


def test_lock_holds_while_the_console_is_down_then_releases():
    console = Console()
    console.locked = True
    sdk = client(console)
    with pytest.raises(GgateAgentContainedError):
        sdk.scan_prompt("locked")
    console.down = True
    with pytest.raises(GgateAgentContainedError) as held:
        sdk.scan_prompt("during outage")
    assert "kill_switch_held" in held.value.decision.reason_codes
    console.down = False
    console.locked = False
    assert sdk.scan_prompt("after unlock").allowed and not sdk.contained


def test_an_outage_without_a_lock_still_fails_open():
    console = Console()
    console.down = True
    decision = client(console).scan_prompt("hello")
    assert decision.allowed and decision.fail_open
