"""执行日志展示层验证（真实浏览器 + 本机历史数据，**不调用模型**）。

验证三件事：
1. 字数事件不再恒显示 0 字（新形态读 `chars`、旧形态回退读 `content` 长度）；
2. 重试/失败事件显示后端记下的原始详情（`detail`）；
3. 协议非法事件显示 `finish_reason` / 正文字数 / 推理长度，且所有事件都命中中文文案
   （不落"记录了一条运行事件"兜底分支）。

为什么要改成"按能力自发现"
------------------------
原先这里写死了 4 个会话 UUID。会话一旦被清理，页面返回"会话不存在。"，
于是 6 项检查变成**假失败**——一次真实的"验证结论依赖本机状态"事故。
现在改为先扫描本机会话的 trace 事件，找出分别提供了
「新形态增量 / 旧形态增量 / 重试 / 协议非法载荷」的会话，再拿它们做断言；
某类历史数据在本机确实不存在时，报 `[SKIP]` 并说明怎么造出来，而不是报 FAIL。

这样"通过"才有意义：它证明的是**渲染逻辑对**，而不是**这台机器上恰好有那几条数据**。

前提：8000 上有真实服务；本机数据库里有过相应形态的运行记录。
"""
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8000"
OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 绕开系统代理，直连本机
SCAN_LIMIT = 60  # 最多扫描多少个最近会话

# 前端 ERROR_MESSAGES 里 model_protocol_error 的文案（改文案时这里要同步）
MAPPED_REASON_COPY = "模型返回了无法解析的响应"
GENERIC_REASON_COPY = "原因：执行过程中出现问题"

# 能力名 -> (说明, 判定函数)
CAPS = {
    "new_delta": (
        "新形态 assistant.delta（chars/iteration）",
        lambda e: e["event_type"] == "assistant.delta" and "chars" in e["payload"],
    ),
    "old_delta": (
        "旧形态 assistant.delta（content）",
        lambda e: e["event_type"] == "assistant.delta"
        and "content" in e["payload"]
        and "chars" not in e["payload"],
    ),
    "retry": ("模型重试事件", lambda e: e["event_type"] == "model.retry"),
    "invalid_output": (
        "协议非法事件（带 output.finish_reason）",
        lambda e: e["event_type"] == "model.invalid" and "output" in e["payload"],
    ),
    "mapped_code": (
        "已映射的错误码（model_protocol_error）",
        lambda e: e["event_type"] == "model.invalid"
        and e["payload"].get("code") == "model_protocol_error",
    ),
    "raw_reason": (
        "未映射码 + 原始 reason 原文",
        lambda e: e["event_type"] in ("model.invalid", "model.failed")
        and bool(e["payload"].get("reason")),
    ),
}

# 造数据提示：本机缺某类形态时告诉用户怎么补
HOW_TO_MAKE = {
    "new_delta": "跑一次真实问答（当前代码即产出 chars 形态）",
    "old_delta": "只有 2026-09-14 之前的运行记录才有 content 形态，新库无法再产生",
    "retry": "把 models.toml 的 endpoint 指向一个连不上的地址，提一个问题即可触发重试",
    "invalid_output": "触发一次协议非法（如让模型一次返回多个 tool_call）",
    "mapped_code": "同上，且 code 为 model_protocol_error",
    "raw_reason": "需要 model.invalid / model.failed 且 payload 带 reason 字段",
}

FRAME = """() => {
  const txt = e => e ? e.textContent.trim() : null;
  const steps = [...document.querySelectorAll('.trace-step')];
  return {
    turns: document.querySelectorAll('.trace-turn').length,
    steps: steps.map(e => ({
      depth: [...e.classList].find(c => c.startsWith('depth-')),
      title: txt(e.querySelector('.trace-title strong')),
      description: txt(e.querySelector('.trace-content p')),
      meta: txt(e.querySelector('.trace-meta')),
    })),
    fallback: steps.filter(e => e.textContent.includes('记录了一条运行事件')).length,
    errors: [...document.querySelectorAll('.error-bar')].map(txt),
  };
}"""

