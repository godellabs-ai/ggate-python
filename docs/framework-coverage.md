# Framework Coverage

The SDK is a collector: it captures framework activity and sends it to the Console's scan API
(`GGATE_CONSOLE_URL` + `GGATE_API_KEY`), which is its only destination. Sync mode waits for that
verdict before the framework call proceeds; async mode queues events and never blocks the
conversation.

## Coverage Matrix

| Framework | Python | Node.js | Integration point | Enforcement |
|---|---:|---:|---|---|
| LangChain | Yes | Yes | Callback handler | Prompt/tool preflight in sync mode |
| LangGraph | Yes | Yes | LangChain-compatible callback handler | Prompt/tool preflight in sync mode |
| CrewAI | Yes | N/A | CrewAI event listener | Prompt/tool preflight where start events fire |
| AutoGen / AG2 | Yes | Yes | Public `run` / stream method wrapper | Run-boundary preflight |
| OpenAI Chat/Responses | Yes | Yes | OpenAI client wrapper | Prompt preflight, response audit |
| OpenAI Assistants | Yes | Yes | Files, thread messages, thread runs, tool outputs | Message/run/file preflight; response/tool audit |
| OpenAI Swarm | Yes | Yes | Swarm `run(...)` wrapper plus Chat Completions underneath | Run-boundary preflight |
| LlamaIndex | Yes | Yes | Callback handler | Event-start preflight, event-end audit |
| Haystack | Yes | N/A | Pipeline/Agent `run` wrapper | Run-boundary preflight |
| Semantic Kernel | Yes | Yes | Prompt/function filters | Prompt/function preflight |
| DSPy | Yes | N/A | Module/callable wrapper | Module-call preflight |
| Phidata / Agno | Yes | N/A | Agent `run` / `print_response` wrapper | Run-boundary preflight |

## Official-Docs Basis

- LangChain Python and JS expose callback handlers for LLM/tool lifecycle events.
- LangGraph uses the LangChain callback model for graph execution instrumentation.
- CrewAI exposes event listeners for monitoring integrations.
- AutoGen AgentChat exposes `run` and `run_stream` methods on agents.
- OpenAI Assistants has thread messages, runs, tools, and files, but is deprecated and scheduled for
  shutdown on August 26, 2026. Keep coverage for existing customers, but prefer Responses/Agents for
  new development.
- OpenAI Swarm is stateless and powered by Chat Completions, so the OpenAI chat wrapper covers most
  calls. A direct `run(...)` wrapper is also provided.
- LlamaIndex exposes callback managers/handlers for event start/end.
- Haystack exposes pipeline/agent run surfaces and tracing; the SDK wraps run boundaries rather than
  relying on private internals.
- Semantic Kernel exposes prompt render, function invocation, and automatic function invocation
  filters.
- DSPy documents observability and custom callback/logging patterns; the SDK uses module/callable
  wrappers for deterministic coverage.
- Phidata provides built-in monitoring, but that sends to Phidata. The SDK wraps agent run methods
  so events still flow through Godel's Gate.

## Important Limitations

- Streaming token-by-token blocking is not implemented yet. Streaming wrappers audit the completed
  stream/output when the stream is consumed.
- For frameworks without stable public callbacks, run-boundary wrappers see inputs/outputs but not
  every internal tool step unless the framework exposes it through return values/events.
- Generated files are captured when they pass through visible file APIs or framework payloads. If a
  framework writes directly to disk without an event or API object, the customer must call
  `scan_file(...)` or attach generated-file metadata manually.
- Python-only frameworks are intentionally not exposed as Node adapters.
- SDK-specific Rust enum variants are still needed in the main product for first-class UI labels.
  Until then the SDK emits `agent_source: "generic"` and stores the precise framework in
  `collector.labels.framework`.

## References

- LangChain Python callbacks: https://reference.langchain.com/python/langchain-core/callbacks
- LangChain JS callbacks: https://reference.langchain.com/javascript/langchain-core/callbacks/base/CallbackHandlerMethods
- CrewAI event listeners: https://docs.crewai.com/en/concepts/event-listener
- AutoGen AgentChat agents: https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/agents.html
- OpenAI Assistants deep dive/deprecation: https://developers.openai.com/api/docs/assistants/deep-dive
- OpenAI Swarm: https://github.com/openai/swarm
- LlamaIndex callbacks: https://developers.llamaindex.ai/python/framework/module_guides/observability/callbacks/
- Haystack tracing: https://docs.haystack.deepset.ai/docs/tracing
- Haystack pipeline breakpoints/snapshot callbacks: https://docs.haystack.deepset.ai/docs/pipeline-breakpoints
- Semantic Kernel filters: https://learn.microsoft.com/en-us/semantic-kernel/concepts/enterprise-readiness/filters
- DSPy observability: https://dspy.ai/tutorials/observability/
- Phidata monitoring: https://docs.phidata.com/monitoring
