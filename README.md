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

This SDK release is **0.3.1**, released with Gödel Gate Console **0.3.1**.
Install a release tag rather than the moving default branch. Keep the SDK release selected by your Console when maintaining an older deployment.
SDK patch releases may advance independently within **0.3.x**. Keep an older SDK release for an older Console and validate minor upgrades together.

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

### Changes in 0.3.1

- Documents that Gate masks evaluated events before storage; SDK redaction remains optional.
- Released with Console 0.3.1. `Decision.pipeline_status` passes through new Console fields such as
  `file_extension_exemptions` unchanged. No runtime or API behavior changes from 0.3.0.

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
python -m pip install "ggate @ git+https://github.com/godellabs-ai/ggate-python.git@v0.3.1"
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
- **`team`** is the workload's owning team. User identity comes from the configured `user` /
  `GGATE_USER`, falling back to the runtime machine's `<os-user>@<hostname>`. Shared applications
  should bind their authenticated user per request as described below.

`agent_name` usually belongs in code (every install of an app is the same named agent), while
`team` usually belongs in the environment (it varies per deployment).

### AI access rules and application users

The Console's **Policies → Authorised AI** access rules apply to SDK scans. Select the named
application (`agent_name`), its framework, or all supported agents. The Console resolves users,
groups, and user tags from its endpoint directory. `team` describes workload ownership; it does
not grant directory group membership.

In a shared application, bind the authenticated user's endpoint email or directory alias for the
whole request. Use a value verified by your server's authentication layer, never an identity
supplied directly in a prompt or request body:

```python
with ggate.session(conversation_id, user=authenticated_user_email):
    # Explicit preflight waits even if GGATE_MODE=async; a block raises GgateBlockedError.
    await ggate.scan_prompt_async(prompt, framework="langgraph", enforce=True)
    answer = await run_agent(prompt)
    await ggate.scan_response_async(answer, framework="langgraph", enforce=True)
    # Deliver only after the final scan returns.
```

`ggate.event_context(user=...)` binds just the user without changing the session. Both scopes follow
async calls and instrumented adapters and restore the previous identity on exit. A scan's `user=`
overrides the scope; the scope overrides the configured `user` / `GGATE_USER`. An explicit `None`
or blank user means anonymous and clears the fallback. Set it explicitly for unauthenticated requests.
Without an override, existing single-user/service deployments keep their configured identity.

Block rules prevent operations only when scans run before the operation with `enforce=True`, or
the application checks the returned verdict. Adapter enforcement follows `GGATE_MODE=sync`.
Audit calls cannot prevent an operation already performed. Warn remains an allowed decision for
the application to present; `decision.policy_source` and `decision.matched_policies` retain policy
IDs and names, including Monitor matches. Ordinary access blocks are evaluated again at the next
scan. Cached Kill Switch locks are separate per user and framework; `client.contained` means at
least one such lock is known.

SDK API keys do not establish a human provider login or subscription plan. Approved **provider
account domains** and observed plan tags come from coding-agent/browser collectors. SDK access
rules use the endpoint user's Console identity and existing directory tags; the SDK does not
claim provider email, plan, groups, or tags from request metadata. Existing transport fail-open
behavior is unchanged; applications requiring fail-closed preflight must also check `decision.fail_open`.

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

Gate is responsible for masking the evaluated event before storage, including nested tool
arguments and detected personal data. Keep Gate updated with its storage-redaction fixes;
an SDK update cannot protect records written by an older server. A block prevents the guarded
action, but the content has already been sent to Gate for evaluation. SDK masking is an optional,
pattern-based privacy control, not a substitute for server-side storage masking.

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
- **Console outages**: under Console Settings → General → *Behaviour when the Console is down*
  an administrator sets a default (fail open or fail closed) and exceptions that get the opposite —
  endpoint users, teams, user tags, coding agents, provider domains, or SDK agent names such as this
  client's `agent_name`. Every answered scan carries what applies to its user
  (`console_outage_action`) and to this agent's other users (`console_outage_fallback`); the SDK
  remembers both (`client.console_outage_action(user)`). When that answer is `block`, a waited-for scan
  that cannot reach the Console at all — no connection, or a gateway answering 502/503/504 for it — returns a `block`
  decision with reason code `console_unreachable` (raised by `enforce` calls), for the rest of the
  cooldown too. A Console that answered with an error, a scan still running when `GGATE_TIMEOUT_MS`
  ran out, and a process that has not yet heard from the Console keep failing open. Queued
  (audit) scans are never blocked.
- **Background queue**: bounded (`GGATE_QUEUE_MAX`, drop-oldest), retries transient failures
  with capped backoff, and its shutdown flush is deadline-bounded (`GGATE_FLUSH_TIMEOUT_MS`,
  default 3000) so application exit never hangs. Call `ggate.flush()` to drain explicitly.
- **Redaction**: with `GGATE_REDACT` on, known secret patterns are masked before anything leaves
  the process; the original content hash + mask count travel in the scan's redaction summary.

## Configuration

