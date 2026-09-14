"""路由层共享件：错误体构造、运行记录脱敏、单例取用与请求体模型。

单独成模块的原因：``routers`` 下每个路由文件都要用同一份定义，若各自复制就会漂移。
``api.py`` 会把这些名字再导出一次，所以历史调用方（含测试）继续从 ``mini_agent.api``
导入也能拿到，拆分不波及它们。
"""
from typing import TYPE_CHECKING, Any

from fastapi import Request
from pydantic import BaseModel, Field

from ..errors import KNOWN_CODES, SUBMIT_HTTP_STATUS, answer_for

if TYPE_CHECKING:
    # 仅用于类型标注。``AppServices`` 定义在 api.py，而 api.py 会导入本包，
    # 运行时导入会成环，因此放进 TYPE_CHECKING 分支。
    from ..api import AppServices

# 终态集合：运行落到这些状态后，SSE 就可以收尾、前端可以释放输入框。
TERMINAL = {"completed", "failed", "cancelled", "limit_reached", "interrupted"}
# 运行记录里不对外暴露的服务端字段：哈希与幂等键只用于内部去重。
INTERNAL_RUN_FIELDS = frozenset({"input_hash", "request_key"})


def services(request: Request) -> "AppServices":
    """取出应用级单例集合；它在 lifespan 启动时挂到 ``app.state`` 上。"""
    return request.app.state.services


def error_detail(code: str) -> dict[str, str]:
    """构造统一的错误响应体。文案取自 errors 模块，本层不再维护第二份表。"""
    return {"code": code, "message": answer_for(code, "请求处理失败，请稍后重试。")}


def submission_status(code: str) -> int:
    """提交运行时：错误码 → HTTP 状态。

    未收录的码说明这是未预期的内部失败，按 500 上报——不能把服务端 bug 说成用户输入有问题；
    已收录但没特别约定状态的（缺消息、资源过大等）按请求内容问题处理，回 400。
    """
    status = SUBMIT_HTTP_STATUS.get(code)
    if status is not None:
        return status
    return 400 if code in KNOWN_CODES else 500


def public_run(run: dict[str, Any] | None) -> dict[str, Any] | None:
    """剔除服务端内部字段后再把运行记录交给客户端。"""
    if run is None:
        return None
    return {key: value for key, value in run.items() if key not in INTERNAL_RUN_FIELDS}


class SessionCreate(BaseModel):
    model_name: str
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=128)


class SessionUpdate(BaseModel):
    model_name: str


class RunCreate(BaseModel):
    # max_length 放宽到 10MB：超长消息由运行时转存为资源，而不是在这里被拒。
    message: str = Field(min_length=1, max_length=10_485_760)
    request_key: str | None = Field(default=None, max_length=128)


class ResourceCreate(BaseModel):
    content: str
    kind: str = Field(default="text", pattern="^(text|json)$")
