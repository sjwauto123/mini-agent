import os
import re
import socket
import threading
import time
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect, sync_playwright

from mini_agent.api import create_app
from mini_agent.config import load_config
from mini_agent.contracts import ExecutionContext
from mini_agent.model import HttpModelClient
from mini_agent.runtime import AgentRuntime
from mini_agent.storage import ResourceStore, Store, TodoStore, migrate_database
from mini_agent.tools import build_registry


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_REAL_MODEL_TESTS") != "1",
    reason="set RUN_REAL_MODEL_TESTS=1 to allow paid real-model requests",
)


async def test_deepseek_native_tool_loop(tmp_path: Path):
    config = load_config()
    model_config = config.models["deepseek-flash"]
    assert model_config.api_key, f"set {model_config.api_key_env} before running this test"

    db_path = tmp_path / "state.db"
    migrate_database(db_path)
    store = Store(db_path)
    await store.init()
    resources = ResourceStore(store, tmp_path / "resources")
    registry = build_registry(TodoStore(store), resources, config.tool_timeout)
    client = HttpModelClient(
        model_config.endpoint,
        model_config.model,
        model_config.api_key,
        model_config.mode,
        config.model_timeout,
        model_config.output_reserve,
    )
    runtime = AgentRuntime(
        store,
        registry,
        resources,
        lambda _: (client, model_config.mode, model_config.context_window, model_config.output_reserve),
        max_model_calls=config.max_model_calls,
        max_repairs=config.max_protocol_repairs,
        max_summary_calls=config.max_summary_calls,
        run_timeout=config.run_timeout,
        safety_margin=config.safety_margin,
        soft_context_ratio=config.soft_context_ratio,
        hard_context_ratio=config.hard_context_ratio,
        target_context_ratio=config.target_context_ratio,
    )
    session_id = await store.create_session(model_config.name)

    async def ask(message: str):
        run, _ = await runtime.submit(session_id, message)
        result = await runtime.execute(run["id"])
        assert result.status == "completed", result.error
        return result

    try:
        # 标记必须用中文：产品刻意要求"推理过程与回答一律使用中文"（LANGUAGE_HINT），
        # 用 READY 这类英文标记时模型会把它翻译成"就绪"，断言随机失败。
        direct = await ask("不要调用工具，只回复两个字：就绪")
        assert "就绪" in direct.answer

        calculation = await ask("Use the calculator tool to calculate 37 * 29, then report the result.")
        assert any(
            item["name"] == "calculator" and item["result"]["data"]["value"] == 1073
            for item in calculation.operations
        )

        followup = await ask("Use the calculator tool to divide that previous result by 7.")
        assert any(
            item["name"] == "calculator" and item["result"]["data"]["value"] == 1073 / 7
            for item in followup.operations
        )

        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        conditional = await ask(
            f"Use the weather tool for 北京 on {tomorrow}. If the mock result says rain, "
            "add a todo with text 明天带伞. Then report what happened."
        )
        assert [item["name"] for item in conditional.operations] == ["weather", "todo"]
        todos = await registry.execute("todo", {"action": "list"}, ExecutionContext("verify", session_id))
        assert any(item["text"] == "明天带伞" for item in todos.data["items"])
    finally:
        await store.close()


def test_deepseek_browser_question_answer(tmp_path: Path):
    config = load_config()
    model_config = config.models["deepseek-flash"]
    assert model_config.api_key, f"set {model_config.api_key_env} before running this test"

    config = replace(config, data_dir=tmp_path)
    migrate_database(tmp_path / "state.db")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(create_app(config), host="127.0.0.1", port=port, log_level="warning")
    )
    errors: list[BaseException] = []

    def serve() -> None:
        try:
            server.run()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    for _ in range(250):
        if server.started:
            break
        if errors or not thread.is_alive():
            detail = repr(errors[0]) if errors else "server thread exited"
            raise AssertionError(f"test server failed to start: {detail}")
        time.sleep(.02)
    else:
        server.should_exit = True
        thread.join(timeout=2)
        raise AssertionError("test server did not start within 5 seconds")

    executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    if not executable and os.name == "nt":
        candidates = [
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        ]
        executable = next((str(path) for path in candidates if path.exists()), None)

    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True, executable_path=executable)
            except PlaywrightError as exc:
                pytest.skip(f"Chromium is unavailable: {exc}")
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            try:
                page.goto(f"http://127.0.0.1:{port}")
                page.get_by_role("button", name="新建会话").first.click()
                expect(page).to_have_url(re.compile(r"\?session=[0-9a-f-]{36}$"))
                page.get_by_placeholder("请输入您想要咨询的问题...").fill("不要调用工具，只回复两个字：就绪")
                page.get_by_placeholder("请输入您想要咨询的问题...").press("Enter")
                expect(page.locator(".assistant .bubble").last).to_contain_text("就绪", timeout=90_000)
                expect(page.locator(".error-bar")).to_have_count(0)
            finally:
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