RESULTS: list[tuple[str, str, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, "PASS" if passed else "FAIL", detail))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}" + (f"  | {detail}" if detail else ""))


def skip(name: str, why: str) -> None:
    RESULTS.append((name, "SKIP", why))
    print(f"  [SKIP] {name}  | {why}")


def call(method: str, path: str, body=None):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with OPEN.open(req, timeout=60) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw else None


def find_chrome():
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    return next((p for p in candidates if Path(p).exists()), None)


def discover() -> tuple[dict, dict, set]:
    """扫描本机会话的 trace。

    返回 ({能力: 会话id}, {能力: 命中的事件}, {该会话存在失败运行}).
    第三个返回值用于区分"成功会话不该有错误条"与"失败运行应该有错误条"——
    原先把两者混在一条断言里，导致失败的会话一被纳入样本就被误判成渲染故障。
    """
    sessions = call("GET", "/api/sessions") or []
    picked: dict[str, str] = {}
    samples: dict[str, dict] = {}
    failed_sessions: set[str] = set()
    print(f"扫描 {min(len(sessions), SCAN_LIMIT)} 个最近会话的 trace …")
    for session in sessions[:SCAN_LIMIT]:
        sid = session["id"]
        try:
            events = call("GET", f"/api/sessions/{sid}/trace") or []
        except urllib.error.URLError:
            continue
        for event in events:
            if event["event_type"] == "run.finished" and event["payload"].get("status") != "completed":
                failed_sessions.add(sid)
        for name, (_, hit) in CAPS.items():
            if name in picked:
                continue
            for event in events:
                if hit(event):
                    picked[name] = sid
                    samples[name] = event
                    break
        if len(picked) == len(CAPS):
            break
    print(f"命中 {len(picked)}/{len(CAPS)} 类能力：{list(picked)}")
    print(f"其中含失败运行的会话 {len(failed_sessions)} 个\n")
    return picked, samples, failed_sessions


def step_text(data: dict) -> str:
    return "\n".join(
        (s["title"] or "") + "|" + (s["description"] or "") + "|" + (s["meta"] or "")
        for s in data["steps"]
    )


