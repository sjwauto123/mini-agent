from dataclasses import dataclass, field
from typing import Any, Literal

RunStatus = Literal["completed", "failed", "cancelled", "limit_reached", "interrupted"]

@dataclass
class RunRequest:
    session_id: str
    message: str
    request_key: str | None = None

@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: dict[str, Any] | None = None
    mock: bool = False
    truncated: bool = False

@dataclass
class RunResult:
    run_id: str
    session_id: str
    status: RunStatus
    answer: str = ""
    error: dict[str, Any] | None = None
    operations: list[dict[str, Any]] = field(default_factory=list)

@dataclass
class ExecutionContext:
    run_id: str
    session_id: str
    user_id: str = "local"
    result_token_budget: int | None = None
    db_connection: Any = None

@dataclass
class Final:
    answer: str
    decision_summary: str = ""

@dataclass
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]
    decision_summary: str = ""
    # 服务商思考模式返回的私有推理字段：只用于按协议回传给模型，不进入界面展示，也不作为决策依据。
    reasoning_content: str = ""

@dataclass
class Invalid:
    code: str
    message: str

ModelEvent = Final | ToolCall | Invalid
