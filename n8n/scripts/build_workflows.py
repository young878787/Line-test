"""透過官方 MCP 建立單一可常駐 workflow，只操作專用 5680 實例。"""

import json
import sys

from mcp_client import LocalN8n, ROOT, LOGS, save

sys.path.insert(0, str(ROOT))
from runtime_config import ADAPTER_URL, INTERNAL_HEADER, internal_key

SKILLS = ["using-n8n-mcp-skills", "n8n-workflow-patterns", "n8n-mcp-tools-expert",
          "n8n-node-configuration", "n8n-expression-syntax", "n8n-error-handling", "n8n-validation-expert"]


def unpack(result):
    if result.get("structuredContent"):
        return result["structuredContent"]
    for content in result.get("content", []):
        if content.get("type") == "text":
            try:
                return json.loads(content["text"])
            except ValueError:
                pass
    raise ValueError("MCP 回覆缺少結構化結果")


def credential(client):
    name = "LINE n8n Internal"
    credentials = client.http.get("/rest/credentials").raise_for_status().json()["data"]
    matches = [c for c in credentials if c["name"] == name and c["type"] == "httpHeaderAuth"]
    if len(matches) > 1:
        raise ValueError("專用內部 credential 名稱重複")
    if matches:
        return {"id": matches[0]["id"], "name": name}
    result = client.http.post("/rest/credentials", json={"name": name, "type": "httpHeaderAuth",
                              "data": {"name": INTERNAL_HEADER, "value": internal_key()}}).raise_for_status().json()["data"]
    return {"id": result["id"], "name": name}


def source(cred, *, mock=False):
    configs = []
    declarations = []

    def define(var, kind, version, name, params, position, **settings):
        config = {"name": name, "parameters": params, "position": position, **settings}
        sample = {"status": "ok"}
        if name == "LINE Raw Webhook":
            sample = {"headers": {"x-line-signature": "test", "x-received-at": "0", "x-received-proof": "test"}, "body": {"events": []}}
        elif name == "Verify and Persist":
            sample = {"statusCode": 200, "body": {"status": "ok"}}
        elif kind == "errorTrigger":
            sample = {"workflow": {"id": "sample"}, "execution": {"id": "sample", "lastNodeExecuted": "sample"}}
        spec = {"type": "n8n-nodes-base." + kind, "version": version, "config": config, "output": [sample]}
        factory = "trigger" if kind in {"webhook", "scheduleTrigger", "errorTrigger"} else "node"
        declarations.append("const " + var + " = " + factory + "(" + json.dumps(spec, ensure_ascii=False) + ");")
        configs.append(spec)

    auth = {"authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth"}
    creds = {"httpHeaderAuth": cred}
    headers = [{"name": "Connection", "value": "keep-alive"}]
    base = {"method": "POST", **auth, "sendHeaders": True, "headerParameters": {"parameters": headers}, "options": {"timeout": 295000}}
    define("incoming", "webhook", 2.1, "LINE Raw Webhook", {"httpMethod": "POST", "path": "line-weather", "responseMode": "responseNode", "options": {"rawBody": True}}, [0, 0])
    define("recover", "scheduleTrigger", 1.3, "Recover Pending Chats", {"rule": {"interval": [{"field": "seconds", "secondsInterval": 1}]}}, [520, -240])
    define("schedule", "scheduleTrigger", 1.3, "Background Every Second", {"rule": {"interval": [{"field": "seconds", "secondsInterval": 1}]}}, [0, 440], disabled=mock)
    define("tick", "webhook", 2.1, "Run Background Tick", {"httpMethod": "POST", "path": "line-weather-tick", "authentication": "headerAuth", "responseMode": "lastNode", "options": {}}, [0, 680], credentials=creds)
    forwarded = headers + [{"name": name, "value": "={{ $json.headers['" + name + "'] ?? '' }}"}
                           for name in ("x-line-signature", "x-received-at", "x-received-proof")]
    ingress = {**base, "url": ADAPTER_URL + "/ingress", "headerParameters": {"parameters": forwarded},
               "sendBody": True, "contentType": "binaryData", "inputDataFieldName": "data",
               "options": {"timeout": 10000, "response": {"response": {"fullResponse": True, "neverError": True, "responseFormat": "json"}}}}
    define("accept", "httpRequest", 4.5, "Verify and Persist", ingress, [260, 0], credentials=creds,
           onError="continueErrorOutput", retryOnFail=True, maxTries=3, waitBetweenTries=100)
    define("ack", "respondToWebhook", 1.5, "Return Verified Status", {"respondWith": "json", "responseBody": "={{ $json.body }}", "options": {"responseCode": "={{ $json.statusCode }}"}}, [520, 0])
    define("drain", "httpRequest", 4.5, "Process Chat Jobs", {**base, "url": ADAPTER_URL + "/jobs/chats"}, [780, 0], credentials=creds, retryOnFail=False)
    define("reject", "respondToWebhook", 1.5, "Adapter Unavailable", {"respondWith": "json", "responseBody": '{"error":"adapter_unavailable"}', "options": {"responseCode": 503}}, [520, 220])
    define("work", "httpRequest", 4.5, "Weather Outbox and Memory", {**base, "url": ADAPTER_URL + "/jobs/background"}, [350, 540], credentials=creds, retryOnFail=True, maxTries=3, waitBetweenTries=100)
    define("failed", "errorTrigger", 1, "Workflow Failed", {}, [0, 940])
    define("record", "httpRequest", 4.5, "Save Sanitized Failure", {**base, "url": ADAPTER_URL + "/errors", "sendBody": True,
           "specifyBody": "json", "jsonBody": "={{ { workflow_id: $json.workflow.id, execution_id: $json.execution.id, node: $json.execution.lastNodeExecuted } }}"}, [350, 940],
           credentials=creds, retryOnFail=True, maxTries=2, waitBetweenTries=1000)
    declarations.append("export default workflow('line-weather-bot', 'LINE Weather Bot').add(incoming).to(accept).to(ack).to(drain).add(accept.onError(reject)).add(recover).to(drain).add(schedule).to(work).add(tick).to(work).add(failed).to(record);")
    return "\n".join(declarations) + "\n", configs


