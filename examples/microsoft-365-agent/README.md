# Microsoft 365 Status Assistant

A Microsoft 365 Agents SDK application demonstrating Gate 0.2.1 middleware, a guarded tool,
and checks before tool results and responses are delivered. The status tool returns **demo data**;
this example does not call a model or Microsoft Graph. Replace its body with your own operation,
keeping the preflight before the operation and the result scan before returning its output.

From this directory, with Python 3.10 or newer:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Fill in the Gate URL/API key and your Microsoft agent registration credentials.
python app.py
```

For a local terminal demo using real Microsoft middleware but no Microsoft channel credentials,
set only the Gate URL/API key and run `python app.py --console`. Enter a service name, then an empty
line to exit. This sends scans to Gate; status values are still demo data.

Use Microsoft's Agents Playground with `http://localhost:3978/api/messages` for local testing,
or register your authenticated HTTPS deployment with Microsoft 365/Teams. `/health` checks only
the HTTP host, not Gate or Microsoft authentication. Follow Microsoft's current setup guide:
https://learn.microsoft.com/en-us/microsoft-365/agents-sdk/quickstart

The app retains Microsoft's JWT authorization middleware; it does not expose an unauthenticated
production endpoint. Configure the connection for your tenant before deploying. The host binds
to loopback by default. No messages are sent until a client calls the application.

An incoming message is scanned, then `get_service_status` is scanned before execution, its result
is checked before delivery, and the outgoing activity is checked by middleware. Conversation
metadata also flows into the explicit Python scans through `ContextVar` scope. Blocks stop the
turn and appear in Gate Activity; the example does not echo blocked content back to the user.

Gate's SDK transport fails open on connection/authentication failures. Inspect `decision.fail_open`
and `pipeline_status` if your application requires a stricter availability or coverage policy.
Remote attachments are metadata-only; card content is scanned, but attachment URLs are not fetched.
Middleware cannot inspect execution inside Microsoft-managed services.

Refresh the lock with `uv pip compile requirements.in -o requirements.txt` from this directory.
Run the local HTTP authentication-boundary test with `python -m unittest -v test_example.py`.
