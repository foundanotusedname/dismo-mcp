from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from dismo_mcp.artifacts import ArtifactStore
from dismo_mcp.auth import StaticTokenVerifier, is_loopback_host
from dismo_mcp.__main__ import main
from dismo_mcp.config import Settings
from dismo_mcp.errors import ArtifactStoreError, ConfigurationError, PathPolicyError, RBridgeError
from dismo_mcp.provenance import assert_file_manifest_matches, build_predictor_manifest
from dismo_mcp.r_bridge import RBridge
from dismo_mcp.server import create_http_server, create_server


def test_path_policy_and_artifact_store(tmp_path: Path) -> None:
    input_file = tmp_path / "occurrences.csv"
    input_file.write_text("lon,lat\n1,2\n", encoding="utf-8")
    settings = Settings(tmp_path, (tmp_path,), None, 30)

    assert settings.resolve_input("occurrences.csv") == input_file.resolve()
    with pytest.raises(PathPolicyError):
        settings.resolve_input(str(Path(__file__).resolve()))

    store = ArtifactStore(tmp_path)
    run = store.create("test")
    model = run.directory / "model.rds"
    model.write_bytes(b"model")
    result = {
        "artifacts": [
            {"role": "model", "path": str(model), "name": model.name}
        ]
    }
    store.finish(run, result)
    assert store.artifact_path(run.run_id, "model") == model.resolve()
    assert store.read(run.run_id)["status"] == "completed"


def test_server_surface(tmp_path: Path) -> None:
    server = create_server(Settings(tmp_path, (tmp_path,), None, 30))
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}

    assert len(names) == 17
    assert {
        "dismo_system_info",
        "fit_sdm",
        "predict_sdm",
        "evaluate_sdm",
        "calculate_mess",
        "calculate_niche_overlap",
    } <= names

    templates = asyncio.run(server.list_resource_templates())
    assert {str(template.uri_template) for template in templates} == {"dismo://runs/{run_id}"}


def _sse_json(response_text: str) -> dict:
    data_line = next(line for line in response_text.splitlines() if line.startswith("data: "))
    return json.loads(data_line.removeprefix("data: "))


def test_http_requires_token_and_enforces_read_scope(tmp_path: Path) -> None:
    read_token = "read-token-" + "x" * 32
    verifier = StaticTokenVerifier(
        read_token=read_token,
        base_url="http://127.0.0.1:8000",
    )
    full_token = "full-token-" + "y" * 32
    full_verifier = StaticTokenVerifier(full_token=full_token, base_url="http://127.0.0.1:8000")
    full_access = asyncio.run(full_verifier.verify_token(full_token))
    assert full_access is not None
    assert set(full_access.scopes) == {"dismo:read", "dismo:write"}
    server = create_server(Settings(tmp_path, (tmp_path,), None, 30), auth=verifier)
    app = server.http_app()
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1"},
        },
    }

    with TestClient(app) as client:
        assert client.post("/mcp", json=initialize, headers=headers).status_code == 401

        headers["Authorization"] = f"Bearer {read_token}"
        initialized = client.post("/mcp", json=initialize, headers=headers)
        assert initialized.status_code == 200
        headers["mcp-session-id"] = initialized.headers["mcp-session-id"]
        client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=headers,
        )
        denied = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "partition_occurrences", "arguments": {}},
            },
            headers=headers,
        )
        assert _sse_json(denied.text)["result"]["isError"] is True
        assert "Unknown tool" in denied.text


def test_non_loopback_http_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("::1")
    assert not is_loopback_host("0.0.0.0")
    for name in (
        "DISMO_MCP_BEARER_TOKEN",
        "DISMO_MCP_READ_TOKEN",
        "DISMO_MCP_WRITE_TOKEN",
        "DISMO_MCP_PUBLIC_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["dismo-mcp", "--transport", "http", "--host", "0.0.0.0"],
    )
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 2


def test_missing_r_marks_run_failed(tmp_path: Path) -> None:
    settings = Settings(tmp_path, (tmp_path,), None, 30)
    store = ArtifactStore(tmp_path)
    bridge = RBridge(settings, store)

    with pytest.raises(ConfigurationError):
        bridge.execute("system_info", {})

    runs = store.list()
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"


def test_predictor_manifest_detects_content_changes(tmp_path: Path) -> None:
    predictor = tmp_path / "predictor.tif"
    predictor.write_bytes(b"first")
    expected = build_predictor_manifest([predictor])
    predictor.write_bytes(b"changed")
    current = build_predictor_manifest([predictor])
    with pytest.raises(PathPolicyError, match="Predictor files differ"):
        assert_file_manifest_matches(expected, current)


