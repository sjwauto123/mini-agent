"""前端渲染格式实测（真实浏览器 + 真实模型 + 8000 真实服务）。

只看两件事：**回答气泡有没有重复/错乱**，以及**执行日志的链路树渲染是否正常**
（有层级缩进、事件都有中文文案、没有落到兜底分支）。
"""
import json
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({}))
TERMINAL = {"completed", "failed", "cancelled", "limit_reached", "interrupted"}
OUT = Path(__file__).resolve().parent / "evidence"
OUT.mkdir(parents=True, exist_ok=True)

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


def find_chrome():
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    return next((p for p in candidates if Path(p).exists()), None)


FRAME = """() => {
  const txt = e => e ? e.textContent.trim() : null;
  const steps = [...document.querySelectorAll('.trace-step')];
  return {
    assistantBubbles: [...document.querySelectorAll('.assistant .bubble')].map(txt),
    userBubbles: [...document.querySelectorAll('.user .bubble')].map(txt),
    toolChips: [...document.querySelectorAll('.tool-calls')].map(txt),
    thinkingPanels: [...document.querySelectorAll('details.thinking')].map(txt),
    errors: [...document.querySelectorAll('.error-bar')].map(txt),
    turns: document.querySelectorAll('.trace-turn').length,
    flows: document.querySelectorAll('.trace-flow').length,
    stepCount: steps.length,
    depths: [...new Set(steps.map(e => [...e.classList].find(c => c.startsWith('depth-'))))].sort(),
    titles: steps.map(e => txt(e.querySelector('.trace-title strong'))),
    metas: steps.map(e => txt(e.querySelector('.trace-meta'))),
    fallback: steps.filter(e => e.textContent.includes('记录了一条运行事件')).length,
    rawNames: steps.map(e => txt(e.querySelector('.trace-title strong')))
                     .filter(t => /^[a-z]+\\.[a-z_]+$/.test(t || '')),
    pageText: document.querySelector('.trace-page') ? document.querySelector('.trace-page').textContent : ''
  };
}"""