A Console URL and API key source are required, as are the agent's name and owning team
(in code or via `GGATE_AGENT_NAME` / `GGATE_TEAM`); everything else has a default. Identity
falls back to the device config at `~/.ggate/config.yaml` when an agent installed on the same
machine wrote one — also used for Console URL and credentials, so SDK events land under the same org/seat/device as
that machine's other collectors. Environment variables:

| Variable | Meaning | Default |
|---|---|---|
| `GGATE_AGENT_NAME` | agent name — **required** (or `agent_name`) | unset |
| `GGATE_TEAM` | owning team — **required** (or `team`) | unset |
| `GGATE_MODE` | `sync` (enforce) or `async` (observe) | `sync` |
| `GGATE_CONSOLE_URL` | Console base URL override, e.g. `https://godel-gate.example.com` | config `api_url` |
| `GGATE_API_KEY` | Legacy environment fallback for the Console IAM API key | config `api_key` first |
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

## Credential rotation and workload identity

The SDK reads the optional collector config at `GGATE_CONFIG`, otherwise
`$GGATE_HOME/config.yaml` (default `~/.ggate/config.yaml`). Its `api_url` supplies the Console URL
when no explicit URL/environment override is set. The default API key comes from `api_key` in
that file; `GGATE_API_KEY` remains a fallback.

For an organization-managed environment secret, set `GGATE_API_KEY_ENV=COMPANY_GATE_KEY` or
`api_key_env: COMPANY_GATE_KEY` in the config. This selects the **name** of the environment
variable, not the secret. A selected source that is empty does not fall back to a stale config key.
All coding-agent hooks support the same config/environment selector. Environment updates must
reach the agent/service process (usually by relaunching it).

For live SDK rotation, use `GGATE_API_KEY_FILE=/mounted/secret` or a synchronous key-provider
callback. The SDK rereads the selected key source before authentication and discards cached
access/refresh tokens when it changes. Provider, named environment, and mounted file selectors
are authoritative in that order, followed by an explicit API key, config key and legacy environment
fallback. Update the selected source, verify a scan, then revoke the old key; this does not provide
a server-side rotation grace window or promise zero failed in-flight scans.

Enable `GGATE_CLOUD_INSTANCE_METADATA=1` and Console **Settings → General → Cloud instance
metadata** to include allowlisted AWS/GCP/Azure metadata plus pod/function identity. Kubernetes
uses `POD_NAMESPACE`, `POD_NAME`, `POD_UID`, `NODE_NAME`, `POD_SERVICE_ACCOUNT`; Lambda, Cloud Run
and Azure Functions use their service/function/instance variables. Metadata discovery never reads
role credentials or identity tokens. Explicit workload identity is also allowlisted. The discovered
identity is cached per client; recreate the client after a workload move.

Options are `api_key_env`, `api_key_file`, `api_key_provider`, `cloud_instance_metadata` and
`workload_identity`. For example: `ggate.init(agent_name="Assistant", team="Platform",
api_key_provider=lambda: secret_cache.current_key)`.

## Retrieval and memory protection

The LangChain/LangGraph callback includes `on_retriever_start`, `on_retriever_end` and
`on_retriever_error`. In sync mode, returned documents are scanned before reaching the model and
block exceptions propagate. Async mode remains observation. Metadata `agent_id`/`agent_type`
is inherited from parent runs and sent on tool events; direct scan calls accept the same fields.

Use `ggate.guard_store(store, sdk_client=client)` around a backing store with `put/get` or
`aput/aget`. The facade checks writes before invoking the store and checks read values before
returning them. It always requests enforcement. Batch/delete methods are not exposed; call the
wrapped methods for protected access. It does not intercept an unwrapped store or remote memory.

`client.scan_memory_write(content, store="memory", operation="put")` and
`await client.scan_memory_write_async(...)` scan JSON-compatible plaintext and metadata before
persistence. The module-level equivalents are also exported. They always wait, even in async
mode, and raise `GgateBlockedError` on Block by default. Pass `enforce=False` only when the caller
will inspect the returned decision before writing. Store facades always enforce.

For vector stores, supply a selector for all plaintext and metadata in the batch:

```python
protected = ggate.guard_vector_store(
    collection, sdk_client=client,
    content_of=lambda operation, args, kwargs: {
        "documents": kwargs["documents"], "metadatas": kwargs.get("metadatas")
    },
)
protected.upsert(ids=["note-1"], documents=["The customer prefers concise answers."])
```

The facade exposes existing `add`, `upsert`, `update`, `add_texts`, `add_documents` methods and
their `a`-prefixed async forms; native async methods also work. It scans the entire batch before
one provider call and preserves the provider's arguments and return value. No other methods are
forwarded. Materialize iterators before passing them, and adapt `content_of` to your provider's
arguments. Embeddings alone cannot reveal poisoned instructions; supply their original text or
scan before embedding with `scan_memory_write`.

Enable **Block poisoned memory writes** in Console Policies and select **Block** (the shipped
default is Monitor). Requires SDK and Gate builds containing this control. The event remains a
pre-action `mcp_tool` with tool `memory.write`, so normal rules, models, identity and storage
redaction apply. Transport failures retain the SDK's configured outage/fail-open behavior;
these guards are not an offline guarantee. Unwrapped writes, provider-side memory and delete
APIs remain outside the facade.
