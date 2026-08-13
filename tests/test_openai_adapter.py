import unittest
import time

from ggate.adapters.openai import wrap_client
from ggate.core.client import Client
from ggate.core.config import Config


class FakeTransport:
    def __init__(self):
        self.requests = []

    def request(self, request):
        if request.get("op") != "collector_ready":
            self.requests.append(request)
        return {"kind": "verdict", "verdict": "pass", "message": "allowed"}


class FakeCompletions:
    def create(self, **kwargs):
        return {"choices": [{"message": {"content": "hi"}}]}


class FakeMessages:
    def create(self, thread_id, **kwargs):
        return {"id": "msg_1", "content": kwargs.get("content")}


class FakeRuns:
    def create(self, thread_id, **kwargs):
        return {"id": "run_1", "messages": [{"content": "done"}]}

    def submit_tool_outputs(self, thread_id, run_id, **kwargs):
        return {"id": run_id}


class FakeFiles:
    def create(self, **kwargs):
        return {"id": "file_1"}


class FakeClient:
    class Chat:
        completions = FakeCompletions()

    chat = Chat()

    class Beta:
        class Threads:
            messages = FakeMessages()
            runs = FakeRuns()

        threads = Threads()

    beta = Beta()
    files = FakeFiles()


class OpenAIAdapterTests(unittest.TestCase):
    def test_wraps_chat_completion(self):
        transport = FakeTransport()
        sdk = Client(Config.from_values(mode="sync"), transport=transport)
        client = wrap_client(FakeClient(), sdk_client=sdk)

        result = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "hello"}],
        )

        self.assertEqual(result["choices"][0]["message"]["content"], "hi")
        self.assertEqual(transport.requests[0]["event"]["payload"]["surface"], "prompt")

    def test_wraps_assistants_thread_messages_and_runs(self):
        transport = FakeTransport()
        sdk = Client(Config.from_values(mode="sync"), transport=transport)
        client = wrap_client(FakeClient(), sdk_client=sdk)

        client.beta.threads.messages.create("thread_1", content="hello", attachments=[{"file_id": "file_1"}])
        client.beta.threads.runs.create("thread_1", additional_messages=[{"role": "user", "content": "run"}])
        client.beta.threads.runs.submit_tool_outputs(
            "thread_1",
            "run_1",
            tool_outputs=[{"tool_call_id": "tool_1", "output": "tool output"}],
        )

        deadline = time.time() + 1
        while len(transport.requests) < 4 and time.time() < deadline:
            time.sleep(0.01)
        surfaces = [request["event"]["payload"]["surface"] for request in transport.requests]
        self.assertIn("prompt", surfaces)
        self.assertIn("tool_result", surfaces)


if __name__ == "__main__":
    unittest.main()
