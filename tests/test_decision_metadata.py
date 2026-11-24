import json
from pathlib import Path

from ggate import Decision

RESPONSE = json.loads((Path(__file__).parent / "fixtures/scan-response.json").read_text())


def test_preserve_scan_coverage_independently_of_verdict():
    for verdict in ("pass", "block"):
        decision = Decision.from_response({**RESPONSE, "verdict": verdict})
        assert decision.blocked is (verdict == "block")
        assert not decision.fail_open
        assert decision.console_outage_action == "allow"
        assert decision.to_json()["console_outage_fallback"] == "block"
        assert decision.pipeline_status == RESPONSE["pipeline_status"]
        assert decision.document_intelligence.to_json() == RESPONSE["document_intelligence"]
        assert decision.to_json()["pipeline_status"] == RESPONSE["pipeline_status"]


def test_missing_verdict_and_detection_link():
    assert Decision.from_response({"kind": "verdict"}).fail_open
    url = "https://console.example/detections?event=test"
    decision = Decision.from_response({"verdict": "block", "detection_url": url})
    assert decision.detection_url == decision.to_json()["detection_url"] == url


def test_older_responses_and_queued_passes_need_no_metadata():
    assert Decision.from_response({"verdict": "pass"}).pipeline_status is None
    assert "pipeline_status" not in Decision.pass_().to_json()
