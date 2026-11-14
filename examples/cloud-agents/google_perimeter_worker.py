"""Run on a cloud VM inside a VPC Service Controls perimeter. Read secrets from stdin."""

import json
import os
import socket
import sys
import urllib.parse
import urllib.request
import uuid

from ggate.core.client import Client
from ggate.core.config import Config
from ggate.core.console_transport import ConsoleTransport


class ObservedTransport(ConsoleTransport):
    def __init__(self, config):
        super().__init__(config)
        self.destinations = set()
        self.events = []

    def _urlopen(self, request):
        self.destinations.add(urllib.parse.urlparse(request.full_url).hostname)
        return super()._urlopen(request)

    def request(self, request):
        result = super().request(request)
        if request.get("op") == "scan_text":
            self.events.append(
                {
                    "id": request["event"]["id"],
                    "verdict": result.get("verdict"),
                    "fail_open": result.get("fail_open", False),
                }
            )
        return result


def main():
    settings = json.load(sys.stdin)
    os.environ["GGATE_CONSOLE_URL"] = settings["url"]
    config = Config.from_values(
        api_key=settings["collector_api_key"],
        mode="sync",
        timeout=30,
        cooldown=0,
        agent_name="Perimeter Operations Assistant",
        host="google-cloud-vm",
        user="cloud-operations",
        team="Platform Engineering",
        workstation_id="gcp-perimeter-worker",
    )
    transport = ObservedTransport(config)
    sdk = Client(config, transport=transport)
    sid = settings.get("session_id", str(uuid.uuid4()))
    meta = {"session_id": sid, "framework": "google-cloud", "provider": "vertex-ai"}
    prompt = "Summarize this order in one sentence: SO-1042, two standing desks, Sydney, shipping AUD 35."
    decision = sdk.scan_prompt(prompt, enforce=True, **meta)
    if decision.fail_open:
        raise RuntimeError("Prompt scan delivery failed")
    metadata = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"},
    )
    with urllib.request.urlopen(metadata, timeout=10) as response:
        token = json.load(response)["access_token"]
    endpoint = "aiplatform.googleapis.com"
    addresses = sorted(
        {r[4][0] for r in socket.getaddrinfo(endpoint, 443, type=socket.SOCK_STREAM)}
    )
    if not addresses or any(
        a not in {"199.36.153.4", "199.36.153.5", "199.36.153.6", "199.36.153.7"}
        for a in addresses
    ):
        raise RuntimeError("Model API is not using the restricted Google API VIP")
    request = urllib.request.Request(
        f"https://{endpoint}/v1/projects/{settings['project']}/locations/global/publishers/google/models/gemini-3.5-flash:generateContent",
        data=json.dumps(
            {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 1000},
            }
        ).encode(),
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(
            f"Vertex AI returned {error.code}: {error.read().decode()}"
        ) from error
    answer = "\n".join(
        p.get("text", "") for p in result["candidates"][0]["content"]["parts"]
    )
    decision = sdk.scan_response(answer, enforce=True, **meta)
    if decision.fail_open or not sdk.flush(timeout=30):
        raise RuntimeError("Response scan delivery failed")
    expected = urllib.parse.urlparse(settings["url"]).hostname
    if transport.destinations != {expected}:
        raise RuntimeError("Unexpected SDK HTTP destination")
    denied = False
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=4):
            pass
    except (OSError, TimeoutError):
        denied = True
    if not denied:
        raise RuntimeError("Unapproved internet destination is reachable")
    print(
        json.dumps(
            {
                "session_id": sid,
                "passed": True,
                "response": answer,
                "model_api_addresses": addresses,
                "sdk_http_destinations": sorted(transport.destinations),
                "other_internet_egress_denied": denied,
                "events": transport.events,
            }
        )
    )


if __name__ == "__main__":
    main()