def wait_runs(sid, expected, timeout=240):
    """等服务端确认已有 expected 条运行且全部到终态（DOM 文本可见 ≠ 运行结束）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        runs = call("GET", f"/api/sessions/{sid}/runs")
        if len(runs) >= expected and all(r["status"] in TERMINAL for r in runs):
            return runs
        time.sleep(1)
    raise AssertionError(f"等待 {expected} 条终态运行超时")


model_name = call("GET", "/api/models")[0]["name"]
sid = call("POST", "/api/sessions", {"model_name": model_name, "timezone": "Asia/Shanghai"})["id"]
print(f"模型 = {model_name}  会话 = {sid}\n")

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, executable_path=find_chrome())
    page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
    try:
        page.goto(f"{BASE}/?session={sid}")
        box = page.get_by_placeholder("请输入您想要咨询的问题...")
        box.wait_for(timeout=20000)

        # 关键：回答是**流式**的，"看到数字" ≠ "运行已结束"。运行期间 composer 上是"停止"
        # 按钮，此时 Enter 会被 send() 直接 return（草稿保留、消息不发出）。
        # 所以固定流程：等 `.composer button.send`（空闲）→ 填 → 回车 → 等 API 确认终态。
        def send_question(text, expected_runs):
            page.wait_for_selector(".composer button.send", timeout=200000)
            box.fill(text)
            page.wait_for_timeout(150)
            box.press("Enter")
            wait_runs(sid, expected_runs)
            page.wait_for_selector(".composer button.send", timeout=200000)

        # 第 1 轮：工具轮
        send_question("用计算器算一下 47*89，然后告诉我结果。", 1)
        page.wait_for_function(
            "() => document.body.innerText.replace(/,/g,'').includes('4183')", timeout=60000)
        print("    第 1 轮（工具轮）已完成")

        # 第 2 轮：纯对话，验证多轮渲染
        send_question("一句话解释什么是缓存命中。", 2)
        page.wait_for_function(
            "() => document.querySelectorAll('.assistant .bubble').length >= 2", timeout=60000)
        page.wait_for_timeout(800)
        print("    第 2 轮（纯对话）已完成")
        check("1.0 两轮提问都被服务端受理", len(call("GET", f"/api/sessions/{sid}/runs")) == 2, "")

        page.screenshot(path=str(OUT / "1-chat.png"), full_page=True)
        chat = page.evaluate(FRAME)

        print("\n[1] 问答页渲染")
        print(f"    助手气泡 {len(chat['assistantBubbles'])} 个, 工具芯片 {chat['toolChips']}, 错误条 {chat['errors']}")
        for b in chat["assistantBubbles"]:
            print(f"      - {b[:70]!r}")
        texts = [b for b in chat["assistantBubbles"]]
        check("1.1 无重复的助手气泡", len(texts) == len(set(texts)), f"气泡文本={[t[:20] for t in texts]}")
        check("1.2 工具轮出现工具芯片", len(chat["toolChips"]) >= 1, f"芯片={chat['toolChips']}")
        check("1.3 无错误提示条", not chat["errors"], f"错误={chat['errors']}")
        check("1.4 用户气泡与提问一致",
              any("47*89" in t for t in chat["userBubbles"]), f"用户气泡={chat['userBubbles']}")
        check("1.5 思考面板存在", len(chat["thinkingPanels"]) >= 1, f"面板数={len(chat['thinkingPanels'])}")
        check("1.6 回答里没有系统提示词特征串",
              not any("你是 Mini Agent" in b or "每次最多调用一个工具" in b for b in texts),
              "已核对")

        # 执行日志页
        page.get_by_role("button", name="执行日志").click()
        page.wait_for_selector(".trace-page", timeout=15000)
        page.wait_for_timeout(600)
        page.screenshot(path=str(OUT / "2-trace.png"), full_page=True)
        trace = page.evaluate(FRAME)

        print("\n[2] 执行日志页渲染")
        print(f"    轮次 {trace['turns']}, 步骤 {trace['stepCount']}, 层级 {trace['depths']}, 兜底 {trace['fallback']}")
        print(f"    步骤标题: {trace['titles']}")
        check("2.1 两轮问答各一条链路", trace["turns"] == 2, f"turns={trace['turns']}")
        check("2.2 一个会话级时间线容器", trace["flows"] == 1, f"flows={trace['flows']}")
        check("2.3 链路有父子层级缩进", len(trace["depths"]) >= 2, f"depths={trace['depths']}")
        check("2.4 没有事件落到兜底文案", trace["fallback"] == 0, f"兜底数={trace['fallback']}")
        check("2.5 没有未翻译的英文事件名", not trace["rawNames"], f"英文名={trace['rawNames']}")
        for expect_text in ["开始运行", "请求模型", "运行完成"]:
            check(f"2.6 出现「{expect_text}」文案", expect_text in trace["pageText"], "")
        check("2.7 工具调用事件已翻译",
              "调用工具：计算器" in trace["pageText"] and "工具执行完成：计算器" in trace["pageText"],
              "已核对")
        # 注意：原始 payload 是**故意**放在折叠的「查看事件数据」里的（供排查用），
        # 所以只校验**可见的标题与备注**里不出现内部字段名，不把那个 details 算作泄露。
        visible = " ".join((t or "") + " " + (m or "") for t, m in zip(trace["titles"], trace["metas"]))
        check("2.8 可见文案里没有内部字段名",
              not any(k in visible for k in ["parent_id", "input_hash", "request_key", "span_id"]),
              f"可见文案={visible[:60]}")
        check("2.9 执行日志页无错误文案",
              "错误" not in trace["pageText"][:400] or "工具执行失败" not in trace["pageText"],
              "已核对")
    finally:
        page.close()
        browser.close()

call("DELETE", f"/api/sessions/{sid}")
print(f"\n已删除测试会话 {sid}")
print(f"截图：{OUT / '1-chat.png'} / {OUT / '2-trace.png'}")

failed = [n for n, ok in RESULTS if not ok]
print(f"\n=== 共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} ===")
for n in failed:
    print(f"  FAILED: {n}")
print("RESULT:", "ALL PASS" if not failed else "HAS FAILURE")
