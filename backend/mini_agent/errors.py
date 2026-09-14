"""错误码：把内部异常收敛成一组稳定的对外错误码。

为什么要单独一层：同一个失败要经过"运行时 → 接口层 → 前端"三道关。如果每道关各写一套
字符串判断，原因就会在传递中被改写——例如"会话引用的模型未配置"会被当成"会话不存在"，
"缺少密钥"会因为码里带了环境变量名而查不到文案。这里规定唯一的归一化入口与唯一的文案表，
三层都从这里取。

约定：``code`` 是稳定标识（只增不改，前端据此展示本地化文案）；``message`` 只是兜底文案。
"""

# 每个稳定错误码对应的用户可读文案。同一件事只在这里写一次。
ERROR_ANSWERS: dict[str, str] = {
    "cancelled": "已停止本次运行，已完成的工具操作仍然保留。",
    "context_too_large": "对话内容超过模型上下文限制，请缩短消息或新建会话后重试。",
    "input_prepare_failed": "消息准备失败，请重试。",
    "message_required": "请输入消息。",
    "model_api_key_missing": "模型 API 密钥未配置，请检查服务端环境变量。",
    "model_auth_failed": "模型服务拒绝了本次请求，请检查服务端 API 密钥与权限。",
    "model_call_limit": "本次运行已达到模型调用次数上限。",
    "model_context_budget_invalid": "模型上下文配置无效。",
    "model_not_configured": "模型未在服务端配置中定义，请检查模型配置。",
    "model_protocol_error": "模型返回了无法解析的响应，请重试。",
    "model_request_invalid": "模型服务拒绝了请求参数，请检查模型端点、模型名与上下文容量配置。",
    "model_unavailable": "模型服务暂时不可用，请检查网络连接和 API 配置后重试。",
    "request_key_conflict": "请求标识已被其他消息使用。",
    "resource_too_large": "资源内容过大。",
    "run_not_found": "运行记录不存在。",
    "run_timeout": "本次运行已达到时间限制，已完成的工具操作仍然保留。",
    "service_restarted": "服务重启导致本次运行中断。",
    "session_busy": "当前会话正在处理其他消息，请稍后再试。",
    "session_not_found": "会话不存在。",
    "timezone_invalid": "时区配置无效。",
}

# 可被认领的错误码集合。不在这里的标识一律退回异常类名，避免把内部细节（如密钥env名）当成码用。
KNOWN_CODES = frozenset(ERROR_ANSWERS)

# 提交运行时的错误码 → HTTP 状态：会话冲突用 409，请求内容问题用 400（默认），
# 服务端自身配置或依赖不可用用 503 —— 用户改不了，需要运维修配置后重试。
SUBMIT_HTTP_STATUS: dict[str, int] = {
    "session_busy": 409,
    "request_key_conflict": 409,
    "session_not_found": 404,
    "model_not_configured": 503,
    "model_api_key_missing": 503,
    "model_unavailable": 503,
    "model_context_budget_invalid": 500,
}


def error_code_of(exc: BaseException) -> str:
    """把异常归一化成稳定错误码。

    优先级：异常自带的 ``code``（领域异常）> 文本前缀（``"code: 细节"`` 的既有约定）>
    异常类名。认不出时退回类名而不是编一个码，日志里仍能看出原始异常类型。
    """
    attached = getattr(exc, "code", None)
    if isinstance(attached, str) and attached in KNOWN_CODES:
        return attached
    prefix = str(exc).split(":", 1)[0].strip()
    if prefix in KNOWN_CODES:
        return prefix
    if isinstance(exc, LookupError):
        # 领域里的 LookupError 专指"按标识查不到"，最常见的就是会话不存在。
        return "session_not_found"
    return type(exc).__name__


def answer_for(code: str, default: str) -> str:
    """取错误码对应的用户可读文案；未收录的码用调用方给的兜底文案。"""
    return ERROR_ANSWERS.get(code, default)


class AgentError(RuntimeError):
    """带稳定错误码的领域异常。

    有意继承 RuntimeError：运行时把"可预期的失败"统一按 RuntimeError 分派，
    新增错误码便不需要再改动分派逻辑。
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class ModelServiceError(AgentError):
    """模型服务返回的不可重试错误：鉴权失败、请求参数被拒、响应结构不合法。

    与可重试的传输层故障区分开——这类错误重试没有意义，必须带稳定错误码直接上报，
    否则 401 与 400 会变成同一种"未知失败"。
    """
