# Gödel Gate Python SDK

`ggate` turns an AI agent application into a Gödel Gate collector: a few lines of code capture
framework activity (prompts, responses, tool calls, tool results, file access) as `gate/v1`
runtime events, send them for detection + policy evaluation, and — in sync mode — enforce the
returned verdict.

SDK events carry `identity.agent_source = "agent-framework"` and
`collector.collector_type = "sdk"`; the specific AI framework (langchain, openai, crewai, …)
travels in `collector.labels.framework` and `source.client`, which the Console renders as
`agent-framework:<framework>` connectors.

The Node.js SDK is [godellabs-ai/ggate-node](https://github.com/godellabs-ai/ggate-node).

## Release compatibility

This SDK release is **0.2.1**, aligned with Gödel Gate Console **0.2.1**.
Use SDK **0.2.y** with Console **0.2.x**; SDK patch releases may advance independently.

### Microsoft 365 Agents SDK

```python
import ggate

gate = ggate.init(agent_name="Microsoft 365 Assistant", team="Platform Engineering", mode="sync")
# adapter is your existing Microsoft Agents CloudAdapter.
ggate.instrument("microsoft-365-agents", adapter=adapter, sdk_client=gate)
```

Install the optional `ggate[microsoft-365-agents]` extra if your app does not already have the
Microsoft package. The adapter uses public middleware hooks to scan incoming content and
outgoing sends/updates, including inline cards and invoke values. It raises `GgateBlockedError`
on a block before calling the next handler or delivering the activity. Catch that exception at
your application's error boundary without echoing blocked content.

See the runnable [Microsoft 365 example](examples/microsoft-365-agent/README.md). Internal model
and tool operations need their own adapter or explicit scans; Microsoft-managed internals and
remote attachment bytes are not automatically visible. Python conversation context propagates
to nested explicit scans and is isolated between concurrent turns.

### Changes in 0.3.0

- **Kill Switch.** A lock in the Console raises `GgateAgentContainedError` (a `GgateBlockedError`) in
  `sync` and `async` mode, even where the caller did not pass `enforce`. In `async` mode the first
  queued verdict turns scanning synchronous, so an unlock is seen at the next scan. A known lock stays
  in force while the Console cannot confirm its release; an outage with no lock still fails open.
- **Sessions.** `with ggate.session(conversation_id):` puts every event of a turn in one session, across
  all adapters. A session bound at call time beats one given to `instrument()`. LangChain and LangGraph
  also read `session_id` / `thread_id` from run metadata and share a session across nested runs.
- **One event, one text.** Prompts hold the newest user message, not the resent history. Responses hold
  the answer, without the task (AutoGen), the conversation (Haystack), or an echoed prompt or chat
  template (Hugging Face pipelines). Nested run methods record one turn. LlamaIndex records only LLM events.
- **Responses in `sync` mode** are checked by adapters before they are returned, so a block withholds
  the answer. This adds one waited scan per model call. Streamed text cannot be recalled.
- **Streaming** OpenAI calls are recorded when the stream ends. Async adapters no longer scan on the
  event loop. LangChain adapters stop on a block by themselves.
- **Media.** Audio and video parts join images and files: OpenAI `input_audio`, LangChain `audio`/`video`
  blocks, Anthropic-style `source` blocks, and framework media objects. CrewAI 1.x uses execution hooks.

### Changes in 0.2.1

- `enforce=True` always waits, even if the default mode is `async`.
- `scan_response(..., enforce=True)` and `scan_tool_result(..., enforce=True)` mark content as
  awaiting delivery and can raise on a block. Do not call them after delivering that content.
- `before_delivery=True` also waits and emits `phase="pre"`, but returns the verdict for the
  caller to handle. `wait=True` alone remains an audit scan with `phase="post"`.
- Decisions retain Gate's `media_gate`, document intelligence, and pipeline status. Explicit
  repository provenance can be supplied with `source_trust`.
- The core now requires Python 3.10+. Some optional frameworks require Python 3.11+.
  The OpenAI dependency allows both 2.x and 3.x so framework-specific requirements can resolve.

## Install

```bash
python -m pip install "ggate @ git+https://github.com/godellabs-ai/ggate-python.git"
```

The core package has no dependencies. Framework adapters import the framework you already use;
extras are available if you want pip to pull one in (`ggate[openai]`, `ggate[langchain]`,
`ggate[all]`, …). Python 3.10+.

## Quickstart

```python
import ggate

ggate.init(
    agent_name="JIRA Project Assistant",  # required: what the Console lists this agent as
    team="Platform Engineering",          # required: who owns it
    mode="sync",                          # default
)

decision = ggate.scan_prompt("Summarize this document", framework="langchain", model="gpt-4o")
if decision.blocked:
    raise RuntimeError(decision.message)

ggate.scan_response(
    "Summary text",
    framework="langchain",
    model="gpt-4o",
    usage={"input_tokens": 1200, "output_tokens": 240},  # optional turn telemetry
)
```

### Naming your agent

`agent_name` and `team` are required, and `init()` raises without them (or without
`GGATE_AGENT_NAME` / `GGATE_TEAM`). They are the two facts the SDK cannot work out for itself:

- **`agent_name`** is what this agent *is*, as an operator would name it — `"JIRA Project
  Assistant"`, not the framework it is built on. The Console lists sessions under this name, so
  three assistants built on LangGraph stay three distinguishable agents. The framework is still
  reported alongside it and still drives the connector view.
- **`team`** is who is accountable for it. A deployed agent has no person at a keyboard, so the
  seat identity falls back to the build machine's `<os-user>@<hostname>` — whoever ran the deploy,
  not whoever owns the workload. The seat identity is still reported unchanged; `team` is the name
  shown beside it.

`agent_name` usually belongs in code (every install of an app is the same named agent), while
`team` usually belongs in the environment (it varies per deployment).

Framework instrumentation — see [docs/framework-coverage.md](docs/framework-coverage.md) for the
full matrix of what each adapter hooks and where it can enforce:

```python
import ggate
from openai import OpenAI

client = ggate.instrument("openai", client=OpenAI())          # wrapped client

handler = ggate.instrument("langchain")                       # callback handler
```

## Connecting to a Console

The Console is the SDK's only destination. Set its URL and an IAM API key (Console UI →
**Admin → API keys**) and scans go to `POST /api/v1/scan`, which runs the full pipeline —
normalize, OCR/extraction of image and file attachments, deterministic rules, the security
classifier, DLP, threat intel, document intelligence, and policy — records the event, and
returns the verdict plus any topic/sensitivity classification:

```bash
GGATE_CONSOLE_URL=https://godel-gate.example.com
GGATE_API_KEY=godel_...
```

The API key is exchanged at `/api/v1/detection-engine/token` for an access/refresh token pair.
The SDK rotates that pair through `/api/v1/detection-engine/token/refresh`, so per-scan auth is a
stateless signature check and hourly access-token renewal does not repeat the API-key hash.

For a Console signed by a private CA, set `GGATE_CONSOLE_CA_CERT=/path/to/ca.pem`; TLS verification
remains enabled.

Both are required. With either missing the SDK logs one warning at startup and every scan fails
open with a message naming what is unset — a configuration mistake must not break the
application, and it must not silently look like an all-clear either.

The SDK sends **unredacted** content by default. The Console is the detection engine, so
client-side masking would hide exactly the secrets it exists to catch; set `GGATE_REDACT=1` for
deployments that would rather lose those detections than let the content leave the process.

## Verdicts

A scan returns a `Decision`: `verdict` (`pass` | `warn` | `block` | `system`),
`message`, `reason_codes`, and — on warn/block — a `detection` headline naming which protection
fired (`{"source": "sensitive_data", "detail": "aws_access_key_id", ...}`). `decision.blocked` /
`decision.allowed` are the convenience accessors; passing `enforce=True` raises
`GgateBlockedError` on a block instead.

When the Console produces document intelligence, a waited scan also returns
`decision.document_intelligence` in the same normalized shape stored in event JSON:

```python
intel = decision.document_intelligence
if intel:
    print(intel.taxonomy.topic.label, intel.sensitivity.severity)
    print(intel.sensitivity.sensitive_data_classes)
```

This field is classification context, not an enforcement finding. It is available only when the
call waits for the Console (sync prompt/tool/file/shell/web scans, or `wait=True` on response and
tool-result scans). Queued calls return an immediate local pass with `document_intelligence=None`.

Async applications have `await`-able twins of every scan call (`scan_prompt_async`,
`scan_response_async`, `scan_tool_call_async`, `scan_tool_result_async`, `scan_file_async`,
`scan_shell_async`, and `scan_web_async`).

### Scan coverage and file classification

A successful HTTP response may include `decision.pipeline_status` with `errors`, `warnings`,
`media`, and `visual_codes`. These describe coverage and evidence, independently of the verdict:
a `pass` with `attachment_scan_incomplete` is not confirmation that every attachment was scanned.
Missing pipeline status on a queued/local pass does not establish coverage either.

Document intelligence can also include `file_classification` (provider-qualified labels,
`status`, and `gate_sensitivity_available`) and `taxonomy.topic.resolution` (topic uncertainty
and resolution provenance). An `unavailable` file-classification status is not a clean result.

