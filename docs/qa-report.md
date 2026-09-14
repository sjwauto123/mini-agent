# Mini Agent 多轮问答验收报告

- 验收日期：2026-09-13
- 验收对象：`mini-agent`（后端 `backend/mini_agent/` + 前端 `frontend/src/`）
- 模型：`deepseek-flash`（思考型模型，`mode = native`）
- 结论：**通过**。后端多轮问答 20/20 通过，前端真实浏览器验证全部通过；期间发现 5 个缺陷（其中 2 个为阻断级），均已定位并修复。

---

## 一、验收目标

按需求对「思考过程 + 回答内容 + 链路执行 + 前端页面响应」四件事做多轮问答测试，发现问题即时修复并留档：

1. **思考过程合理**：每轮回答都要有思考过程，且是干净的中文判断依据（不泄漏协议标签）。
2. **回答内容合理**：数值可核对、mock 数据如实标注来源、工具失败要诚实上报而不是编造。
3. **链路合理**：工具序列与模型轮次符合预期，无协议退化、无硬异常事件。
4. **前端响应合理**：消息不丢、历史回答不被清空、会话列表可折叠、侧栏固定、执行日志可读。
5. **问题及时修复**，并把测试过程写成文档。

---

## 二、验收结论速览

| 验收项 | 方法 | 结果 |
| --- | --- | --- |
| 后端多轮问答 | API 驱动 10 个用例 × 2 轮（同一会话内累积） | **20/20 通过** |
| 思考过程覆盖 | 每轮断言「终答带思考过程」+「工具轮带决策摘要」 | 20/20 |
| 链路（trace） | 断言工具序列、事件序列、无硬异常事件 | 20/20 |
| 前端页面 | Playwright 真实 Chrome，多轮问答 + 回归 + 交互 | **全部通过** |
| 单元/集成测试 | `pytest` 全量 | **46 passed, 2 skipped**（含本轮新增的 WAL 回归用例） |
| 发现并修复的缺陷 | — | 5 项（2 项阻断级） |

---

## 三、发现并修复的问题

### P0-1　JSON 模式与原生工具调用不兼容，模型周期性输出空正文

**现象**：多轮问答中，模型偶发返回 `finish_reason=stop`、正文仅含空白字符（约 45–70 个空格），既不是回答也不是工具调用；同一上下文重试**必然复现**。前端表现为「一直转圈」，测试表现为轮次超时。

**定位过程**（探针脚本见 `.pytest-tmp/bisect*.py`、`probe_*.py`）：

| 变量隔离 | 结果 |
| --- | --- |
| 同一会话连续 4 轮 vs 每轮新会话 | 同会话 3/4 轮空白；新会话 0 失败 → 与**历史累积**相关 |
| 去掉 `response_format` / temp=0 / 加大 max_tokens / 硬化提示词 / 不回传 reasoning | 基线 9/10，其余多数 10/10 → **调参无法根治** |
| 每配置 ×24 次 | 全部 24/24 干净 → 说明空白是**偶发突发**，非配置问题 |
| 二分（V0–V8） | 触发条件 = **工具清单 + 工具调用形态的历史消息**，且仅在 `response_format={"type":"json_object"}` 下出现 |
| 相同形态改用原生工具调用（native） | **6/6 通过** |

**结论**：`deepseek-flash` 在「带工具清单 + 工具调用形态历史」的上下文里，无法同时满足 `json_object` 约束，会以 `stop` 只吐空白。原生协议在完全相同的形态下 100% 可靠。

**修复**：`models.toml` 中 `[models.deepseek-flash]` 的 `mode = "json"` → **`mode = "native"`**。（`models.example.toml` 本就是 native，印证 native 才是预期默认。）

**配套加固**：
- `model.py` 新增 `empty_response`（正文全空白）可恢复错误码，与 `model_truncated`（`finish_reason=length`）区分开；
- `runtime.py` 遇到空白正文直接**换最小上下文重试**（其他格式错误先原样重试，最后一次才退最小上下文），并区分「硬异常」(`model.failed`) 与「软异常」(`model.invalid`/`model.retry`)。

### P0-2　SSE 断开导致 SQLite 残留读事务，写接口持续 500

**现象**：服务运行一段时间后，`POST /api/sessions` 持续返回 `500 Internal Server Error`；但 `GET /api/sessions` 正常。前端无法新建会话、无法发消息；浏览器多轮测试卡在第 2 轮。

