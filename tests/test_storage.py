import hashlib
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text

from mini_agent.config import load_config
from mini_agent.errors import ERROR_ANSWERS
from mini_agent.storage import Store, migrate_database


async def test_run_stores_input_digest_preview_and_model_snapshot(tmp_path: Path):
    db_path = tmp_path / "state.db"
    migrate_database(db_path)
    store = Store(db_path)
    await store.init()
    try:
        session_id = await store.create_session("model-a")
        text = "x" * 2_000
        run, created = await store.start_run(session_id, text, "request-1")
        repeated, repeated_created = await store.start_run(session_id, text, "request-1")
        assert created and not repeated_created
        assert repeated["id"] == run["id"]
        assert run["input_hash"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert run["input_preview"] == text[:500]
        assert run["model_name"] == "model-a"
        assert "input" not in run
    finally:
        await store.close()


async def test_init_marks_abandoned_run_interrupted(tmp_path: Path):
    db_path = tmp_path / "state.db"
    migrate_database(db_path)
    first = Store(db_path)
    await first.init()
    session_id = await first.create_session("model-a")
    run, _ = await first.start_run(session_id, "hello", None)
    await first.add_message(session_id, run["id"], "user", {"content": "hello"})
    await first.close()

    second = Store(db_path)
    await second.init()
    try:
        restored = await second.get_run(run["id"])
        assert restored["status"] == "interrupted"
        assert restored["error"] == {"code": "service_restarted"}
        messages = await second.list_messages(session_id)
        assert messages[0]["content"] == "hello"
        # 收尾要写满三处：runs 状态、执行日志、说明消息。缺任何一处，这次运行看起来都像"断在半路"：
        # 没有轨迹则执行日志没有收尾步骤，没有消息则用户的话下面空无一物。
        trace = await second.list_trace(run["id"])
        assert [item["event_type"] for item in trace] == ["run.finished"]
        assert trace[0]["payload"] == {"status": "interrupted", "code": "service_restarted"}
        assert [item["role"] for item in messages] == ["user", "assistant"]
        assert messages[1]["content"] == ERROR_ANSWERS["service_restarted"]
        assert messages[1]["interrupted"] is True
    finally:
        await second.close()


async def test_init_keeps_partial_answer_without_adding_second_message(tmp_path: Path):
    """流式中途断掉时已经落了一条 incomplete 的助手消息：收尾只补轨迹，不能再加一条气泡。"""
    db_path = tmp_path / "state.db"
    migrate_database(db_path)
    first = Store(db_path)
    await first.init()
    session_id = await first.create_session("model-a")
    run, _ = await first.start_run(session_id, "hello", None)
    await first.add_message(session_id, run["id"], "user", {"content": "hello"})
    await first.add_message(session_id, run["id"], "assistant", {"content": "说到一半", "incomplete": True})
    await first.close()

    second = Store(db_path)
    await second.init()
    try:
        messages = await second.list_messages(session_id)
        assert [item["role"] for item in messages] == ["user", "assistant"]
        assert messages[1]["content"] == "说到一半"
        # 轨迹仍然要补：消息已存在不代表这次运行有收尾记录。
        assert [item["event_type"] for item in await second.list_trace(run["id"])] == ["run.finished"]
    finally:
        await second.close()


async def test_sqlite_uses_wal_and_survives_lingering_reader(tmp_path: Path):
    """回归：客户端异常断开可能残留未释放的读事务，WAL 下不得阻塞写入。

    此前使用回滚日志（journal_mode=delete）：SSE 流被取消时，SQLAlchemy 归还连接的
    过程可能被一并取消，导致连接带着未释放的读事务泄漏。残留读者会让之后所有写入提交
    持续报 "database is locked"，表现为创建会话接口长期 500。
    切到 WAL 后读不再阻塞写，可以在残留读者存在时继续写入。
    """
    db_path = tmp_path / "state.db"
    migrate_database(db_path)
    store = Store(db_path)
    reader: sqlite3.Connection | None = None
    try:
        assert await store.create_session("model-a")
        async with store.engine.connect() as connection:
            assert (await connection.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
        # 制造残留读事务，模拟异常断开后未释放的连接
        reader = sqlite3.connect(str(db_path))
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM sessions").fetchone()
        # 残留读者在场时，写入仍必须成功
        assert await store.create_session("model-a")
    finally:
        if reader is not None:
            reader.rollback()
            reader.close()
        await store.close()


def test_runtime_ratios_are_configurable(tmp_path: Path):
    config_path = tmp_path / "models.toml"
    config_path.write_text(
        """
[runtime]
soft_context_ratio = 0.60
hard_context_ratio = 0.80
target_context_ratio = 0.40

[models.test]
endpoint = "http://example.invalid"
model = "fake"
api_key_env = "UNUSED"
mode = "native"
context_window = 8192
output_reserve = 1024
""".strip(),
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config.soft_context_ratio == .60
    assert config.hard_context_ratio == .80
    assert config.target_context_ratio == .40


def test_env_file_is_loaded_without_overriding_process_environment(tmp_path: Path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("MINI_AGENT_TEST_KEY=from-file\n", encoding="utf-8")
    config_path = tmp_path / "models.toml"
    config_path.write_text(
        """
[models.test]
endpoint = "http://example.invalid"
model = "fake"
api_key_env = "MINI_AGENT_TEST_KEY"
mode = "native"
context_window = 8192
output_reserve = 1024
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.delenv("MINI_AGENT_TEST_KEY", raising=False)
    monkeypatch.setenv("MINI_AGENT_ENV_FILE", str(env_path))
    config = load_config(config_path)
    assert config.models["test"].api_key == "from-file"

    monkeypatch.setenv("MINI_AGENT_TEST_KEY", "from-process")
    config = load_config(config_path)
    assert config.models["test"].api_key == "from-process"


async def test_add_trace_returns_id_and_supports_parent_chain(tmp_path: Path):
    """add_trace 必须返回新插入事件的 id，供埋点把后续事件挂到这一事件下面。

    parent_id 字段本身没有任何数据库约束（仅存在 payload 里），因此埋点哪怕填错也
    不会落库失败——这意味着测试不仅要看写入成功，还要看"取出的 id 真的能在后续事件
    的 payload 里读到"。
    """
    db_path = tmp_path / "state.db"
    migrate_database(db_path)
    store = Store(db_path)
    await store.init()
    try:
        session_id = await store.create_session("model-a")
        run, _ = await store.start_run(session_id, "hi", None)
        # 自增 id 应当严格单调，调用方据此把后续事件挂在正确的层级下。
        root = await store.add_trace(run["id"], "run.started", {})
        model_started = await store.add_trace(run["id"], "model.started", {"attempt": 1, "iteration": 1})
        tool_started = await store.add_trace(run["id"], "tool.started", {
            "call_id": "c1",
            "name": "calculator",
            "iteration": 1,
            "parent_id": model_started
        })
        assert isinstance(root, int) and root > 0
        assert isinstance(model_started, int) and model_started > root
        assert isinstance(tool_started, int) and tool_started > model_started
        trace = await store.list_trace(run["id"])
        assert [item["event_type"] for item in trace] == ["run.started", "model.started", "tool.started"]
        assert trace[2]["payload"]["parent_id"] == model_started
    finally:
        await store.close()


async def test_add_message_rejects_roles_outside_the_whitelist(tmp_path: Path):
    """角色白名单：model.py 是把 messages 原样透传给上游的（没有任何二次过滤），

    所以"能不能落进一条 role=system 的消息"只能在这一层挡住。当前所有写入都出自 runtime、
    取值本身不会越界；这道校验保护的是将来可能出现的"导入对话""用户可写消息"之类的接口——
    一旦漏掉，role 就会被直接送进模型请求，等于让用户自己给自己提权。
    """
    db_path = tmp_path / "state.db"
    migrate_database(db_path)
    store = Store(db_path)
    await store.init()
    try:
        session_id = await store.create_session()
        for role in ("system", "developer", "USER", ""):
            with pytest.raises(ValueError):
                await store.add_message(session_id, None, role, {"content": "x"})
        # 白名单内的角色正常写入；匹配必须大小写敏感，"USER" 不是 "user" 的等价写法。
        assert await store.add_message(session_id, None, "user", {"content": "ok"}) == 1
    finally:
        await store.close()
