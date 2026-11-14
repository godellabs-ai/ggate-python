# Cloud agent applications

These Python applications use synthetic order data and never change real orders. They demonstrate Godel Gate scans at application-controlled boundaries with AWS, Azure and Google Cloud models. The framework runs and model requests are real.

## Setup

Use Python 3.12 and a separate virtual environment for each framework family. Install this SDK from the repository root with `pip install -e .`, then install the relevant requirements file from this directory. Set these variables through your process environment or secret manager:

```
GGATE_CONSOLE_URL=https://gate.your-company.com
GGATE_API_KEY=<collector API key>
```

Run applications from this directory, for example `python google_adk_agent.py`. Application names and team defaults are supplied in `common.py`; override them with `GGATE_AGENT_NAME` and `GGATE_TEAM`. The shared client preserves the SDK's user and workstation identity resolution, including `GGATE_USER` / `GGATE_USER_EMAIL` and `GGATE_WORKSTATION_ID` overrides. Bind the authenticated caller per request in shared services. No administrator key is needed to scan. `GGATE_ADMIN_API_KEY` is optional and is used only by the local verification helper to reconcile event IDs with the Console. Never include it in a deployed workload. Without it, the helper reports submitted events and asks you to verify Activity; it does not claim reconciliation.

## Applications

| Application | Configuration | Coverage |
| --- | --- | --- |
| `aws_langgraph_agent.py` | AWS credentials, Bedrock access in `ap-southeast-2`, optional `BEDROCK_MODEL_ID` | Existing LangChain callback; prompt, tools, tool results, responses |
| `aws_strands_agent.py` | Same AWS prerequisites | Three framework hooks plus a buffered final-response scan; tool cancellation and result replacement enforce verdicts |
| `lambda_worker.py` | Lambda handler `lambda_worker.handler`, `GGATE_SECRET_ARN`, Console URL, Secrets Manager read permission | Blocked admission followed by explicit flush before exit; intentionally makes no model call |
| `azure_semantic_kernel.py` | `AZURE_FOUNDRY_ANTHROPIC_URL`, `AZURE_FOUNDRY_DEPLOYMENT`, `AZURE_FOUNDRY_API_KEY` | Existing Semantic Kernel prompt and function filters |
| `azure_microsoft_agents.py` | Same Azure model configuration | Two middleware classes around model requests/responses and function calls/results |
| `azure_foundry_agent.py` | Above plus `AZURE_FOUNDRY_PROJECT_ENDPOINT`, Azure CLI login and project agent permissions | Four manual scan boundaries; uses the preview agent endpoint (`allow_preview=True`), creates a uniquely named temporary agent and conversation, and cleans them up on exit |
| `google_adk_agent.py` | `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION=australia-southeast1`, Application Default Credentials or `GOOGLE_OAUTH_ACCESS_TOKEN` | Four ADK callbacks; prompt, tool call, tool result, final buffered response |
| `google_perimeter_worker.py` | Run on a Google VM; JSON on stdin supplies project, Console URL, collector key and session ID | Real model request through restricted Google APIs, observed SDK destinations, negative internet-egress check |

Confirm model access before deploying infrastructure. Catalog visibility is not invocation permission. Verify the AWS examples end to end in your target account before rollout. AgentCore hosting needs a separate deployment and coverage check; it is not covered by the local Strands application.

The Google ADK application uses a regional model. The perimeter worker uses Gemini 3.5 Flash at the global endpoint; its VM is in Sydney. Do not infer model data residency from the VM location. The worker assumes restricted Google API DNS and firewall rules are already in place and fails if those controls are absent.

## Enforcement and boundaries

A scan with `fail_open=True` means the content was not reliably evaluated. Manual scan callbacks reject or withhold failed scan results. Existing framework adapters follow the SDK configuration, while the verification helper checks delivery after the workflow. The AWS callback adapter follows the SDK's configured enforcement semantics; the verification helper additionally rejects failed scan delivery. Do not infer strict fail-closed transport behavior from a successful callback run.

The managed Foundry example covers custom functions executed by the caller. Internal model turns and hosted service tools are not intercepted. The Microsoft middleware is intended for buffered output; it rejects streaming. Strands suppresses its default streaming callback and checks the completed answer before printing it.

The Lambda workload requires a prompt blocked by your configured policy. It returns event IDs and flush duration so a trusted operator can reconcile them in Activity. A forced process kill or expired deadline cannot be repaired by a flush that never completes.

Integration effort is visible in the source: two middleware classes for Microsoft Agent Framework, four manual scan boundaries and a dispatch loop for Foundry, and four callbacks for ADK. Treat production hardening, identity design, retry behavior and managed-tool coverage as separate work.

## Verification

The runnable examples generate a fresh session ID; the Lambda and perimeter workers also accept one supplied by the caller. Verify the expected tool names, final response, scan delivery status and stored event IDs. The helper checks prompt, response, expected tool-call and tool-result coverage even without an administrator credential. It records failed transport attempts and rejects incomplete delivery. When enabled, local reconciliation writes full synthetic events under `evidence/` (ignored by Git); use only synthetic input or a protected output directory.

Frameworks may make different numbers of model turns, so total scan counts can vary. Use bounded scripted corpora when comparing controls; preserve actual verdict disagreements.
