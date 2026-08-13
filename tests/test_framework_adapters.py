import asyncio
import time
import unittest

import ggate
from ggate.adapters._common import textify
from ggate.adapters.autogen import wrap_agent as wrap_autogen_agent
from ggate.adapters.crewai import GgateCrewAIListener
from ggate.adapters.dspy import wrap_module as wrap_dspy_module
from ggate.adapters.haystack import wrap_target as wrap_haystack_target
from ggate.adapters.llamaindex import GgateLlamaIndexCallbackHandler
from ggate.adapters.phidata import wrap_agent as wrap_phidata_agent
from ggate.adapters.semantic_kernel import SemanticKernelMonitor
from ggate.core.client import Client
from ggate.core.config import Config


class FakeTransport:
    def __init__(self):
        self.requests = []

    def request(self, request):
        if request.get("op") != "collector_ready":
            self.requests.append(request)
        return {"kind": "verdict", "verdict": "pass", "message": "allowed"}

    async def request_async(self, request):
        if request.get("op") != "collector_ready":
            self.requests.append(request)
        return {"kind": "verdict", "verdict": "pass", "message": "allowed"}


class Agent:
    def run(self, task=None, **kwargs):
        return {"content": f"done {task}"}


class Module:
    def __call__(self, **kwargs):
        return {"answer": "42"}


class FrameworkAdapterTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        self.client = Client(Config.from_values(mode="sync"), transport=self.transport)

    def test_instrument_routes_every_python_framework(self):
        cases = [
            ("langchain", {}),
            ("langgraph", {}),
            ("crewai", {"sdk_client": self.client}),
            ("autogen", {"sdk_client": self.client}),
            ("llamaindex", {"sdk_client": self.client}),
            ("haystack", {"sdk_client": self.client}),
            ("semantic-kernel", {"sdk_client": self.client}),
            ("dspy", {"sdk_client": self.client}),
            ("phidata", {"sdk_client": self.client}),
            ("agno", {"sdk_client": self.client}),
        ]
        for framework, kwargs in cases:
            with self.subTest(framework=framework):
                self.assertIsNotNone(ggate.instrument(framework, **kwargs))

    def test_autogen_run_wrapper_scans_prompt_and_response(self):
        agent = wrap_autogen_agent(Agent(), sdk_client=self.client)
        self.assertEqual(agent.run("hello")["content"], "done hello")
        self._wait_for_requests(2)
        self.assertEqual([r["event"]["payload"]["surface"] for r in self.transport.requests], ["prompt", "response"])

    def test_haystack_run_wrapper_scans_prompt_and_response(self):
        pipeline = wrap_haystack_target(Agent(), sdk_client=self.client)
        pipeline.run({"query": "hello"})
        self.assertEqual(self.transport.requests[0]["event"]["collector"]["labels"]["framework"], "haystack")

    def test_phidata_run_wrapper_scans_prompt_and_response(self):
        agent = wrap_phidata_agent(Agent(), sdk_client=self.client)
        agent.run("hello")
        self.assertEqual(self.transport.requests[0]["event"]["collector"]["labels"]["framework"], "phidata")

    def test_dspy_proxy_scans_call(self):
        module = wrap_dspy_module(Module(), sdk_client=self.client)
        module(question="life")
        self.assertEqual(self.transport.requests[0]["event"]["collector"]["labels"]["framework"], "dspy")

    def test_crewai_listener_manual_event(self):
        listener = GgateCrewAIListener(sdk_client=self.client)
        listener.handle_event({"task": "do it"}, phase="start")
        listener.handle_tool_event({"tool_name": "search", "input": "query"}, started=True)
        self.assertEqual(len(self.transport.requests), 2)

    def test_framework_payload_text_does_not_include_wrapper_keys_or_metadata(self):
        self.assertEqual(textify({"llm": "User: hello world\nAssistant:"}), "User: hello world\nAssistant:")
        self.assertEqual(
            textify(
                {
                    "replies": "Hello! How can I assist you today?",
                    "meta": {
                        "model": "qwen",
                        "finish_reason": "stop",
                        "usage": {"total_tokens": 15},
                    },
                }
            ),
            "Hello! How can I assist you today?",
        )

    def test_llamaindex_callback_scans_start_and_end(self):
        handler = GgateLlamaIndexCallbackHandler(sdk_client=self.client)
        handler.on_event_start("LLM", {"prompt": "hello"}, event_id="1")
        handler.on_event_end("LLM", {"response": "hi"}, event_id="1")
        self._wait_for_requests(2)
        self.assertEqual(len(self.transport.requests), 2)

    def test_semantic_kernel_filters_block_without_next(self):
        async def scenario():
            monitor = SemanticKernelMonitor(sdk_client=self.client)
            called = False

            async def next_fn(context):
                nonlocal called
                called = True

            await monitor.prompt_render_filter(type("Ctx", (), {"prompt": "hello"})(), next_fn)
            self.assertTrue(called)

        asyncio.run(scenario())

    def _wait_for_requests(self, count):
        deadline = time.time() + 1
        while len(self.transport.requests) < count and time.time() < deadline:
            time.sleep(0.01)


if __name__ == "__main__":
    unittest.main()
