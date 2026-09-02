# Godel's Gate Python SDK

`ggate` turns an AI agent application into a Godel's Gate collector: a few lines of code capture
framework activity (prompts, responses, tool calls, tool results, file access) as `gate/v1`
runtime events, send them for detection + policy evaluation, and — in sync mode — enforce the
returned verdict.

SDK events carry `identity.agent_source = "agent-framework"` and
`collector.collector_type = "sdk"`; the specific AI framework (langchain, openai, crewai, …)
travels in `collector.labels.framework` and `source.client`, which the Console renders as
`agent-framework:<framework>` connectors.

The Node.js SDK is [godellabs-ai/ggate-node](https://github.com/godellabs-ai/ggate-node).

## Install

```bash
python -m pip install "ggate @ git+https://github.com/godellabs-ai/ggate-python.git"
```

The core package has no dependencies. Framework adapters import the framework you already use;
extras are available if you want pip to pull one in (`ggate[openai]`, `ggate[langchain]`,
`ggate[all]`, …). Python 3.9+.

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
GGATE_CONSOLE_URL=https://godels-gate.example.com
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

When rclassifier produces topic/sensitivity output, a waited scan also returns
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

## Latency and failure semantics

The SDK never breaks the host application:

- **Sync mode** blocks prompt, tool-call, file, shell, and web scans until the verdict, bounded by the scan budget
  (`GGATE_TIMEOUT_MS`, else 4s — a ceiling, not a per-call cost; raise it for prompts carrying
  attachments, where the Console also extracts and OCRs the file). Responses and tool results are
  queued by default; pass `wait=True` to receive classification, or `enforce=True` for a
  block-capable tool-result boundary.
- **Async mode** (`ggate.init(mode="async")`) queues everything and always returns an immediate
  pass — observability without gating.
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
| `GGATE_CONSOLE_URL` | Console base URL, e.g. `https://godels-gate.example.com` — **required** | unset |
| `GGATE_API_KEY` | Console IAM API key (`godel_...`) — **required** | unset |
| `GGATE_CONSOLE_CA_CERT` | PEM CA used to verify a private-CA Console | system trust |
| `GGATE_TIMEOUT_MS` | sync scan budget | 4000 |
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
