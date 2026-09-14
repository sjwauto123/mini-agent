import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest
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


def test_sse_message_events_are_anchored_and_never_empty(tmp_path: Path):
    """回归：运行刚开始时的空载荷会被前端当成流式目标，把上一条回答覆盖掉。

    因此 message 事件必须带 seq（前端据此精确定位），且不允许推送空内容。
    """

    class DelayedModel:
        async def complete(self, messages, tools, *, tool_choice="auto"):
            await asyncio.sleep(.6)
            return final("上海是中国最大的经济中心城市。", reasoning="可以直接回答。")

    config = make_config(tmp_path)
    migrate_database(tmp_path / "state.db")
    with TestClient(create_app(config, {"test": DelayedModel()})) as client:
        session_id = client.post("/api/sessions", json={"model_name": "test"}).json()["id"]
        first = client.post(f"/api/sessions/{session_id}/runs", json={"message": "介绍北京"})
        assert wait_for_terminal(client, first.json()["run_id"])["status"] == "completed"

        second = client.post(f"/api/sessions/{session_id}/runs", json={"message": "再介绍上海"})
        events = client.get(f"/api/runs/{second.json()['run_id']}/events")
        assert events.status_code == 200

        payloads = []
        for line in events.text.splitlines():
            if not line.startswith("data: "):
                continue
            try:
                data = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and "content" in data:
                payloads.append(data)

        assert payloads, "运行过程中应当推送过 message 事件"
        for payload in payloads:
            assert isinstance(payload.get("seq"), int), f"message 事件必须带 seq：{payload}"
            assert payload.get("content") or payload.get("thinking"), f"不允许推送空载荷：{payload}"

        history = client.get(f"/api/sessions/{session_id}/messages").json()
        previous_seq = next(item["seq"] for item in history if item["role"] == "assistant")
        assert all(payload["seq"] > previous_seq for payload in payloads), "推送目标必须是本轮新回答"


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


def test_delete_while_running_keeps_resources(tmp_path: Path):
    """忙碌时删会话必须整体拒绝。

    此前是先删资源文件、再发现会话在跑并回 409 —— 被拒绝的请求产生了不可逆副作用。
    """

    class SlowModel:
        async def complete(self, messages, tools, *, tool_choice="auto"):
            await asyncio.sleep(30)
            return final("too late")

    config = make_config(tmp_path)
    migrate_database(tmp_path / "state.db")
    with TestClient(create_app(config, {"test": SlowModel()})) as client:
        session_id = client.post("/api/sessions", json={"model_name": "test"}).json()["id"]
        client.post(f"/api/sessions/{session_id}/resources", json={"content": "重要资料"})
        run_id = client.post(f"/api/sessions/{session_id}/runs", json={"message": "wait"}).json()["run_id"]
        rejected = client.delete(f"/api/sessions/{session_id}")
        assert rejected.status_code == 409 and rejected.json()["detail"]["code"] == "session_busy"
        # 会话、资源数据行与磁盘文件都必须原样保留。
        assert len(list((tmp_path / "resources").glob("*.txt"))) == 1
        assert client.get(f"/api/sessions/{session_id}/messages").status_code == 200
        assert client.post(f"/api/runs/{run_id}/cancel").json()["accepted"] is True
        assert wait_for_terminal(client, run_id)["status"] == "cancelled"


def test_delete_when_idle_purges_session_and_files(tmp_path: Path):
    """空闲时删除：数据行、资源索引与磁盘文件一起清干净。"""
    config = make_config(tmp_path)
    migrate_database(tmp_path / "state.db")
    with TestClient(create_app(config, {"test": ScriptedModel([final("ok")])})) as client:
        session_id = client.post("/api/sessions", json={"model_name": "test"}).json()["id"]
        client.post(f"/api/sessions/{session_id}/resources", json={"content": "临时资料"})
        assert client.delete(f"/api/sessions/{session_id}").json() == {"id": session_id, "deleted": True}
        assert list((tmp_path / "resources").glob("*.txt")) == []
        assert client.get(f"/api/sessions/{session_id}/messages").status_code == 404
        # 资源索引必须与外键同事务清掉，否则删会话会被 FOREIGN KEY 约束挡下。
        with sqlite3.connect(tmp_path / "state.db") as connection:
            remaining = connection.execute("select count(*) from resources where session_id = ?", (session_id,)).fetchone()[0]
        assert remaining == 0


