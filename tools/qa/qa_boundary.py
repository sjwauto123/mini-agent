"""定向边界测试：真正触发"用户输入外置成资源"这条路径。

背景：runtime.py:237 的外置阈值 = input_budget * soft_context_ratio。
deepseek-flash 的 context_window=1048576、output_reserve=8192、soft=0.70
→ 阈值 ≈ 728,268 token ≈ 2.19MB（estimate_tokens = 字节数/3）。
所以 6500 字的输入根本不会外置，必须发 ~2.4MB 才能走到这条路径。

要回答的问题：
1. 外置路径在真实模型下是否可用（不 500、能 completed）；
2. 模型能否自己去读资源；
3. **关键风险**：用户真正的问题在消息末尾，会跟正文一起被外置；
   可见消息里只剩一句"已保存为资源 X"，模型是否还能答出问题？
"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({}))
TERMINAL = {"completed", "failed", "cancelled", "limit_reached", "interrupted"}


def call(method, path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with OPEN.open(req) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw else None


def describe(turn):
    """按 call_id 去重，避免 assistant.tool_calls 与 tool 消息重复计数。"""
    seen, tools, answer = set(), [], ""
    for message in turn:
        for item in message.get("tool_calls") or []:
            if item["id"] not in seen:
                seen.add(item["id"])
                tools.append(item["function"]["name"])
        if message.get("role") == "tool":
            key = message.get("call_id") or message.get("tool_call_id")
            name = message.get("name") or "?"
            if key and key in seen:
                continue
            if name not in seen:
                tools.append(name)
        if message.get("role") == "assistant" and message.get("content") and not message.get("tool_calls"):
            answer = message["content"]
    return tools, answer


models = call("GET", "/api/models")
model_name = models[0]["name"]
print(f"模型 = {model_name}")

# 计算实际阈值，避免再次拍脑袋。
cfg = next((m for m in models if m["name"] == model_name), {})
print(f"模型配置字段 = {sorted(cfg.keys())}")

sid = call("POST", "/api/sessions", {"model_name": model_name, "timezone": "Asia/Shanghai"})["id"]
print(f"会话 = {sid}")

# 正文是重复填充，真正的问题放在**末尾**——这正是会被一起外置的部分。
# 用唯一标记作为问题内容：标记只可能来自"内联保留的尾部"（翻页预算 2% 读不到 2.8M 字的末尾）。
filler = "这是一段用于测试的填充文本。" * 200_000  # 200k 句 × 14 字 ≈ 2.8M 字 ≈ 8.4MB ≈ 2.8M token，稳超阈值
MARKER = "MARKER-TAIL-9F3K"
question = f"请只回复这个标记，不要回复任何其他内容：{MARKER}"
message = filler + "\n" + question
print(f"输入字符数 = {len(message)}, 字节数 = {len(message.encode())}, 估算 token ≈ {len(message.encode())//3}")

t0 = time.time()
run_id = call("POST", f"/api/sessions/{sid}/runs", {"message": message})["run_id"]
deadline = time.time() + 300
while time.time() < deadline:
    run = call("GET", f"/api/runs/{run_id}")
    if run["status"] in TERMINAL:
        break
    time.sleep(2)
print(f"运行状态 = {run['status']}  用时 = {time.time()-t0:.1f}s  error = {run.get('error')}")

messages = call("GET", f"/api/sessions/{sid}/messages")
turn = [m for m in messages if m.get("run_id") == run_id]
user_msg = next((m for m in turn if m.get("role") == "user"), None)
visible = (user_msg or {}).get("content", "")
externalized = "保存为资源" in visible
print(f"[用户可见消息（模型实际看到的）] {visible[:160]!r}")
print(f"[是否外置] {externalized}")

tools, answer = describe(turn)
print(f"[工具调用] {tools}")
print(f"[终答] {answer[:300]!r}")

print("\n--- 判定 ---")
print(f"A. 外置路径可达             : {externalized}")
print(f"B. 运行未崩                 : {run['status'] == 'completed'}")
print(f"C. 模型自行读取了资源       : {'resource_read' in tools or 'resource_search' in tools}")
print(f"D. 尾部问题（标记）对模型可见: {MARKER in answer}")
print(f"E. 可见消息以尾部问题结尾   : {visible.endswith(question)}")

ok = externalized and run["status"] == "completed" and MARKER in answer
print("RESULT:", "ALL PASS" if ok else "HAS FAILURE")
if not ok:
    print(f"  模型终答 = {answer[:200]!r}")

call("DELETE", f"/api/sessions/{sid}")
print("已清理会话")
