# SDK Testing Guide

Use this guide in three layers: unit tests, a live-Console smoke test, and Console validation.

## 1. Unit Tests

These need no Console: the transport is stubbed.

```bash
pip install -e ".[dev]"
python -m pytest tests
```

Expected result: all tests pass.

## 2. Live Console Smoke Test

This validates the SDK as a main-product collector.

Prerequisites:

- A reachable Console (`GGATE_CONSOLE_URL`).
- An IAM API key from Console UI -> Admin -> API keys (`GGATE_API_KEY`).

```bash
python3 -m venv /tmp/ggate-sdk-venv
/tmp/ggate-sdk-venv/bin/pip install -e .
GGATE_MODE=sync \
GGATE_CONSOLE_URL=https://godels-gate.example.com \
GGATE_API_KEY=godel_... \
  /tmp/ggate-sdk-venv/bin/python - <<'PY'
import ggate

ggate.init(agent_name="Smoke Test", team="Platform Engineering", mode="sync")
decision = ggate.scan_prompt("Godel's Gate SDK smoke test", framework="manual", model="test")
print(decision.to_json())
PY
```

Pass criteria:

- With the Console reachable, `fail_open` should be `false`.
- The event should appear in the Console's Activity stream.
- Point `GGATE_CONSOLE_URL` at an unreachable host and the same script should continue and return
  a fail-open pass decision; unset it entirely and it should do the same, logging which setting is
  missing.

## 3. Policy Enforcement Test

Use a policy/detector input that is known to block in your environment. Do not assume a generic text
prompt will block unless the policy says so.

```python
import ggate

ggate.init(agent_name="Smoke Test", team="Platform Engineering", mode="sync")
decision = ggate.scan_prompt("YOUR_KNOWN_BLOCK_FIXTURE", enforce=False, framework="manual")
assert decision.verdict == "block", decision.to_json()
```

Pass criteria:

- Sync mode returns `verdict=block` before the LLM/provider call.
- Framework adapters using `enforce=True` raise before the underlying call.
- Async mode records the event but does not block.

## 4. Framework Smoke Tests

Run each with fake/model-stub providers first, then with a real provider key.

| Framework | Smoke test |
|---|---|
| LangChain/LangGraph | Add `ggate.instrument("langchain")` or `ggate.instrument("langgraph")` as a callback handler. Run one LLM call and one tool call. |
| CrewAI | Register `ggate.instrument("crewai")` as a CrewAI event listener. Run one crew task and one tool. |
| AutoGen | Wrap the agent/team with `ggate.instrument("autogen", agent=agent)`. Run `agent.run(...)` and `run_stream(...)`. |
| OpenAI | Wrap the OpenAI client. Test `chat.completions.create`, `responses.create`, `files.create`, `beta.threads.messages.create`, and `beta.threads.runs.create`. |
| OpenAI Swarm | Wrap the Swarm client with `ggate.instrument("openai-swarm", client=swarm_client)`. Run one handoff path. |
| LlamaIndex | Add the callback handler returned by `ggate.instrument("llamaindex")`. Run one query. |
| Haystack | Wrap the pipeline or agent with `ggate.instrument("haystack", pipeline=pipe)` or `agent=agent`. Run one query. |
| Semantic Kernel | Register the filters from `ggate.instrument("semantic-kernel", kernel=kernel)` or manually attach returned filters. Invoke one prompt function and one tool/function. |
| DSPy | Wrap a module with `ggate.instrument("dspy", module=module)`. Call the module. |
| Phidata/Agno | Wrap the agent with `ggate.instrument("phidata", agent=agent)` or `ggate.instrument("agno", agent=agent)`. Run one prompt and one tool call. |

Every framework above has a Python adapter; the Node SDK covers a subset (see its own guide).

## 5. Edge Cases To Verify Before Release

- Console unreachable or unconfigured: SDK returns fail-open pass and the app still works.
- Console slow: sync timeout is bounded by `GGATE_TIMEOUT_MS`; async mode does not block.
- Rejected/expired API key: the SDK re-exchanges once, then fails open rather than throwing.
- Queue overflow: async queue drops oldest events and does not grow unbounded.
- Blocked prompt: provider call is not made.
- Blocked tool call: tool function is not made where the framework filter/wrapper supports preflight.
- Streaming output: app receives stream; final output is audited after consumption.
- File upload: file metadata is captured; optional text/content capture follows configured caps.
- Generated file: SDK captures metadata when visible through file APIs; otherwise call `scan_file`.
- Concurrent requests: correlation IDs differ per request and events do not leak context.
- Redaction: secrets are masked before leaving the app process.
- Disabled SDK: `GGATE_DISABLED=1` returns pass and emits nothing.

## 6. Console Validation

With the Console stack running:

1. Run the smoke script above.
2. Open Activity.
3. Filter by `collector.labels.language=python`.
4. Filter by `collector.labels.framework`.
5. Confirm prompt, response, tool, and file events are visible.
6. Confirm fail-open decisions are not produced when the Console is healthy.

## 7. What To Automate In CI

- Unit tests on Python 3.9 through current supported Python.
- Contract test of emitted `scan_text` events against the Console's event crates (the Rust crates
  are the wire-shape source of truth).
- Stub-Console HTTP integration test.
- Real-Console e2e test against a running deployment.
- Console e2e test that verifies events reach Activity.
