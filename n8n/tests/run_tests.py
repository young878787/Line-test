"""完整 mock 驗證、真實程序恢復與僅空事件的公開 HTTPS 探測。"""

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from runtime_config import ADAPTER_URL, GATEWAY_URL, INTERNAL_HEADER, RUNTIME, internal_key, load_settings
from processes import NodeRuntime, STARTUP_TIMEOUT, port_open
from mcp_client import LocalN8n, LOGS, save
from build_workflows import build, unpack
from tunnel import N8nTunnel


def state():
    return json.loads((RUNTIME / "service.json").read_text(encoding="utf-8"))


def wait_for(predicate, timeout=STARTUP_TIMEOUT + 150, process=None):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if process is not None and process.poll() is not None:
            raise RuntimeError("服務已結束；請檢查 n8n/.runtime/test_service.log")
        try:
            result = state()
            if predicate(result):
                return result
        except (FileNotFoundError, ValueError):
            pass
        time.sleep(.2)
    raise TimeoutError("服務狀態未在期限內完成；請檢查 n8n/.runtime/test_service.log")


def launch():
    log = (RUNTIME / "test_service.log").open("w", encoding="utf-8")
    started = datetime.now(timezone.utc)
    process = subprocess.Popen([sys.executable, str(ROOT / "service.py"), "--mock"], cwd=ROOT.parent,
                               stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        # Windows venv python.exe 是 launcher；服務實際 PID 可能是它的子程序。
        ready = wait_for(lambda s: s["status"] == "ready" and s["mode"] == "mock"
                         and datetime.fromisoformat(s["checked_at"]) >= started, process=process)
    except BaseException:
        log.close()
        raise
    return process, log, ready


def stop(process, log):
    try:
        if process.poll() is None:
            httpx.post(ADAPTER_URL + "/service/stop", headers={INTERNAL_HEADER: internal_key()}, timeout=5, trust_env=False).raise_for_status()
            wait_for(lambda s: s["status"] == "stopped", timeout=45)
            process.wait(timeout=10)
        if any(port_open(p) for p in (5680, 5681, 5682, 5683)):
            raise AssertionError("停止後仍有本專案 listener")
    finally:
        log.close()


async def public_probe():
    tunnel = N8nTunnel(load_settings(True), mock=True, directory=RUNTIME / "public_probe")
    await tunnel.start()
    try:
        async with asyncio.timeout(180):
            while True:
                path = tunnel.directory / "latest.json"
                if path.exists():
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if value["status"] == "ready":
                        break
                await asyncio.sleep(.25)
        base = tunnel.endpoint.removesuffix("/webhook")
        async with httpx.AsyncClient(timeout=15, trust_env=False) as http:
            hidden = {route: (await http.post(base + route)).status_code for route in ("/rest/settings", "/mcp-server/http", "/mock/reset", "/service/stop")}
        assert all(code == 404 for code in hidden.values())
        save(LOGS / "public_https.json", {"status": "passed", "checked_at": datetime.now(timezone.utc).isoformat(),
             "endpoint": tunnel.endpoint, "checks": value["checks"],
             "hidden_routes": hidden, "business_services": "mock", "line_endpoint_updated": False})
    except BaseException as error:
        path = tunnel.directory / "latest.json"
        save(LOGS / "public_https.json", {"status": "failed", "checked_at": datetime.now(timezone.utc).isoformat(),
             "error": type(error).__name__, "tunnel": json.loads(path.read_text()) if path.exists() else {},
             "business_services": "mock", "line_endpoint_updated": False})
        raise
    finally:
        await tunnel.stop()
    assert tunnel.process is None


def main():
    RUNTIME.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    if port_open(5682) or port_open(5683):
        raise RuntimeError("請先使用 n8n/stop.ps1 停止本專案服務，再執行完整測試")
    cases = []
    process = log = None
    try:
        print("Starting isolated mock service...", flush=True)
        process, log, first = launch()
        cases.append({"id": "single_command_start", "status": "passed", "state": first})
        for filename, output in (("test_service.py", "service_test_output.log"), ("test_workflows.py", "test_output.log")):
            print("Running " + filename, flush=True)
            with (LOGS / output).open("w", encoding="utf-8") as file:
                result = subprocess.run([sys.executable, str(ROOT / "tests" / filename)], cwd=ROOT.parent, stdout=file, stderr=subprocess.STDOUT)
            if result.returncode:
                raise RuntimeError(filename + " failed; inspect n8n/logs/" + output)
        duplicate = subprocess.run([sys.executable, str(ROOT / "service.py"), "--mock"], cwd=ROOT.parent,
                                   capture_output=True, timeout=15)
        assert duplicate.returncode == 1 and state()["service_pid"] == first["service_pid"]
        cases.append({"id": "duplicate_start_refused", "status": "passed"})
        print("Simulating dedicated n8n child exit...", flush=True)
        node_pid = state()["n8n_pid"]
        assert NodeRuntime().verify_owner() == node_pid
        subprocess.run(["taskkill", "/PID", str(node_pid), "/T", "/F"], capture_output=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
        recovered = wait_for(lambda s: s["status"] == "ready" and s["n8n_restarts"] >= 1 and s["n8n_pid"] != node_pid)
        assert recovered["workflow_id"] == first["workflow_id"]
        cases.append({"id": "n8n_child_exit_recovers_same_workflow", "status": "passed", "state": recovered})
        # 啟用正式排程拓撲，業務端仍是隔離 mock，驗證兩個常駐分支皆可執行。
        client = LocalN8n()
        manifest = build(mock=False, client=client)
        client.call("update_workflow", {"workflowId": manifest["id"], "operations": [{"type": "setWorkflowSettings",
                    "settings": {"saveDataSuccessExecution": "all"}}]})
        client.call("publish_workflow", {"workflowId": manifest["id"]})
        time.sleep(2)
        detail = unpack(client.call("get_workflow_details", {"workflowId": manifest["id"]}))["workflow"]
        assert not any(n.get("disabled") for n in detail["nodes"] if n["type"] == "n8n-nodes-base.scheduleTrigger")
        executions = unpack(client.call("search_workflow_executions", {"workflowId": manifest["id"], "limit": 10}))["data"]
        observed = None
        for execution in executions:
            if execution["status"] != "success":
                continue
            data = unpack(client.call("get_workflow_execution", {"workflowId": manifest["id"], "executionId": execution["id"],
                          "includeData": True, "nodeNames": ["Background Every Second", "Weather Outbox and Memory"]}))
            nodes = data.get("data", {}).get("resultData", {}).get("runData", {})
            if "Background Every Second" in nodes and "Weather Outbox and Memory" in nodes:
                assert nodes["Weather Outbox and Memory"][0]["data"]["main"][0][0]["json"]["status"] == "ok"
                observed = execution["id"]
                break
        assert observed is not None
        cases.append({"id": "live_schedule_topology_with_mock_services", "status": "passed", "execution_id": observed})
        print("Probing public HTTPS with empty events; LINE configuration stays unchanged...", flush=True)
        asyncio.run(public_probe())
        client.http.close()
        stop(process, log)
        process = log = None
        cases.append({"id": "graceful_stop_releases_all_listeners", "status": "passed"})
        print("Checking full service restart...", flush=True)
        process, log, restarted = launch()
        assert restarted["workflow_id"] == first["workflow_id"]
        client = LocalN8n()
        manifest = build(mock=False, client=client)
        client.call("publish_workflow", {"workflowId": manifest["id"]})
        subprocess.run([sys.executable, str(ROOT / "scripts/finalize.py")], cwd=ROOT.parent, check=True)
        client.http.close()
        stop(process, log)
        process = log = None
        cases.append({"id": "restart_reuses_account_workflow_and_database", "status": "passed", "state": restarted})
        save(LOGS / "runtime_tests.json", {"status": "passed", "total": len(cases), "passed": len(cases), "failed": 0, "cases": cases})
        print("Mock workflows, service recovery, public HTTPS, and restart passed.", flush=True)
    except BaseException as error:
        cases.append({"id": "runtime_failure", "status": "failed", "error": type(error).__name__})
        save(LOGS / "runtime_tests.json", {"status": "failed", "total": len(cases), "passed": len(cases) - 1, "failed": 1, "cases": cases})
        raise
    finally:
        if process is not None:
            stop(process, log)


if __name__ == "__main__":
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    main()
