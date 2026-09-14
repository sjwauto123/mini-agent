"""运行时各模块之间的数据契约（dataclass 定义）。

分工：模型层产出 ``ModelEvent``，运行时层消费它并产出 ``RunResult``；工具层通过
``ExecutionContext`` 拿到上下文、返回 ``ToolResult``。集中放在这里可以避免模块间循环依赖，
也让"一次运行会经过哪些数据结构"一目了然。
"""
from dataclasses import dataclass, field
from typing import Any, Literal

# 一次运行的终态集合。只有落到这些状态，前端才会停止等待流式推送、释放输入框。
RunStatus = Literal["completed", "failed", "cancelled", "limit_reached", "interrupted"]

@dataclass
class RunRequest:
    """提交一次运行所需的入参。

    ``request_key`` 用于幂等去重：同一个 key 重复提交只会真正执行一次，避免用户重复点击
    或在网络重试时产生两条回答。
    """
    session_id: str
    message: str
    request_key: str | None = None

@dataclass
class ToolResult:
    """工具执行结果。``ok=False`` 时错误细节放在 ``error``（含稳定错误码）。

    ``error`` 固定包含 ``code`` / ``message`` / ``outcome``（not_executed | failed | unknown）。
    可额外带 ``detail``：只随工具结果进入模型上下文、供模型修正参数用，前端不渲染 tool 消息，
    因此不会把内部报错暴露给用户。
    """
    ok: bool
    data: Any = None
    error: dict[str, Any] | None = None
    # 是否为模拟数据（如 mock 天气/搜索）。要求在回答里如实标注来源，不能冒充真实数据。
    mock: bool = False
    # 结果是否被截断；超长结果会被转存为资源，由 resource_read 按需读取。
    truncated: bool = False

@dataclass
class RunResult:
    """一次运行的最终产出，直接对应前端看到的状态、回答与执行摘要。"""
    run_id: str
    session_id: str
    status: RunStatus
    answer: str = ""
    error: dict[str, Any] | None = None
    # 本次运行做过的工具调用清单（含参数与结果），用于展示与排查。
    operations: list[dict[str, Any]] = field(default_factory=list)

@dataclass
class ExecutionContext:
    """执行工具时传给工具的上下文。

    ``db_connection`` 只在"本地写"类工具（如待办）中注入，使工具与写入结果落在同一个事务里，
    避免出现"工具改了但消息没落库"的中间态。
    """
    run_id: str
    session_id: str
    user_id: str = "local"
    result_token_budget: int | None = None
    db_connection: Any = None

@dataclass
class Final:
    """模型给出的最终回答（不再调用工具）。"""
    answer: str
    # 界面上"思考过程"展示的内容，由模型首行「思考：…」拆出。
    decision_summary: str = ""

@dataclass
class ToolCall:
    """模型决定调用某个工具。"""
    call_id: str
    name: str
    arguments: dict[str, Any]
    decision_summary: str = ""
    # 服务商思考模式返回的私有推理字段：只用于按协议回传给模型，不进入界面展示，也不作为决策依据。
    reasoning_content: str = ""

@dataclass
class Invalid:
    """模型响应不符合协议时的可恢复错误，运行时据此要求模型重答。"""
    code: str
    message: str

# 模型一轮响应的三种可能形态，运行时用 isinstance 分派处理。
ModelEvent = Final | ToolCall | Invalid
