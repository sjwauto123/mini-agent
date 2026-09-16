"""为 README 生成功能演示截图（真实服务 + 真实模型 + 系统 Chrome）。

与 qa_browser_render.py 的区别：那个脚本的产出是**断言证据**（顺带存两张图），
这个脚本的产出是**给人看的演示图**，所以取景方式不同：

1. 页面 `overflow: hidden`、滚动发生在右侧工作区内部，因此 `full_page=True`
   截到的就是视口本身 —— 想多装内容**只能把视口调高**，不能靠全页截图。
2. 思考面板在回答完成后会**自动折叠**（`ThinkingPanel` 的 open 跟随 busy），
   所以出图前要手动点开，否则读者看不到这个功能。
3. 第三张用 `clip` 取「工具调用芯片 + 回答」的近景，避免三张图长得一模一样。

用法（先在仓库根目录起服务，再在仓库根目录执行）：
    .venv/Scripts/python.exe tools/qa/make_readme_shots.py
输出：docs/images/01-chat.png / 02-trace.png / 03-tool-call.png
"""
import json
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({}))
TERMINAL = {"completed", "failed", "cancelled", "limit_reached", "interrupted"}
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "images"
OUT.mkdir(parents=True, exist_ok=True)

WIDE = {"width": 1240, "height": 940}
TALL = {"width": 1240, "height": 1700}


def call(method, path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with OPEN.open(req) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw else None


def find_chrome():
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    return next((p for p in candidates if Path(p).exists()), None)


def wait_runs(sid, expected, timeout=300):
    """等**服务端**确认已有 expected 条运行且全部到终态。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        runs = call("GET", f"/api/sessions/{sid}/runs")
        if len(runs) >= expected and all(r["status"] in TERMINAL for r in runs):
            return runs
        time.sleep(1)
    raise AssertionError(f"等待 {expected} 条终态运行超时")


def expand_thinking(page):
    """点开所有已折叠的思考面板（回答结束后它们会自动折叠）。"""
    panels = page.locator("details.thinking")
    for i in range(panels.count()):
        panel = panels.nth(i)
        if not panel.evaluate("e => e.open"):
            panel.locator("summary").click()
    page.wait_for_timeout(300)


model_name = call("GET", "/api/models")[0]["name"]
sid = call("POST", "/api/sessions", {"model_name": model_name, "timezone": "Asia/Shanghai"})["id"]
print(f"模型 = {model_name}   会话 = {sid}\n")
print(f"输出目录 = {OUT}\n")

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, executable_path=find_chrome())
    context = browser.new_context(viewport=WIDE, device_scale_factor=2)
    page = context.new_page()
    try:
        page.goto(f"{BASE}/?session={sid}")
        box = page.get_by_placeholder("请输入您想要咨询的问题...")
        box.wait_for(timeout=20000)

        def ask(text, expected_runs):
            page.wait_for_selector(".composer button.send", timeout=240000)
            box.fill(text)
            page.wait_for_timeout(150)
            box.press("Enter")
            wait_runs(sid, expected_runs)
            page.wait_for_selector(".composer button.send", timeout=240000)
            page.wait_for_timeout(700)

        ask("用计算器算一下 47*89，然后告诉我结果。", 1)
        ask("一句话解释什么是缓存命中。", 2)
        expand_thinking(page)
        page.wait_for_timeout(500)

        page.screenshot(path=str(OUT / "01-chat.png"))
        print("已出图 01-chat.png（问答视图：侧栏 + 用户气泡 + 工具芯片 + 回答 + 思考过程）")

        summary = page.evaluate("""() => ({
          assistant: [...document.querySelectorAll('.assistant .bubble')].length,
          chips: [...document.querySelectorAll('.tool-calls')].map(e => e.textContent.trim()),
          thinking: [...document.querySelectorAll('details.thinking')].length,
          errors: [...document.querySelectorAll('.error-bar')].length,
        })""")
        print(f"    自查：助手气泡 {summary['assistant']} / 工具芯片 {summary['chips']} "
              f"/ 思考面板 {summary['thinking']} / 错误条 {summary['errors']}")

        # —— 第三张：工具调用近景（换个工具，顺带展示 mock 标注）——
        # 一张图要同时说清「模型想了什么 → 调了什么工具 → 回了什么」，所以不能只截最后一条
        # 助手消息：工具芯片属于**上一条**助手消息，单独截最后一条就把芯片漏掉了。
        ask("北京现在的模拟天气怎么样？", 3)
        expand_thinking(page)
        last = page.locator(".assistant").last
        last.scroll_into_view_if_needed()
        page.wait_for_timeout(400)
        rects = [page.locator(".assistant").nth(-2).bounding_box(), last.bounding_box()]
        rects = [r for r in rects if r]
        if rects:
            pad = 18
            x0 = max(0, min(r["x"] for r in rects) - pad)
            y0 = max(0, min(r["y"] for r in rects) - pad)
            x1 = max(r["x"] + r["width"] for r in rects) + pad
            y1 = max(r["y"] + r["height"] for r in rects) + pad
            clip = {
                "x": x0, "y": y0,
                "width": min(WIDE["width"] - x0, x1 - x0),
                "height": min(WIDE["height"] - y0, y1 - y0),
            }
            page.screenshot(path=str(OUT / "03-tool-call.png"), clip=clip)
            print(f"已出图 03-tool-call.png（近景 {int(clip['width'])}x{int(clip['height'])} 逻辑像素）")

        # —— 第二张：执行日志（加高视口，尽量把整棵链路装进来）——
        page.set_viewport_size(TALL)
        page.get_by_role("button", name="执行日志").click()
        page.wait_for_selector(".trace-page", timeout=20000)
        page.wait_for_timeout(800)
        page.screenshot(path=str(OUT / "02-trace.png"))
        steps = page.locator(".trace-step").count()
        print(f"已出图 02-trace.png（执行日志：{steps} 个步骤）")
    finally:
        context.close()
        browser.close()

call("DELETE", f"/api/sessions/{sid}")
print(f"已删除临时会话 {sid}（截图保留）")
for name in ["01-chat.png", "02-trace.png", "03-tool-call.png"]:
    path = OUT / name
    print(f"  {name}  {path.stat().st_size // 1024} KB" if path.exists() else f"  {name}  缺失")
