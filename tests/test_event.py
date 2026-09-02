import unittest

from ggate.core.config import Config
from ggate.core.event import RuntimeEventBuilder


class EventTests(unittest.TestCase):
    def test_redacts_secret_and_hashes_original(self):
        # Redaction is opt-in: the Console is the detection engine, so the default sends raw
        # content. This exercises the deployments that turn masking on anyway.
        builder = RuntimeEventBuilder(Config.from_values(mode="sync", redact=True))
        event, redaction = builder.prompt("token=abc1234567890", framework="generic")

        self.assertIn("[REDACTED]", event["payload"]["text"])
        self.assertEqual(redaction.redaction_count, 1)
        self.assertIsNotNone(redaction.content_sha256)

    def test_derivable_fields_stay_off_the_wire(self):
        """Sizes and the schema constant are the receiver's to fill (`fill_derived`)."""
        builder = RuntimeEventBuilder(Config.from_values(mode="sync"))
        prompt, _ = builder.prompt("hello")
        result, _ = builder.tool_result("bash", "stdout", kind="shell")

        self.assertNotIn("schema_version", prompt)
        self.assertNotIn("length", prompt["payload"])
        self.assertNotIn("output_len", result["payload"])
        # The event id and timestamp are NOT derivable: the id is the retry idempotency key and the
        # timestamp is when the application saw the content, not when the Console received it.
        self.assertIn("id", prompt)
        self.assertIn("timestamp", prompt)

    def test_every_event_names_the_agent_and_its_owning_team(self):
        builder = RuntimeEventBuilder(
            Config.from_values(
                mode="sync", agent_name="JIRA Project Assistant", team="Platform Engineering"
            )
        )
        event, _ = builder.prompt("hello", framework="langgraph")

        self.assertEqual(event["identity"]["agent_name"], "JIRA Project Assistant")
        self.assertEqual(event["identity"]["team"], "Platform Engineering")
        # The declared name replaces nothing: the framework still travels where the Console reads
        # it for connector/logo resolution.
        self.assertEqual(event["identity"]["agent_source"], "agent-framework")
        self.assertEqual(event["collector"]["labels"]["framework"], "langgraph")
        self.assertEqual(event["source"]["client"], "langgraph")

    def test_session_carries_only_established_correlation(self):
        builder = RuntimeEventBuilder(Config.from_values(mode="sync"))
        anonymous, _ = builder.prompt("hello")
        correlated, _ = builder.prompt("hello", correlation_id="turn-7")

        self.assertNotIn("correlation_id", anonymous["session"])
        self.assertNotIn("cwd", anonymous["session"])
        self.assertEqual(correlated["session"]["correlation_id"], "turn-7")

    def test_default_session_and_platform_are_runtime_friendly(self):
        builder = RuntimeEventBuilder(Config.from_values(mode="sync"))
        event, _ = builder.prompt("hello", framework="langchain")

        self.assertTrue(event["session"]["session_id"].startswith("py-sdk-"))
        self.assertEqual(event["collector"]["labels"]["framework"], "langchain")
        # platform_os is a Console dimension: must match the Rust std::env::consts vocabulary.
        self.assertIn(event["collector"]["platform"]["os"], {"linux", "macos", "windows"})

    def test_correlation_ids_ride_in_source_not_session(self):
        builder = RuntimeEventBuilder(Config.from_values(mode="sync"))
        event, _ = builder.response(
            "hi",
            framework="openai",
            model="gpt-4o",
            conversation_id="conv-1",
            request_id="req-1",
        )

        # The Rust Session struct has no model/conversation/request fields; they
        # belong to SourceContext and would be silently dropped from session.
        for key in ("model", "conversation_id", "request_id"):
            self.assertNotIn(key, event["session"])
        self.assertEqual(event["source"]["model"], "gpt-4o")
        self.assertEqual(event["source"]["conversation_id"], "conv-1")
        self.assertEqual(event["source"]["request_id"], "req-1")

    def test_response_carries_token_usage_telemetry(self):
        builder = RuntimeEventBuilder(Config.from_values(mode="sync"))
        event, _ = builder.response(
            "hi",
            usage={"input_tokens": 10, "output_tokens": 5, "unknown_key": 1},
            duration_ms=1200,
            api_calls=2,
        )

        payload = event["payload"]
        self.assertEqual(payload["usage"], {"input_tokens": 10, "output_tokens": 5})
        self.assertEqual(payload["duration_ms"], 1200)
        self.assertEqual(payload["api_calls"], 2)

    def test_current_response_tool_and_policy_surface_fields(self):
        builder = RuntimeEventBuilder(Config.from_values(mode="sync"))
        response, _ = builder.response(
            "done", thinking="analysis", thinking_redacted=False, subagent_type="Explore"
        )
        tool_call, _ = builder.tool_call(
            "search", input_summary={"q": "x"}, server_info={"transport": "stdio"}
        )
        tool_result, _ = builder.tool_result(
            "bash",
            "failed",
            kind="shell",
            ok=False,
            exit_code=2,
            output_len=100,
            truncated=True,
            duration_ms=50,
        )
        shell, _ = builder.shell("curl https://example.com", argv=["curl", "https://example.com"])
        web, _ = builder.web("https://example.com/path", method="GET")

        self.assertEqual(response["payload"]["thinking"], "analysis")
        self.assertEqual(response["payload"]["subagent_type"], "Explore")
        self.assertEqual(tool_call["payload"]["server_info"], {"transport": "stdio"})
        self.assertFalse(tool_result["payload"]["ok"])
        self.assertEqual(tool_result["payload"]["exit_code"], 2)
        self.assertEqual(shell["payload"]["surface"], "shell")
        self.assertEqual(web["payload"]["domain"], "example.com")

    def test_tool_call_defaults_server_to_framework_and_redacts_input(self):
        builder = RuntimeEventBuilder(Config.from_values(mode="sync", redact=True))
        event, redaction = builder.tool_call(
            "web_search",
            input_summary={"query": "hello", "auth": "token=abc1234567890"},
            framework="crewai",
        )

        payload = event["payload"]
        self.assertEqual(payload["server"], "crewai")
        self.assertEqual(payload["input_summary"]["auth"], "[REDACTED]")
        self.assertEqual(redaction.redaction_count, 1)

    def test_file_event_rejects_unknown_action(self):
        builder = RuntimeEventBuilder(Config.from_values(mode="sync"))
        with self.assertRaises(ValueError):
            builder.file_event("/tmp/x", action="chmod")


if __name__ == "__main__":
    unittest.main()
