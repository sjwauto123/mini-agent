import os
import re
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect, sync_playwright

from mini_agent.api import create_app
from mini_agent.config import AppConfig, ModelConfig
from mini_agent.storage import migrate_database

from .fakes import final


class EchoModel:
    async def complete(self, messages, tools, *, tool_choice="auto"):
        latest = next(message["content"] for message in reversed(messages) if message["role"] == "user")
        if latest == "trigger model failure":
            raise httpx.ConnectError("simulated connection failure")
        return final(f"Echo: {latest}")


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.fixture
def live_server(tmp_path: Path):
    port = _free_port()
    model = ModelConfig("test", "http://example.invalid", "fake", "UNUSED", "native", 16384, 2048)
    config = AppConfig(data_dir=tmp_path, models={"test": model})
    migrate_database(tmp_path / "state.db")
    server = uvicorn.Server(uvicorn.Config(create_app(config, {"test": EchoModel()}), host="127.0.0.1", port=port, log_level="warning"))
    errors: list[BaseException] = []

    def serve() -> None:
        try:
            server.run()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
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
    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_live_server_health(live_server: str):
    with httpx.Client(trust_env=False) as client:
        assert client.get(f"{live_server}/api/health", timeout=2).json() == {"status": "ok"}


def test_two_browser_pages_keep_sessions_isolated_and_restore(live_server: str, tmp_path: Path):
    executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    if not executable and os.name == "nt":
        candidates = [
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        ]
        executable = next((str(path) for path in candidates if path.exists()), None)

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True, executable_path=executable)
        except PlaywrightError as exc:
            pytest.skip(f"Chromium is unavailable: {exc}")
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        try:
            first = context.new_page()
            first.goto(live_server)
            first.locator(".new-session").click()
            expect(first).to_have_url(re.compile(r"\?session=[0-9a-f-]{36}$"))
            first_session = first.url.split("session=", 1)[1]
            expect(first.locator(".composer .composer-model")).to_be_visible()
            expect(first.locator(".composer .composer-model.fixed")).to_be_visible()
            assert first.get_by_title("添加资料").count() == 0
            first.get_by_placeholder("请输入您想要咨询的问题...").fill("window one")
            first.get_by_placeholder("请输入您想要咨询的问题...").press("Enter")
            expect(first.locator(".assistant .bubble").first).to_contain_text("Echo: window one")
            first.get_by_role("button", name="执行日志").click()
            expect(first.locator(".trace-page")).to_contain_text("执行日志")
            first.locator(".trace-group-toggle").first.click()
            expect(first.locator(".trace-group-toggle").first).to_have_attribute("aria-expanded", "true")
            expect(first.locator(".trace-page")).to_contain_text("开始运行")
            expect(first.locator(".trace-page")).to_contain_text("请求模型")
            expect(first.locator(".trace-page")).to_contain_text("运行完成")
            first.screenshot(path=str(tmp_path / "mini-agent-trace-desktop.png"), full_page=True)
            first.get_by_role("button", name="问答").click()
            first.get_by_placeholder("请输入您想要咨询的问题...").fill("**markdown**")
            first.get_by_placeholder("请输入您想要咨询的问题...").press("Enter")
            expect(first.locator(".assistant .bubble strong")).to_contain_text("markdown")
            first.get_by_role("button", name="执行日志").click()
            expect(first.locator(".trace-page .trace-group")).to_have_count(2)
            first.locator(".trace-group-toggle").nth(1).click()
            expect(first.locator(".trace-page .trace-group").nth(1)).to_contain_text("请求模型")

            second = context.new_page()
            second.goto(live_server)
            expect(second).to_have_url(re.compile(r"\?session=[0-9a-f-]{36}$"))
            second.locator(".new-session").click()
            second.wait_for_function(
                "previous => new URL(location.href).searchParams.get('session') !== previous",
                arg=first_session,
            )
            second_session = second.url.split("session=", 1)[1]
            assert second_session != first_session
            second.get_by_placeholder("请输入您想要咨询的问题...").fill("window two")
            second.get_by_placeholder("请输入您想要咨询的问题...").press("Enter")
            expect(second.locator(".assistant .bubble")).to_contain_text("Echo: window two")

            expect(first).to_have_url(re.compile(re.escape(first_session)))
            first.get_by_role("button", name="问答").click()
            expect(first.locator(".user .bubble").first).to_contain_text("window one")

            second.get_by_placeholder("请输入您想要咨询的问题...").fill("trigger model failure")
            second.get_by_placeholder("请输入您想要咨询的问题...").press("Enter")
            expect(second.locator(".error-bar")).to_contain_text("模型服务暂时不可用", timeout=10_000)

            first.reload()
            expect(first).to_have_url(re.compile(re.escape(first_session)))
            expect(first.locator(".assistant .bubble").first).to_contain_text("Echo: window one")

            first.get_by_placeholder("请输入您想要咨询的问题...").fill("| 工具 | 作用 |\n|---|---|\n| calculator | 计算 |\n| todo | 待办 |")
            first.get_by_placeholder("请输入您想要咨询的问题...").press("Enter")
            expect(first.locator(".assistant .bubble table")).to_have_count(1)
            expect(first.locator(".assistant .bubble table")).to_contain_text("calculator")
            expect(first.locator(".assistant .bubble").last).not_to_contain_text("---|")

            first.set_viewport_size({"width": 390, "height": 844})
            first.wait_for_timeout(300)
            assert first.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            first.screenshot(path=str(tmp_path / "mini-agent-mobile.png"), full_page=True)
        finally:
            context.close()
            browser.close()
