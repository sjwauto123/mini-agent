import hashlib
from pathlib import Path

from mini_agent.config import load_config
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
        assert (await second.list_messages(session_id))[0]["content"] == "hello"
    finally:
        await second.close()


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
