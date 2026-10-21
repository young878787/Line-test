"""匯出單一流程、來源雜湊與驗證摘要；不改變常駐服務的啟用狀態。"""

from datetime import datetime, timezone
import hashlib
import json
import subprocess
import uuid

from mcp_client import LocalN8n, ROOT, LOGS, save
from build_workflows import DESCRIPTION, unpack


def main():
    client = LocalN8n()
    manifest = json.loads((ROOT / "workflows/manifest.json").read_text(encoding="utf-8"))
    workflow = unpack(client.call("get_workflow_details", {"workflowId": manifest["id"]}))["workflow"]
    save(ROOT / "workflows/workflow.json", {**{k: workflow[k] for k in ("name", "nodes", "connections", "settings")}, "description": DESCRIPTION, "active": False})
    validations = {name: unpack(json.loads((LOGS / f"{name}.json").read_text(encoding="utf-8"))) for name in ("nodes_validation", "workflow_validation")}
    assert all(v.get("valid") is True and not v.get("errors") and not v.get("warnings") for v in validations.values())
    assert all(uuid.UUID(node["id"]).version == 4 for node in workflow["nodes"])
    assert not workflow["settings"].get("errorWorkflow")
    assert workflow["connections"]["Workflow Failed"]["main"][0][0]["node"] == "Save Sanitized Failure"
    assert all(n["parameters"]["url"].startswith("http://127.0.0.1:5682/") for n in workflow["nodes"] if n["type"] == "n8n-nodes-base.httpRequest")
    save(LOGS / "executions.json", unpack(client.call("search_workflow_executions", {"workflowId": manifest["id"], "limit": 200})))
    files = [{"path": path.relative_to(ROOT.parent).as_posix(), "lines": len(path.read_bytes().splitlines()),
              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in sorted((ROOT.parent / "src/weather_bot").glob("*.py"))]
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT.parent, capture_output=True, text=True, check=True).stdout.strip()
    save(LOGS / "source_manifest.json", {"commit": revision, "files": files})
    latest = json.loads((LOGS / "latest.json").read_text(encoding="utf-8"))
    max_reply = 0
    for case in latest["cases"]:
        evidence = json.loads((ROOT / case["evidence"]).read_text(encoding="utf-8"))
        assert evidence["external_network_calls"] == 0
        for reply in evidence["replies"]:
            assert len(reply["payload"]["messages"]) == 1
            max_reply = max(max_reply, reply["elapsed_seconds"])
    assert max_reply < 60
    save(LOGS / "verification.json", {"checked_at": datetime.now(timezone.utc).isoformat(), "n8n_version": "2.35.7",
         "workflow_id": manifest["id"], "workflow_count": 1, "node_count": len(workflow["nodes"]),
         "validation_passed": True, "same_workflow_error_handler": True, "all_http_targets_local": True,
         "connections": workflow["connections"], "test_report": "logs/latest.json", "service_tests": "logs/service_tests.json",
         "runtime_tests": "logs/runtime_tests.json", "public_https": "logs/public_https.json",
         "max_mock_reply_seconds": round(max_reply, 4), "external_business_network_calls": 0})
    client.http.close()
    print("Single workflow export and verification saved.")


if __name__ == "__main__":
    main()
