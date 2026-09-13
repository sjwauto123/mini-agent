import asyncio
import time
from pathlib import Path

from fastapi.testclient import TestClient

from mini_agent.api import create_app
from mini_agent.config import AppConfig, ModelConfig
from mini_agent.storage import migrate_database

from .fakes import ScriptedModel, final


def make_config(tmp_path: Path) -> AppConfig:
    model_config = ModelConfig("test", "http://example.invalid", "fake", "UNUSED", "native", 16384, 2048)
    return AppConfig(data_dir=tmp_path, models={"test": model_config})


def wait_for_terminal(client: TestClient, run_id: str) -> dict:
    for _ in range(200):
        snapshot = client.get(f"/api/runs/{run_id}").json()
        if snapshot["status"] in {"completed", "failed", "cancelled", "limit_reached", "interrupted"}:
            return snapshot
        time.sleep(.01)
    raise AssertionError("run did not finish")


def test_web_session_run_and_refresh(tmp_path: Path):
    config = make_config(tmp_path)
    fake = ScriptedModel([final("hello from agent")])
    migrate_database(tmp_path / "state.db")
    with TestClient(create_app(config, {"test": fake})) as client:
        created = client.post("/api/sessions", json={"model_name": "test"})
        assert created.status_code == 201
        session_id = created.json()["id"]
        submitted = client.post(f"/api/sessions/{session_id}/runs", json={"message": "hello", "request_key": "one"})
        assert submitted.status_code == 202
        run_id = submitted.json()["run_id"]
        snapshot = wait_for_terminal(client, run_id)
        assert snapshot["answer"] == "hello from agent"
        assert client.get(f"/api/sessions/{session_id}/runs/latest").json()["id"] == run_id
        history = client.get(f"/api/sessions/{session_id}/messages").json()
        assert [message["role"] for message in history] == ["user", "assistant"]


def test_busy_cancel_and_terminal_sse_snapshot(tmp_path: Path):
    class SlowModel:
        async def complete(self, messages, tools, *, tool_choice="auto"):
            await asyncio.sleep(30)
            return final("too late")

    config = make_config(tmp_path)
    migrate_database(tmp_path / "state.db")
    with TestClient(create_app(config, {"test": SlowModel()})) as client:
        session_id = client.post("/api/sessions", json={"model_name": "test"}).json()["id"]
        first = client.post(f"/api/sessions/{session_id}/runs", json={"message": "wait"})
        run_id = first.json()["run_id"]
        busy = client.post(f"/api/sessions/{session_id}/runs", json={"message": "second"})
        assert busy.status_code == 409 and busy.json()["detail"]["code"] == "session_busy"
        assert busy.json()["detail"]["message"] == "当前会话正在处理其他消息，请稍后再试。"
        cancelled = client.post(f"/api/runs/{run_id}/cancel")
        assert cancelled.status_code == 202 and cancelled.json()["accepted"] is True
        assert wait_for_terminal(client, run_id)["status"] == "cancelled"
        events = client.get(f"/api/runs/{run_id}/events")
        assert events.status_code == 200
        assert "event: snapshot" in events.text and '"status": "cancelled"' in events.text


def test_different_sessions_run_independently(tmp_path: Path):
    class BriefModel:
        async def complete(self, messages, tools, *, tool_choice="auto"):
            await asyncio.sleep(.1)
            return final("done")

    config = make_config(tmp_path)
    migrate_database(tmp_path / "state.db")
    with TestClient(create_app(config, {"test": BriefModel()})) as client:
        first_session = client.post("/api/sessions", json={"model_name": "test"}).json()["id"]
        second_session = client.post("/api/sessions", json={"model_name": "test"}).json()["id"]
        first = client.post(f"/api/sessions/{first_session}/runs", json={"message": "window one"})
        second = client.post(f"/api/sessions/{second_session}/runs", json={"message": "window two"})
        assert first.status_code == second.status_code == 202
        assert wait_for_terminal(client, first.json()["run_id"])["status"] == "completed"
        assert wait_for_terminal(client, second.json()["run_id"])["status"] == "completed"
        first_history = client.get(f"/api/sessions/{first_session}/messages").json()
        second_history = client.get(f"/api/sessions/{second_session}/messages").json()
        assert first_history[0]["content"] == "window one"
        assert second_history[0]["content"] == "window two"


def test_api_validates_timezone_and_resource_size(tmp_path: Path):
    config = make_config(tmp_path)
    migrate_database(tmp_path / "state.db")
    with TestClient(create_app(config, {"test": ScriptedModel([])})) as client:
        invalid = client.post("/api/sessions", json={"model_name": "test", "timezone": "Mars/Olympus"})
        assert invalid.status_code == 400 and invalid.json()["detail"]["code"] == "timezone_invalid"
        assert invalid.json()["detail"]["message"] == "时区配置无效。"
        session_id = client.post("/api/sessions", json={"model_name": "test"}).json()["id"]
        client.app.state.services.resources.MAX_BYTES = 4
        oversized = client.post(f"/api/sessions/{session_id}/resources", json={"content": "12345"})
        assert oversized.status_code == 413 and oversized.json()["detail"]["code"] == "resource_too_large"


def test_session_trace_keeps_requests_separate(tmp_path: Path):
    config = make_config(tmp_path)
    fake = ScriptedModel([final("one"), final("two")])
    migrate_database(tmp_path / "state.db")
    with TestClient(create_app(config, {"test": fake})) as client:
        session_id = client.post("/api/sessions", json={"model_name": "test"}).json()["id"]
        first = client.post(f"/api/sessions/{session_id}/runs", json={"message": "first"}).json()["run_id"]
        assert wait_for_terminal(client, first)["status"] == "completed"
        second = client.post(f"/api/sessions/{session_id}/runs", json={"message": "second"}).json()["run_id"]
        assert wait_for_terminal(client, second)["status"] == "completed"
        runs = client.get(f"/api/sessions/{session_id}/runs").json()
        trace = client.get(f"/api/sessions/{session_id}/trace").json()
        assert [item["id"] for item in runs] == [second, first]
        assert {item["run_id"] for item in trace} == {first, second}
        assert [item["event_type"] for item in trace].count("run.started") == 2