`pipeline_status`, `file_classification`, and topic `resolution` are dictionaries; the existing
taxonomy and sensitivity fields remain dataclasses.

## Latency and failure semantics

Transport failures fail open; missing agent identity and explicit block enforcement can still raise errors:

- **Sync mode** blocks prompt, tool-call, file, shell, and web scans until the verdict. `GGATE_TIMEOUT_MS` (default 4000) applies to each HTTP request
  (Python uses a socket-operation timeout), not a whole-scan deadline. Token exchange, refresh,
  and retries can add to the total time. Raise it for attachments that need extraction or OCR. Responses and tool results are
  queued by default; pass `wait=True` to receive classification, or `enforce=True` for a
  block-capable tool-result boundary.
- **Async mode** (`ggate.init(mode="async")`) queues preflight scans and returns an immediate local
  pass. Explicit `wait` on response/tool-result scans and all `enforce` calls still wait
  for the Console in either mode.
- **Fail open**: any problem reaching the Console (unreachable, restarting, slow, rejected key,
  unconfigured) yields an allow decision with `fail_open=True`. After a failure the SDK fails
  open instantly for a cooldown (`GGATE_COOLDOWN_SECS`, default 30) instead of re-calling a
  struggling Console on every request.
- **Background queue**: bounded (`GGATE_QUEUE_MAX`, drop-oldest), retries transient failures
  with capped backoff, and its shutdown flush is deadline-bounded (`GGATE_FLUSH_TIMEOUT_MS`,
  default 3000) so application exit never hangs. Call `ggate.flush()` to drain explicitly.
- **Redaction**: with `GGATE_REDACT` on, known secret patterns are masked before anything leaves
  the process; the original content hash + mask count travel in the scan's redaction summary.

## Configuration

`GGATE_CONSOLE_URL` and `GGATE_API_KEY` are required, as are the agent's name and owning team
(in code or via `GGATE_AGENT_NAME` / `GGATE_TEAM`); everything else has a default. Identity
falls back to the device config at `~/.ggate/config.yaml` when an agent installed on the same
machine wrote one — read for identity only, so SDK events land under the same org/seat/device as
that machine's other collectors. Environment variables:

| Variable | Meaning | Default |
|---|---|---|
| `GGATE_AGENT_NAME` | agent name — **required** (or `agent_name`) | unset |
| `GGATE_TEAM` | owning team — **required** (or `team`) | unset |
| `GGATE_MODE` | `sync` (enforce) or `async` (observe) | `sync` |
| `GGATE_CONSOLE_URL` | Console base URL, e.g. `https://godel-gate.example.com` — **required** | unset |
| `GGATE_API_KEY` | Console IAM API key (`godel_...`) — **required** | unset |
| `GGATE_CONSOLE_CA_CERT` | PEM CA used to verify a private-CA Console | system trust |
| `GGATE_TIMEOUT_MS` | per-request timeout (Python: socket-operation timeout) | 4000 |
| `GGATE_DISABLED` | `1` disables the SDK entirely | off |
| `GGATE_HOME` | where the device config is looked for | `~/.ggate` |
| `GGATE_CONFIG` | device config.yaml path (identity defaults only) | `$GGATE_HOME/config.yaml` |
| `GGATE_ORG_ID` | organization id | config `org_id`, else `local` |
| `GGATE_USER` / `GGATE_USER_EMAIL` | seat identity | config `user_email`, else `<os-user>@<host>` |
| `GGATE_WORKSTATION_ID` | stable device id | config `workstation_id` |
| `GGATE_COLLECTOR_ID` | collector id | `<workstation_id>:ggate-python-sdk` |
| `GGATE_QUEUE_MAX` | background queue bound | 1024 |
| `GGATE_COOLDOWN_SECS` | fail-open cooldown after a failure | 30 |
| `GGATE_FLUSH_TIMEOUT_MS` | default/atexit flush deadline | 3000 |
| `GGATE_REDACT` | mask secrets before sending | off |
| `GGATE_CAPTURE_FILE_TEXT` | capture attachment content (else metadata only) | off |
| `GGATE_MAX_FILE_BYTES` | attachment capture cap | 65536 |

The same values can be passed programmatically to `ggate.init(...)`.

## Development

```bash
pip install -e ".[dev]"
python -m pytest tests
```

See [docs/testing-guide.md](docs/testing-guide.md) for the layered test approach — unit tests, a
live-Console smoke test, policy enforcement, and per-framework checks.

## License

Apache-2.0 — see [LICENSE](LICENSE).