**定位过程**：
1. 数据库最后成功写入时间为 `22:35`，此后所有写入静默失败 → 与「测试脚本会话隔离」无关，是**服务端写入通道被堵死**。
2. 逐层对比写入路径（同一文件、同一时刻）：

   | 写入方式 | 结果 |
   | --- | --- |
   | 原始 `sqlite3`（`BEGIN IMMEDIATE` + `INSERT` + `ROLLBACK`） | OK |
   | 原始 `aiosqlite`（同上） | OK |
   | `sqlite3` `BEGIN` + `INSERT` + **`COMMIT`** | **FAIL：database is locked** |
   | SQLAlchemy 异步引擎（相同文件） | **FAIL：database is locked** |
   | SQLAlchemy 异步引擎（全新临时库） | OK |

   关键在最后两行：**回滚不需要独占锁，提交才需要**。所以「能读、能回滚、不能提交」= 有连接持着未释放的**读事务**（共享锁），把提交要的独占锁挡死了。全新库正常 → 问题在**这个库被某个连接占着**。

3. 服务日志 `.pytest-tmp/server7.log` 给出了决定性堆栈：

   ```
   INFO: "GET /api/runs/62adb2b2-.../events HTTP/1.1" 200 OK
   Exception terminating connection <AdaptedConnection <Connection(Thread-2, ...)>>
     ... sqlalchemy/pool/base.py _close_connection → do_terminate → aiosqlite close() ...
   asyncio.exceptions.CancelledError: Cancelled via cancel scope ... RequestResponseCycle.run_asgi()
   ```

**机制**：浏览器关闭 `EventSource`（或页面跳走）时，uvicorn **取消**该 SSE 请求任务。SQLAlchemy 恰好在取消期间归还/关闭池中的 aiosqlite 连接，这个关闭动作被一并取消，连接**带着未提交的读事务泄漏**。SQLite 处于回滚日志模式（`journal_mode=delete`）时，残留读者会阻塞所有写入提交 → 之后创建会话、保存消息等写操作全部 500。

**修复**：`backend/mini_agent/storage.py` 的连接初始化改为 **WAL 模式**（读写互不阻塞），并保留 `busy_timeout=5000`：

```python
cursor.execute("PRAGMA journal_mode=WAL")
cursor.execute("PRAGMA synchronous=NORMAL")
cursor.execute("PRAGMA foreign_keys=ON")
cursor.execute("PRAGMA busy_timeout=5000")
```

**验证**：
- 场景复现验证：先让服务连接把库切到 WAL，再故意用另一个连接持有一个未提交的读事务（完全复刻故障现场），此时写入**立即成功**（`0.00s`）。修复前该场景必然 `database is locked`。
- 新增回归用例 `tests/test_storage.py::test_sqlite_uses_wal_and_survives_lingering_reader`，断言 `journal_mode == "wal"` 且残留读者在场时写入成功。

### P1-1　发消息时上一条回答被清空（前端竞态，已修）

**现象**：新建会话后立刻发消息，界面空白（服务端其实已处理）；多轮时上一条回答消失。

**原因**：`loadMessages(id)` 是异步的，返回后无条件 `setMessages(服务端历史)`；若期间用户已发出消息（本地乐观追加）或正在流式接收，飞行中的旧快照返回后会把界面状态**整体覆盖**掉。

**修复**（`frontend/src/App.tsx`）：
- 新增 `messagesLoadRef` 代次守卫：加载前 `generation = ++messagesLoadRef.current`，返回后若 `selectedRef.current !== id || generation !== messagesLoadRef.current` 则**作废本次覆盖**；
- `send()` 在追加乐观消息前 `messagesLoadRef.current += 1`；
- 流式 `message` 事件只按服务端给出的 `seq` 更新对应那条消息，不做「最后一条助手消息」的猜测。

### P1-2　思考过程展示（已修）

原生协议下模型返回的 `reasoning_content` 不稳定（约 60–65% 概率才有），导致思考过程时有时无。

