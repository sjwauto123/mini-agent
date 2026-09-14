"""执行日志契约：后端埋点的每个事件类型，前端都必须有对应的文案分支。

为什么要单独测这个：``tracePresentation`` 的 switch 漏一个分支不会报任何错，只会让界面
显示英文 ``event_type`` 原文加一句"Agent 记录了一条运行事件。"——后端有埋点、前端没翻译。
而且只有真跑到那条分支才看得见，``model.repair`` 这类分支平时几乎不触发（要先让模型返回
非法协议），所以漏了也很难被人当场发现。这里把"新增事件要记得补前端"从纪律变成测试。
"""
import ast
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend" / "mini_agent"
APP = Path(__file__).resolve().parents[1] / "frontend" / "src" / "App.tsx"

# 事件名形如 "model.retry"：小写字母与下划线组成、中间恰好一个点。
_EVENT_NAME = re.compile(r"^[a-z][a-z_]*\.[a-z_]+$")
# 前端分支形如 case 'model.retry':
_CASE = re.compile(r"case '([a-z][a-z_]*\.[a-z_]+)'")


def _callee_name(func: ast.expr) -> str | None:
    """取出被调用者的名字：``add_trace(...)`` 与 ``self.store.add_trace(...)`` 都算 ``add_trace``。"""
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _emitted_events() -> set[str]:
    """从后端源码里取出所有埋点的事件名。

    用 AST 而不是正则扫描源码：正则只能看单行，一旦调用被折成多行
    （``add_trace(`` 与事件名不在同一行）就会静默漏事件——这恰恰是契约测试最怕的失效方式。

    两种下发形态都要覆盖：
    - 直接写字面量：``add_trace(run_id, "tool.started", {...})``；
    - 先算再传：``event = "model.retry" if 还有余量 else "model.failed"``，随后 ``add_trace(run_id, event, ...)``。
    只覆盖第一种会漏掉 ``model.retry``，而它恰好是前端已有分支、最容易被误判为"已对齐"的一个。
    """
    names: set[str] = set()
    for path in sorted(BACKEND.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _callee_name(node.func) == "add_trace" and len(node.args) >= 2:
                second = node.args[1]
                if isinstance(second, ast.Constant) and isinstance(second.value, str):
                    names.add(second.value)
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.IfExp):
                if not any(isinstance(t, ast.Name) and t.id == "event" for t in node.targets):
                    continue
                for side in (node.value.body, node.value.orelse):
                    if isinstance(side, ast.Constant) and isinstance(side.value, str):
                        names.add(side.value)
    return {name for name in names if _EVENT_NAME.match(name)}


def _translated_events() -> set[str]:
    """取出前端 tracePresentation 已经翻译过的事件名（只看这一个函数，避免误收别处的字符串）。"""
    source = APP.read_text(encoding="utf-8")
    start = source.index("const tracePresentation")
    end = source.index("// —— 3.", start)
    return set(_CASE.findall(source[start:end]))


def test_every_trace_event_has_a_frontend_branch():
    emitted = _emitted_events()
    # 兜底：正则一旦失效会静默返回空集合，断言就变成永远通过的空壳。
    assert len(emitted) >= 15, f"事件提取疑似失效，只取到 {len(emitted)} 个：{sorted(emitted)}"
    missing = emitted - _translated_events()
    assert not missing, f"这些事件后端有埋点、前端却没翻译，界面会漏出英文事件名：{sorted(missing)}"