def test_raster_sidecar_obeys_input_limit(tmp_path: Path) -> None:
    raster = tmp_path / "predictor.grd"
    raster.write_bytes(b"grid")
    sidecar = tmp_path / "predictor.gri"
    sidecar.write_bytes(b"too-large")
    settings = Settings(tmp_path, (tmp_path,), None, 30, max_input_bytes=8)
    with pytest.raises(PathPolicyError, match="Raster sidecar exceeds"):
        settings.resolve_raster_input("predictor.grd", suffixes=(".grd",))


def test_run_storage_prunes_finished_but_rejects_active(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path, max_runs=1, max_bytes=10_000)
    active = store.create("active")
    with pytest.raises(ArtifactStoreError, match="Run storage limit"):
        store.create("blocked")
    store.fail(active, "done")
    replacement = store.create("replacement")
    assert replacement.run_id != active.run_id
    assert not active.directory.exists()


def test_late_r_response_cannot_resurrect_cancelled_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ready = threading.Event()
    release = threading.Event()

    class LateResponseProcess:
        returncode = 0

        def __init__(self, command: list[str], **_: object) -> None:
            self.command = command

        def communicate(self, timeout: int | None = None) -> tuple[str, str]:
            ready.set()
            assert release.wait(2)
            Path(self.command[-1]).write_text(
                json.dumps({"ok": True, "result": {}}), encoding="utf-8"
            )
            return "", ""

        def poll(self) -> int | None:
            return None if not release.is_set() else self.returncode

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

    monkeypatch.setattr("dismo_mcp.r_bridge.subprocess.Popen", LateResponseProcess)
    settings = Settings(tmp_path, (tmp_path,), Path(sys.executable), 5)
    store = ArtifactStore(tmp_path)
    bridge = RBridge(settings, store)
    result: list[BaseException] = []

    def invoke() -> None:
        try:
            bridge.execute("system_info", {})
        except BaseException as exc:  # noqa: BLE001 - assertion target
            result.append(exc)

    thread = threading.Thread(target=invoke)
    thread.start()
    assert ready.wait(2)
    time.sleep(0.01)
    run_id = store.list()[0]["run_id"]
    assert bridge.cancel(str(run_id))
    release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert result and isinstance(result[0], RBridgeError)
    assert store.read(str(run_id))["status"] == "failed"


def test_http_factory_requires_auth_and_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "DISMO_MCP_BEARER_TOKEN",
        "DISMO_MCP_READ_TOKEN",
        "DISMO_MCP_WRITE_TOKEN",
        "DISMO_MCP_PUBLIC_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConfigurationError, match="requires"):
        create_http_server(host="127.0.0.1", port=8000)
    monkeypatch.setenv("DISMO_MCP_BEARER_TOKEN", "x" * 32)
    with pytest.raises(ConfigurationError, match="loopback"):
        create_http_server(host="0.0.0.0", port=8000)
    unauthenticated = create_server(Settings(Path.cwd(), (Path.cwd(),), None, 30))
    with pytest.raises(ConfigurationError, match="Unauthenticated"):
        unauthenticated.http_app()


def test_cancel_queued_run_marks_failed(tmp_path: Path) -> None:
    settings = Settings(tmp_path, (tmp_path,), None, 30)
    store = ArtifactStore(tmp_path)
    bridge = RBridge(settings, store)
    run = store.create("long_operation")
    assert bridge.cancel(run.run_id)
    assert store.read(run.run_id)["status"] == "failed"
    assert not bridge.cancel(run.run_id)


def test_r_process_crash_marks_failed(tmp_path: Path) -> None:
    settings = Settings(tmp_path, (tmp_path,), Path(sys.executable), 30)
    store = ArtifactStore(tmp_path)
    bridge = RBridge(settings, store)
    with pytest.raises(RBridgeError):
        bridge.execute("system_info", {})
    assert store.list()[0]["status"] == "failed"


def test_r_timeout_marks_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class TimeoutProcess:
        returncode = -9

        def communicate(self, timeout: int | None = None) -> tuple[str, str]:
            if timeout is not None:
                raise subprocess.TimeoutExpired(["Rscript"], timeout)
            return "", ""

        def kill(self) -> None:
            return None

        def poll(self) -> int | None:
            return None

        def terminate(self) -> None:
            return None

    monkeypatch.setattr(
        "dismo_mcp.r_bridge.subprocess.Popen",
        lambda *args, **kwargs: TimeoutProcess(),
    )
    settings = Settings(tmp_path, (tmp_path,), Path(sys.executable), 1)
    store = ArtifactStore(tmp_path)
    bridge = RBridge(settings, store)
    with pytest.raises(RBridgeError, match="timed out"):
        bridge.execute("system_info", {})
    assert store.list()[0]["status"] == "failed"