**修复**：
- 系统提示改为「每次回复第一行必须是『思考：<一句简短中文判断依据>』」，且明确禁止在回答正文里输出「决策说明」「思考过程」这类标签；
- `model.py` 新增 `split_decision_note()`，把首行的 `思考：…` 拆成思考过程，其余为回答正文；未按约定返回时原样保留（**不伪造**思考）；
- 协议提示从 `insert(1, …)` 改到消息末尾 `append(…)`（更靠近生成位置），遵从率从约 5/10 提升到 9/10 以上。

### P2-1　执行日志里流式片段显示为原始事件名（已修）

**现象**：执行日志页把流式片段渲染成 `assistant.delta` 原始事件名 + 通用描述「Agent 记录了一条运行事件。」，一次回答出现 5 行，既不可读又显得像故障。

**修复**（`frontend/src/App.tsx`）：为 `assistant.delta` 增加呈现规则 →「输出回答片段 / 回答输出完成」，并显示本片段字数。

### 其他加固

- `runtime.py`：模型调用的异常捕获由 `(httpx.TimeoutException, httpx.NetworkError)` 改为 **`httpx.TransportError`**，覆盖 `RemoteProtocolError` 等协议层错误（此前这类错误未被分类重试，直接暴露成未知异常）。
- `model.py`：JSON 模式增强兼容——容忍模型把 JSON 包在 ```` ```json ```` 代码块中（`_unwrap_json_text`）；`_strict_object` 异常带 `code` 与正文开头便于定位。
- `context.py`：`prepare(..., only_run_id=...)` 支持「只保留本轮消息」的最小上下文恢复路径。

---

## 四、后端多轮问答逐用例记录

测试方式：直接调用 API 在同一会话内连续发问（因此 R4、R7 等同时验证了多轮上下文记忆），每轮读取 `run` 状态、`trace` 事件、消息内容后自动断言。

断言项：状态 `completed` / 工具序列符合预期 / 终答非空 / 终答带思考过程 / 工具轮带决策摘要 / 无硬异常事件。

### 第 1 轮（`qa-report1.json`，会话 `e0cd1456`）

| 用例 | 问题 | 工具 | 模型调用 | 思考过程（摘） | 回答要点 | 判定 |
| --- | --- | --- | --- | --- | --- | --- |
| R1 | 你好，请用一句话说明你可以做什么。 | — | 1 | 用户只是询问能力范围，无需调用工具 | 正确自述能力边界 | ✅ |
| R2 | 帮我算一下 (23*17+9)/4 等于多少 | calculator | 2 | 工具已返回明确结果，直接汇报 | 100 | ✅ |
| R3 | 北京今天天气怎么样？适合跑步吗？ | weather | 2 | 雨天且气温偏低，户外跑步不太合适 | 雨天 18℃，建议不跑，标注 mock | ✅ |
| R4 | 那上海呢？ | weather | 2 | 已拿到上海当日模拟天气 | 晴 25℃，正确继承上下文 | ✅ |
| R5 | 如果明天北京下雨，就加一条「记得带伞」待办 | weather + todo | 6 | 明天为雨天，待办已添加 | 条件判断 + 落库，正确 | ✅ |
| R6 | 帮我查一下拉萨今天的天气 | weather | 2 | 工具返回失败，说明城市不在支持范围 | 诚实上报 `location_not_supported` | ✅ |
| R7 | 现在我的待办清单里有哪些内容？ | todo | 2 | 待办列表已成功返回 | 列出 R5 写入的「记得带伞」 | ✅ |
| R8 | 帮我搜索一下 Mini Agent 是什么 | search | 2 | 搜索返回空，需如实告知 | 如实说明 0 条结果，**不编造** | ✅ |
| R9 | 读取资源 … 的前 200 个字符 | resource_read | 2 | 资源已读取完整 | 正确复述内容 | ✅ |
| R10 | 帮我算 5/0 等于多少 | calculator | 2 | 工具返回除零错误 | 说明无定义 + `division_by_zero` | ✅ |

### 第 2 轮（`qa-report2.json`，会话 `63292742`，独立会话复测）

10/10 通过。其中：

- **R5**：`weather → todo` 串联，3 次模型调用，条件满足后落库；
- **R6**：出现一次软异常 `model.invalid`（`empty_response`，模型只回了「思考：需要查询拉萨…」23 字正文），系统自动**退最小上下文重试并自愈**（`repair_events` 记录 `{"code": "empty_response", "minimal_context": true}`），最终仍诚实上报 `location_not_supported`。这一条正好验证了 P0-1 的加固路径在真实流量中生效。

**两轮合计：20/20 轮全部检查通过，0 硬异常、0 协议退化。**

---

## 五、前端浏览器验证记录

方式：Playwright 驱动真实 Chrome（`channel="chrome"`）访问 `http://127.0.0.1:8000`，先经 API 预建一个**全新空会话**并以 `?session=<id>` 直接进入（避免挂载期自动选中历史会话带来的计数干扰），再执行多轮问答与交互检查。