def run_assertions(picked: dict, samples: dict, collected: dict, failed_sessions: set) -> None:
    all_steps = [s for d in collected.values() for s in d["steps"]]

    check(
        "所有会话的步骤都命中中文文案（无兜底分支）",
        all(d["fallback"] == 0 for d in collected.values()),
        f"兜底条数={[d['fallback'] for d in collected.values()]}",
    )

    clean = [sid for sid in collected if sid not in failed_sessions]
    broken = [sid for sid in collected if sid in failed_sessions]
    if clean:
        bad = [(sid[:8], collected[sid]["errors"]) for sid in clean if collected[sid]["errors"]]
        check("成功运行的会话不出现错误条", not bad, f"异常会话={bad[:2]}")
    else:
        skip("成功运行的会话不出现错误条", "本次样本里没有全部成功的会话")
    if broken:
        missing = [sid[:8] for sid in broken if not collected[sid]["errors"]]
        check(
            "失败的运行如实显示错误条",
            not missing,
            f"失败会话={len(broken)} 个，未显示错误条={missing}",
        )
    else:
        skip("失败的运行如实显示错误条", "本次样本里没有失败运行可验")

    check(
        "链路树存在两级缩进",
        {s["depth"] for s in all_steps} >= {"depth-0", "depth-1"},
        f"出现层级={sorted({s['depth'] for s in all_steps})}",
    )

    zero_meta = [s["meta"] for s in all_steps if re.fullmatch(r"(共 )?0 字", s["meta"] or "")]
    check("不再出现 '0 字'", not zero_meta, f"零值备注={zero_meta[:3]}")

    if "new_delta" in picked:
        chars = samples["new_delta"]["payload"]["chars"]
        text = step_text(collected[picked["new_delta"]])
        check("新形态字数按 chars 正确显示", f"共 {chars} 字" in text, f"期望『共 {chars} 字』")
    else:
        skip("新形态字数按 chars 正确显示", HOW_TO_MAKE["new_delta"])

    if "old_delta" in picked:
        steps = collected[picked["old_delta"]]["steps"]
        readable = [
            s["meta"]
            for s in steps
            if (s["meta"] or "").endswith("字") and "共" not in (s["meta"] or "")
        ]
        check("旧形态字数仍可读（回退读 content 长度）", bool(readable), f"样例={readable[:3]}")
    else:
        skip("旧形态字数仍可读（回退读 content 长度）", HOW_TO_MAKE["old_delta"])

    if "retry" in picked:
        steps = collected[picked["retry"]]["steps"]
        text = step_text(collected[picked["retry"]])
        retry_steps = [s for s in steps if "重试" in (s["title"] or "")]
        check(
            "重试事件显示第几次尝试",
            any("次尝试" in (s["meta"] or "") for s in retry_steps),
            f"重试备注={[s['meta'] for s in retry_steps][:3]}",
        )
        check(
            "重试事件显示原始详情（detail）",
            "详情：" in text,
            f"重试描述样例={[s['description'] for s in retry_steps][:1]}",
        )
    else:
        skip("重试事件显示第几次尝试", HOW_TO_MAKE["retry"])
        skip("重试事件显示原始详情（detail）", HOW_TO_MAKE["retry"])

    if "invalid_output" in picked:
        text = step_text(collected[picked["invalid_output"]])
        check("协议非法事件显示 finish_reason", "finish_reason=" in text, "")
        check("协议非法事件显示正文字数", "正文" in text and "字" in text, "")
        check("协议非法事件显示推理长度", "推理" in text, "")
    else:
        for name in ("协议非法事件显示 finish_reason", "协议非法事件显示正文字数", "协议非法事件显示推理长度"):
            skip(name, HOW_TO_MAKE["invalid_output"])

    if "mapped_code" in picked:
        text = step_text(collected[picked["mapped_code"]])
        check("已映射的错误码仍显示具体原因", MAPPED_REASON_COPY in text, "")
    else:
        skip("已映射的错误码仍显示具体原因", HOW_TO_MAKE["mapped_code"])

    if "raw_reason" in picked:
        text = step_text(collected[picked["raw_reason"]])
        check("有详情时不再输出无信息量的通用原因", GENERIC_REASON_COPY not in text, "")
        check("未映射码给出原始 reason 原文", "原因：" in text or "详情：" in text, "")
    else:
        skip("有详情时不再输出无信息量的通用原因", HOW_TO_MAKE["raw_reason"])
        skip("未映射码给出原始 reason 原文", HOW_TO_MAKE["raw_reason"])


def main() -> int:
    picked, samples, failed_sessions = discover()
    if not picked:
        print("本机没有任何可用的历史运行记录，无法验证渲染层。请先跑一次问答。")
        return 2

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=find_chrome())
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        collected: dict[str, dict] = {}
        for sid in dict.fromkeys(picked.values()):
            labels = [n for n, s in picked.items() if s == sid]
            page.goto(f"{BASE}/?session={sid}")
            page.wait_for_selector(".composer button.send", timeout=30000)
            page.get_by_role("button", name="执行日志").click()
            page.wait_for_selector(".trace-page", timeout=15000)
            page.wait_for_timeout(700)
            data = page.evaluate(FRAME)
            collected[sid] = data
            print(f"[{sid[:8]}] 覆盖 {labels}")
            print(f"    轮次 {data['turns']}，步骤 {len(data['steps'])}，错误条 {data['errors']}")
        browser.close()

    print("\n=== 断言 ===")
    run_assertions(picked, samples, collected, failed_sessions)

    print("\n=== 汇总 ===")
    failed = [n for n, st, _ in RESULTS if st == "FAIL"]
    skipped = [n for n, st, _ in RESULTS if st == "SKIP"]
    print(f"通过 {len(RESULTS) - len(failed) - len(skipped)}/{len(RESULTS)}，跳过 {len(skipped)}")
    if failed:
        print("失败项:", failed)
    if skipped:
        print("跳过项:", skipped)
    print("RESULT:", "ALL PASS" if not failed else "HAS FAILURE")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
