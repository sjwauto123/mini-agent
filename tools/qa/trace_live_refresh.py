"""验证「运行期间执行日志随运行推进刷新」（真实模型 + 真实浏览器）。

背景：SSE 回调是「发消息那一刻」的闭包，此前它读的是当时捕获的 view；而发消息只能在问答页，
于是运行期间切到执行日志后，轨迹只在「切入那一刻」和「终态」各刷一次，中间过程不再刷新。
这条链路只能用真实运行验证：用一个需要多轮工具调用的提问把运行拖长，观察切换后步骤数是否增长。

会真实调用模型并创建一个临时会话，结束后删除该会话。
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({}))
TERMINAL = {"completed", "failed", "cancelled", "limit_reached", "interrupted"}
QUESTION = "先用计算器算一下 47*89，再查一下拉萨今天的天气，然后用一句话总结我该带什么。"

RESULTS = []


def check(name, passed, detail=""):
    RESULTS.append((name, passed))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}" + (f"  | {detail}" if detail else ""))


def call(method, path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with OPEN.open(req) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw else None


def latest_status(sid):
    run = call("GET", f"/api/sessions/{sid}/runs/latest")
    return run["status"] if run else None


def find_chrome():
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    return next((p for p in candidates if Path(p).exists()), None)


model_name = call("GET", "/api/models")[0]["name"]
sid = call("POST", "/api/sessions", {"model_name": model_name, "timezone": "Asia/Shanghai"})["id"]
print(f"模型 = {model_name}  临时会话 = {sid}\n")

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, executable_path=find_chrome())
    page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
    try:
        page.goto(f"{BASE}/?session={sid}")
        box = page.get_by_placeholder("请输入您想要咨询的问题...")
        box.wait_for(timeout=20000)
        box.fill(QUESTION)
        page.wait_for_timeout(150)
        box.press("Enter")
        print("已提交提问，等待运行进入 running…")

        # 等运行真正开始（此刻页面还在问答视图 —— 这正是触发旧 bug 的前提条件）。
        deadline = time.time() + 30
        while time.time() < deadline and latest_status(sid) in (None, "queued"):
            page.wait_for_timeout(200)
        started = latest_status(sid)
        check("运行已开始（提问已被受理）", started not in (None, "queued"), f"状态={started}")

        # 立刻切到执行日志；此后每一步的步骤数增长都必须来自 SSE 快照触发的刷新。
        page.get_by_role("button", name="执行日志").click()
        page.wait_for_selector(".trace-page", timeout=15000)
        page.wait_for_timeout(300)
        first = page.locator(".trace-step").count()
        print(f"切入执行日志时步骤数 = {first}")

        grew_mid_run = 0
        samples = []
        deadline = time.time() + 60
        while time.time() < deadline:
            status = latest_status(sid)
            count = page.locator(".trace-step").count()
            samples.append((status, count))
            if status in TERMINAL:
                break
            if count > first and grew_mid_run == 0:
                grew_mid_run = count
                print(f"运行中观察到步骤数增长：{first} → {count}（状态={status}）")
            page.wait_for_timeout(250)

        final_status = latest_status(sid)
        check("运行到达终态", final_status in TERMINAL, f"状态={final_status}")
        check("运行期间执行日志随运行推进刷新（旧闭包下不会增长）",
              grew_mid_run > first,
              f"运行中最大步骤数={grew_mid_run}，切入时={first}，采样={samples[:8]}")

        page.wait_for_timeout(800)
        final = page.locator(".trace-step").count()
        check("终态后步骤数不少于运行中所见", final >= max(first, grew_mid_run), f"终态={final}")
        check("执行日志无错误提示条", page.locator(".error-bar").count() == 0,
              f"错误条={page.locator('.error-bar').count()}")
    finally:
        browser.close()

print("\n=== 清理 ===")
call("DELETE", f"/api/sessions/{sid}")
print(f"已删除临时会话 {sid}")

print("\n=== 汇总 ===")
failed = [n for n, ok in RESULTS if not ok]
print(f"通过 {len(RESULTS) - len(failed)}/{len(RESULTS)}")
if failed:
    print("失败项:", failed)
    sys.exit(1)
print("全部通过")