| 检查项 | 期望 | 实测 | 判定 |
| --- | --- | --- | --- |
| 第 1 轮：我方消息可见 | 1 | 1 | ✅ |
| 第 1 轮：回答数 / 思考块 / 工具提示 | 1 / ≥1 / 1 | 1 / 2 / 「调用了 1 个工具」 | ✅ |
| 第 2 轮：我方消息可见 | 2 | 2 | ✅ |
| 第 2 轮：回答数（**上一条回答不被清空**） | 2 且均非空 | 2（北京天气 + `12 × 12 = 144`） | ✅ |
| 第 2 轮：思考块 | ≥2 | 4 | ✅ |
| 新建会话后**立即**发消息（回归点） | 乐观消息不丢 | `user_message_kept = true` | ✅ |
| 会话列表折叠 / 再次展开 | 均可 | 收起 `true` / 展开 `true` | ✅ |
| 执行日志：运行分组 / 步骤 | 可读、步骤完整 | 1 组 / 9 步（含工具与模型轮次） | ✅ |
| 侧栏固定：滚动会话列表后主区矩形 | 不变 | `x=260, width=1180, height=900` 完全一致 | ✅ |

截图：`.pytest-tmp/browser-chat.png`（多轮问答）、`.pytest-tmp/browser-trace.png`（执行日志）。结构化结果：`.pytest-tmp/browser-report.json`。

> 说明：回归用例中「新建会话后立刻发送」问的是「上海呢？」，而新会话本就没有上文，因此模型如实回答「没看到之前你在问什么」——这是**正确行为**；该用例要验证的是用户消息**不被历史快照抹掉**，实测通过。

---

## 六、测试命令与产物

```bash
# 后端多轮问答（需服务在 8000 端口运行）
.venv/Scripts/python.exe .pytest-tmp/qa_suite.py

# 前端浏览器验证
.venv/Scripts/python.exe .pytest-tmp/browser_check.py

# 全量单测
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider

# 前端构建
cd frontend && npm run build
```

产物：

| 文件 | 内容 |
| --- | --- |
| `.pytest-tmp/qa-report1.json` / `qa-report2.json` | 后端 10 用例 × 2 轮的逐轮明细 |
| `.pytest-tmp/browser-report.json` | 前端浏览器验证结构化结果 |
| `.pytest-tmp/browser-chat.png` / `browser-trace.png` | 前端截图 |
| `.pytest-tmp/server7.log` | P0-2 的故障现场日志（含决定性堆栈） |
| `.pytest-tmp/bisect*.py`、`probe_*.py` | P0-1 的变量隔离探针 |

> 注意：本机注册表配置了系统代理，Python 的 `urllib` 默认会走代理并给 `127.0.0.1` 返回 502。测试脚本已显式 `ProxyHandler({})` 直连；手工用 `curl` 不受影响。

---

## 七、遗留问题与建议

1. **P0-2 的泄漏根因仍在**（WAL 已让后果不再致命，但连接泄漏本身没消除）：SSE 请求被取消时，归还数据库连接的动作可能被中断。建议后续让流式读的数据库调用具备「取消安全」（例如对单次读操作做 shield），或改造 SSE 长轮询。残留读者长时间存在还会阻止 WAL checkpoint。
2. **WAL 文件增长**：长期运行建议关注 `.mini-agent/state.db-wal` 大小，必要时定期 checkpoint。
3. **执行日志仍偏啰嗦**：一次回答会出现多条「输出回答片段」，可考虑合并连续片段为一条。
4. 本地库中累积了 41 个测试会话，建议清理后再做演示。
5. 本轮所有改动（`models.toml`、`model.py`、`context.py`、`runtime.py`、`storage.py`、`App.tsx`、`tests/test_storage.py`、`docs/qa-report.md`）尚未提交 git。
