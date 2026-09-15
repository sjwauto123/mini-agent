"""最终冒烟：对 8000 上真实运行的服务提一条需要工具的问题，验证
① 回答正确 ② 首片增量远早于结束（真流式）③ tool_calls 在运行期间就随增量下发（本次修复点）。
跑完删掉测试会话。"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 绕开系统代理，直连本机


def call(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with OPEN.open(req) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw else None


models = call("GET", "/api/models")
model_name = models[0]["name"] if isinstance(models[0], dict) else models[0]
session = call("POST", "/api/sessions", {"model_name": model_name, "timezone": "Asia/Shanghai"})
sid = session["id"]
print(f"[1] session = {sid}  model = {model_name}")

t0 = time.time()
run = call("POST", f"/api/sessions/{sid}/runs", {"message": "请用计算器算一下 1234 × 5678 等于多少？"})
rid = run["run_id"]
print(f"[2] run = {rid}  created={run['created']}")

req = urllib.request.Request(BASE + f"/api/runs/{rid}/events", headers={"Accept": "text/event-stream"})
first_message_at = None
tool_calls_at = None
ended_at = None
final_text = ""
answer_msg = None
counts = {}

with OPEN.open(req) as resp:
    event = None
    for raw_line in resp:
        line = raw_line.decode("utf-8", "replace").rstrip("\n")
        if line.startswith("event: "):
            event = line[7:]
            counts[event] = counts.get(event, 0) + 1
            continue
        if not line.startswith("data: "):
            continue
        payload = json.loads(line[6:])
        now = time.time()
        if event == "message":
            if first_message_at is None:
                first_message_at = now
            if payload.get("tool_calls") and tool_calls_at is None:
                tool_calls_at = now
            if payload.get("content"):
                answer_msg = payload
        elif event == "snapshot":
            # 快照载荷就是 run 本身的字典（不是 {"run": ...}）
            status = payload.get("status")
            if status in ("completed", "failed", "cancelled", "limit_reached", "interrupted"):
                ended_at = now
                break
        elif event == "notice":
            print(f"    [notice] {payload}")

first_at = time.time()
final_text = (answer_msg or {}).get("content") or ""
messages = call("GET", f"/api/sessions/{sid}/messages")
for m in messages:
    if m.get("role") == "assistant" and m.get("content") and not m.get("tool_calls"):
        final_text = m["content"]

print("\n=== 结果 ===")
print(f"事件计数        : {counts}")
print(f"首片增量耗时    : {first_message_at - t0:.2f}s" if first_message_at else "首片增量        : 无")
print(f"tool_calls 到达 : {tool_calls_at - t0:.2f}s" if tool_calls_at else "tool_calls      : 未随增量下发 ✗")
print(f"整轮结束耗时    : {ended_at - t0:.2f}s" if ended_at else "结束            : 未观测到终态")
print(f"终答            : {final_text[:160]!r}")

ok_answer = "7006652" in final_text.replace(",", "").replace(" ", "")
ok_stream = first_message_at is not None and ended_at is not None and (first_message_at - t0) < (ended_at - t0) * 0.8
ok_toolcalls = tool_calls_at is not None and ended_at is not None and tool_calls_at <= ended_at
print("\n=== 断言 ===")
print(f"[{'PASS' if ok_answer else 'FAIL'}] 回答正确（含 7006652）")
print(f"[{'PASS' if ok_stream else 'FAIL'}] 真流式（首片明显早于结束）")
print(f"[{'PASS' if ok_toolcalls else 'FAIL'}] tool_calls 在运行期间下发（本次修复点）")

call("DELETE", f"/api/sessions/{sid}")
print(f"\n[cleanup] session {sid} 已删除")
print("RESULT:", "ALL PASS" if (ok_answer and ok_stream and ok_toolcalls) else "HAS FAILURE")