def test_delete_survives_resource_cleanup_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """磁盘清理失败不能把一次已经完成的删除报成失败。

    数据行与资源索引在同一个事务里删除并已提交，此时文件删不掉只是残留一个文件；
    若让异常穿透出去，客户端会收到 500 并以为删除失败而重试，而会话其实早已不存在。
    这里用 SystemExit 模拟：平台删除防护/杀软这类故障抛出的并不是 ``Exception``，
    只按 ``OSError``/``Exception`` 兜底会漏掉。
    """

    def refuse(*args, **kwargs):
        raise SystemExit(1)

    config = make_config(tmp_path)
    migrate_database(tmp_path / "state.db")
    with TestClient(create_app(config, {"test": ScriptedModel([final("ok")])})) as client:
        session_id = client.post("/api/sessions", json={"model_name": "test"}).json()["id"]
        client.post(f"/api/sessions/{session_id}/resources", json={"content": "删不掉的资料"})
        monkeypatch.setattr(Path, "unlink", refuse)
        deleted = client.delete(f"/api/sessions/{session_id}")
        assert deleted.status_code == 200 and deleted.json() == {"id": session_id, "deleted": True}
        # 删除结果以数据库为准：会话与资源索引都已清理，文件残留不影响事实。
        assert client.get(f"/api/sessions/{session_id}/messages").status_code == 404
        with sqlite3.connect(tmp_path / "state.db") as connection:
            remaining = connection.execute("select count(*) from resources where session_id = ?", (session_id,)).fetchone()[0]
        assert remaining == 0


def test_missing_api_key_is_reported_as_configuration_error(tmp_path: Path):
    """缺少密钥属于服务端配置问题：不能报成"会话忙碌"，错误码里也不该带着环境变量名。"""
    config = make_config(tmp_path)
    migrate_database(tmp_path / "state.db")
    # 不注入 model_overrides，且配置引用的 UNUSED 在本机没有对应环境变量。
    with TestClient(create_app(config)) as client:
        session_id = client.post("/api/sessions", json={"model_name": "test"}).json()["id"]
        rejected = client.post(f"/api/sessions/{session_id}/runs", json={"message": "hi"})
        assert rejected.status_code == 503
        assert rejected.json()["detail"]["code"] == "model_api_key_missing"
        # 落库的运行记录也要写真实原因，而不是统一的 input_prepare_failed。
        assert client.get(f"/api/sessions/{session_id}/runs").json()[0]["error"]["code"] == "model_api_key_missing"


def test_session_with_unconfigured_model_is_not_reported_as_missing(tmp_path: Path):
    """会话引用了已下线的模型时，必须报"模型未配置"，而不是 404"会话不存在"。"""
    config = make_config(tmp_path)
    db_path = tmp_path / "state.db"
    migrate_database(db_path)
    with TestClient(create_app(config, {"test": ScriptedModel([])})) as client:
        # 直接写库造出"会话存在但模型不在配置里"的状态：接口层不会允许建出这种会话。
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                "insert into sessions (id, model_name, timezone, title, created_at) values (?, ?, ?, ?, ?)",
                ("ghost-session", "ghost", "Asia/Shanghai", None, "2026-01-01T00:00:00+00:00"),
            )
        rejected = client.post("/api/sessions/ghost-session/runs", json={"message": "hi"})
        assert rejected.status_code == 503
        assert rejected.json()["detail"]["code"] == "model_not_configured"


def test_run_records_hide_internal_fields(tmp_path: Path):
    """input_hash / request_key 是服务端去重用的内部字段，不应出现在接口返回里。"""
    config = make_config(tmp_path)
    migrate_database(tmp_path / "state.db")
    with TestClient(create_app(config, {"test": ScriptedModel([final("ok")])})) as client:
        session_id = client.post("/api/sessions", json={"model_name": "test"}).json()["id"]
        run_id = client.post(f"/api/sessions/{session_id}/runs", json={"message": "hi", "request_key": "k1"}).json()["run_id"]
        assert wait_for_terminal(client, run_id)["status"] == "completed"
        records = [
            client.get(f"/api/runs/{run_id}").json(),
            client.get(f"/api/sessions/{session_id}/runs/latest").json(),
            client.get(f"/api/sessions/{session_id}/runs").json()[0],
        ]
        for record in records:
            assert record["id"] == run_id
            assert "input_hash" not in record and "request_key" not in record
