# tools/qa — 可复跑的验收套件

这里是**能跑出证据**的验收脚本，替代原先散落在 `.pytest-tmp/`（被 gitignore）里的一次性脚本。
区别只有一条，但很关键：**每次运行都会在 `evidence/` 留下机器可读的结果**，
所以"验过了"这句话可以被评审复核、可以被 diff、可以追溯到具体的代码版本。

## 快速开始

```bash
# 先起服务（必须在仓库根目录，models.toml 与 .mini-agent 都是相对 cwd 解析的）
.venv/Scripts/python.exe -m uvicorn --app-dir backend mini_agent.api:app --host 127.0.0.1 --port 8000

# 看有哪些套件
.venv/Scripts/python.exe tools/qa/run.py list

# 跑一遍（除 boundary 外的全部）
.venv/Scripts/python.exe tools/qa/run.py all

# 跑单个
.venv/Scripts/python.exe tools/qa/run.py smoke
```

退出码即结论：有任何 `FAIL` 非 0，可直接接在 `&&` 后面或给 CI 用。

## 套件

| 名称 | 脚本 | 调真实模型 | 需要浏览器 | 覆盖什么 |
|---|---|---|---|---|
| `behavior` | `qa_behavior.py` | 是 | 否 | 回答规范性、权限边界、前端 SSE 载荷、链路合理性 |
| `browser` | `qa_browser_render.py` | 是 | 是 | 回答气泡不重复、执行日志渲染 |
| `matrix` | `qa_matrix.py` | 是 | 否 | 上下文加固回归：工具链、跨轮记忆、提示注入、边界输入 |
| `trace-fields` | `trace_fields_browser.py` | 否 | 是 | 执行日志字段渲染（新/旧载荷形态、重试详情、协议非法） |
| `trace-live` | `trace_live_refresh.py` | 是 | 是 | 运行期间切到执行日志能否跟着刷新 |
| `smoke` | `final_smoke.py` | 是 | 否 | 回答正确 / 真流式 / 工具调用随增量下发 |
| `boundary` | `qa_boundary.py` | 是 | 否 | 输入外置成资源时，消息尾部意图是否保住（发 ~2.8M 字） |

`all` 默认跳过 `boundary`（单次要发 2.8M 字，代价与其它几套不在一个量级），
需要时加 `--with-boundary`。

## 证据格式

每次运行写 `evidence/<套件名>.json`：

```json
{
  "suite": "trace-fields",
  "script": "tools/qa/trace_fields_browser.py",
  "script_sha256_16": "7185ab78395e4178",
  "started_at": "2026-09-15T10:30:49+00:00",
  "duration_s": 7.8,
  "service": { "base": "http://127.0.0.1:8000", "healthy": true, "models": ["deepseek-flash"] },
  "totals": { "total": 15, "passed": 15, "failed": 0, "advisory": 0, "skipped": 0 },
  "checks": [ { "status": "PASS", "name": "...", "detail": "..." } ],
  "result": "ALL PASS"
}
```

为什么记这些：**脚本内容哈希** + **服务与模型的实测状态** + **逐项结果**。
没有前两者，"通过了"无法定位到任何一个可复现的状态——这正是这批脚本此前的问题。

`evidence/history/` 存放迁移前的原始运行日志（`.pytest-tmp/*.log`），
是 2026-09-15 之前那些验收结论的唯一幸存记录，保留供追溯。

## 三条设计约定

**1. 前置条件机器可检，不满足就立刻失败。**
每次运行先探 `/api/health` 与 `/api/models`，缺服务或缺模型直接报 `BLOCKED` 并说明原因，
而不是让脚本跑到一半报一堆看不懂的错——避免把"环境没起"误读成"功能坏了"。

**2. 缺 fixture 报 `SKIP`，不报 `FAIL`。**
`trace-fields` 需要本机历史库里存在特定形态的 trace 事件（旧载荷形态、协议非法事件等）。
它先扫描会话按**能力**发现样本；本机确实没有某类数据时，报 `[SKIP]` 并说明怎么造出来。
原先它写死 4 个会话 UUID，会话一被清理就变成 6 项**假失败**——
一次真实的"验证结论依赖本机状态"事故，别再犯。

**3. 断言只覆盖它真正想证明的事。**
例：错误条检查拆成了两条——
*成功运行*的会话不许出现错误条，*失败运行*必须如实显示错误条。
混成一条"渲染无错误提示条"时，只要样本里混进一个失败的会话就会被误判成渲染故障。

## 新增套件时

1. 脚本用 `print` 输出 `  [PASS] 名称  | 详情` / `[FAIL] ...` / `[SKIP] ...`（`run.py` 靠这个解析）；
2. 结尾 `print("RESULT:", "ALL PASS" if 无失败 else "HAS FAILURE")`；
3. 有失败时 `sys.exit(1)`；
4. 在 `run.py` 的 `SUITES` 里登记；
5. **不要硬编码会话 id / 端口 / 历史数据快照**——按能力自发现，缺数据报 SKIP。
