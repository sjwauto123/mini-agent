# .githooks — 提交门禁

把 `AGENTS.md` 里写的规则变成**会真的拦住你**的东西。

## 启用（每台机器一次）

```bash
git config core.hooksPath .githooks
```

`core.hooksPath` 是**仓库本地**配置，不随 `clone` 传播——这是 git 的设计，不是疏漏。
换机器或新 clone 后要再执行一次。

为什么不用 `.git/hooks/`：那里不进版本库，对下一个人、另一台机器、任何 agent 都不存在。

## 文件

| 文件 | 作用 |
|---|---|
| `pre-commit` | 门禁 1（批量删除）、门禁 3（人工过目）、门禁 4（证据过期提醒） |
| `commit-msg` | 门禁 2（结构等价）、门禁 2b（注释类只增不删） |
| `lib.sh` | 两者共用的函数（解释器解析、暂存区读取） |

## 为什么门禁 2 在 `commit-msg` 而不是 `pre-commit`

`git commit` 的执行顺序是：**① 跑 pre-commit → ② 才写提交信息 → ③ 跑 commit-msg**。

所以 pre-commit 阶段读 `.git/COMMIT_EDITMSG` 拿到的是**上一条**提交的信息。
最早这两道门禁都写在 pre-commit 里，后果是门禁 2 会"看情况随机生效"：
「注释：+ 改了个字符串字面量」被放行，而上一条恰好也是 `注释：` 时才偶然拦住。

这个 bug 单元测试全绿也没发现（它们用环境变量直接喂信息），只有真跑 `git commit` 才暴露。
现在它被固化成了 `test_hooks_e2e.py` 里的回归用例。

**教训**：门禁依赖什么输入，就必须放在能拿到那个输入的阶段。
不要用"测试通过"替代"流程对"。

## 门禁一览

| # | 触发条件 | 钩子 | 行为 | 放行办法 |
|---|---|---|---|---|
| 1 | 一次提交删除 ≥ 3 个跟踪文件 | pre-commit | 阻断，列出全部路径 | `GATE_ALLOW_BULK_DELETE=1` |
| 2 | 信息以 `注释：`/`格式：`/`工程：` 开头且改了 `.py` | commit-msg | 阻断，要求 AST 结构等价 | `GATE_SKIP_AST_EQUIV=1` |
| 2b | 信息以 `注释：` 开头且删除了任何行 | commit-msg | 阻断，列出删行数 | 改用 `格式：` 等前缀 |
| 3 | 改动触及 `frontend/src/`、`runtime.py`、`events.py`、`routers/sse.py` | pre-commit | 阻断，要求人工过目真机表现 | `GATE_REVIEWED=1` |
| 4 | 验收证据比本次改动旧 | pre-commit | **仅提醒**，不阻断 | 跑 `python tools/qa/run.py all` |
| 附加 | — | pre-commit | 可选跑 pytest | `GATE_RUN_TESTS=1` |

每道门禁的阻断信息里都写清了**为什么**（对应哪次真实事故）和**怎么放行**。
不要因为嫌烦就常驻逃生阀——那等于把门禁删掉，还留个假象。

```bash
# 正常提交（会经过门禁）
git commit -m "修复：SSE 断线残留读事务"

# 改动触及页面/流式，确认已亲眼看过真机表现
GATE_REVIEWED=1 git commit -m "前端：执行日志补实时轨迹"

# 确实要一次删掉多个模块
GATE_ALLOW_BULK_DELETE=1 git commit -m "重构：移除废弃的旧路由模块"
```

## 门禁自身也要测

```bash
python tools/gates/selftest.py
```

三层共 21 个用例：

| 测试 | 层 | 用例数 | 覆盖 |
|---|---|---|---|
| `test_ast_equiv.py` | 工具 | 3 | 只加注释 → 等价；改字面量 → 不等价；折行 → 等价 |
| `test_hooks.py` | 钩子（单元） | 10 | 两个钩子的每条阻断分支 + 每个逃生阀；commit-msg 用 `$1` 传参（贴近 git 真实调用） |
| `test_hooks_e2e.py` | 钩子（端到端） | 8 | 临时仓库里**真的执行 `git commit`**，验证提交被拦住 / 被放行 |

每个用例都断言**退出码 + 该分支专属文案**——只断言退出码会漏掉"拦对了但理由不对"。
所有测试用临时索引或临时仓库，**不碰工作区、不碰真实暂存区、不产生提交**。

端到端那层不能省：本轮就是它抓到了"门禁 2 读的是上一条提交信息"这个 bug。

## 相关文件

- `tools/gates/check_ast_equiv.py` — 结构等价校验（门禁 2 调用它）
- `tools/gates/test_*.py`、`tools/gates/selftest.py` — 门禁测试
- `AGENTS.md` — 规则本身，以及每条规则对应的事故
- `tools/qa/` — 验收套件与证据（门禁 4 关心的东西）
