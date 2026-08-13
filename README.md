# ggate Python SDK

Python package for monitoring AI agent framework prompts, responses, files, and tool activity.
Scans reach a detection engine two ways, chosen from the configuration: the local `ggate-agent`
over mTLS (default), or the Console API for applications with no per-device agent.

```python
import ggate

decision = ggate.scan_prompt("Summarize this document", framework="langchain")
if decision.blocked:
    raise RuntimeError(decision.message)
```

Console mode needs the Console URL and an IAM API key (Console UI → Admin → API keys); the
Console runs the full pipeline for each scan, records the event, and returns the verdict:

```bash
GGATE_CONSOLE_URL=https://godels-gate.example.com
GGATE_API_KEY=godel_...
```

See the repository README one directory up for the transports, verdict semantics, latency and
fail-open contract, configuration, and complete examples.