def build(*, mock=False, client=None):
    client = client or LocalN8n()
    code, specs = source(credential(client), mock=mock)
    path = ROOT / "workflows/manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    save(LOGS / "nodes_validation.json", client.call("validate_node_config", {"nodes": [
        {"name": s["config"]["name"], "type": s["type"], "typeVersion": s["version"], "parameters": s["config"]["parameters"]} for s in specs]}))
    validated = client.call("validate_workflow", {"code": code})
    save(LOGS / "workflow_validation.json", validated)
    result = unpack(validated)
    if not result.get("valid") or result.get("errors"):
        raise ValueError("Workflow SDK 驗證失敗")
    (ROOT / "workflows/workflow.js").write_text(code, encoding="utf-8")
    if "id" not in manifest:
        created = unpack(client.call("create_workflow_from_code", {"code": code, "skillsUsed": SKILLS,
                       "versionName": "Unify webhook scheduling and error handling", "description": "單一 LINE Weather workflow；服務由 n8n/service.py 常駐管理。"}))
        workflow_id = created["workflowId"]
        detail = unpack(client.call("get_workflow_details", {"workflowId": workflow_id}))["workflow"]
        assert len(detail["nodes"]) == len(specs) and detail["connections"]
        retired = []
        for entry in manifest.values():
            client.call("unpublish_workflow", {"workflowId": entry["id"]})
            client.call("archive_workflow", {"workflowId": entry["id"]})
            retired.append(entry["id"])
        if retired:
            save(LOGS / "migration.json", {"retired_workflow_ids": retired, "replacement_workflow_id": workflow_id, "retirement": "archived"})
        manifest = {"id": workflow_id, "name": "LINE Weather Bot", "url": f"http://127.0.0.1:5680/workflow/{workflow_id}"}
        save(path, manifest)
    workflow_id = manifest["id"]
    detail = unpack(client.call("get_workflow_details", {"workflowId": workflow_id}))["workflow"]
    operations = []
    for spec in specs:
        config = spec["config"]
        current = next(n for n in detail["nodes"] if n["name"] == config["name"])
        if current["parameters"] != config["parameters"]:
            operations.append({"type": "updateNodeParameters", "nodeName": current["name"], "parameters": config["parameters"], "replace": True})
        if bool(current.get("disabled")) != bool(config.get("disabled")):
            operations.append({"type": "setNodeDisabled", "nodeName": current["name"], "disabled": bool(config.get("disabled"))})
    settings = {"timezone": "Asia/Taipei", "executionTimeout": 300, "executionOrder": "v1", "saveManualExecutions": True,
                "saveDataErrorExecution": "all", "saveDataSuccessExecution": "all" if mock else "none"}
    if any(detail["settings"].get(k) != v for k, v in settings.items()):
        operations.append({"type": "setWorkflowSettings", "settings": settings})
    if operations:
        client.call("update_workflow", {"workflowId": workflow_id, "operations": operations, "versionName": "Apply verified service settings", "skillsUsed": SKILLS})
        detail = unpack(client.call("get_workflow_details", {"workflowId": workflow_id}))["workflow"]
    assert detail["connections"]["Workflow Failed"]["main"][0][0]["node"] == "Save Sanitized Failure"
    assert not detail["settings"].get("errorWorkflow"), "錯誤處理必須留在同一 workflow"
    save(ROOT / "workflows/workflow.json", {k: detail[k] for k in ("name", "nodes", "connections", "settings")})
    return manifest


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock", action="store_true")
    print(build(mock=parser.parse_args().mock)["url"])
