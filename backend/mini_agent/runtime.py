"""Agent 运行时：一次运行的完整执行循环。

主循环（``_execute``）反复做四件事，直到拿到最终回答：

1. 检查是否被请求取消；
2. 必要时压缩上下文（``_maybe_summarize``）；
3. 组装消息并调用模型（``_model_call``，含重试与协作式取消）；
4. 按模型响应分派：终答（流式落库）/ 工具调用（``_tool_call``）/ 协议非法（要求重答）。

三条贯穿全文件的约定：
- **一切可恢复的问题都先修再报错**：格式错误重答、空正文换最小上下文、传输错误退避重试；
- **取消是协作式的**：长任务用 100ms 轮询检查取消标志，而不是强杀；
- **状态先落库再返回**：任何出口都会写消息 + finish_run + trace，前端只依赖数据库状态。
"""
import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

import httpx

from .context import ContextManager, estimate_tokens
from .contracts import ExecutionContext, Final, Invalid, RunResult, ToolResult
from .errors import ModelServiceError, answer_for, error_code_of
from .events import RunEventBus
from .model import ModelClient, parse_response
from .storage import ResourceStore, Store
from .tools import ToolRegistry

# 运行期的内部异常必须留下日志：trace 只记错误码，细节只有这里能看到。
logger = logging.getLogger(__name__)


def _backoff_delay(attempt: int) -> float:
    """第 attempt 次失败后的等待时长：0.5s → 1s → 2s …，上限 4s。

    用指数计算而不是查表：查表会让"表长"隐式地限制可重试次数，
    一旦调用方传入更大的尝试次数就会下标越界。
    """
    return min(.5 * 2 ** attempt, 4)


# 语言约束单独作为最后一条系统消息追加。
# 只写在 SYSTEM_PROMPT（消息列表开头）与协议提示的句尾都不够稳：实测第 1 轮仍整轮返回
# 英文推理（1000+ 字，会原样显示在界面的思考面板里）。把要求独立成"紧贴模型输出的最后
# 一条消息"后遵从率明显提高——位置比措辞更关键，所以不与协议提示合并，也不放开头。
LANGUAGE_HINT = "推理过程与回答一律使用中文，不要使用英文推理。"


def _model_output_head(raw: dict[str, Any], limit: int = 160) -> dict[str, Any]:
    """取出模型正文开头、结束原因与用量，仅用于在 trace 里定位协议错误原因。

    兼容两种响应形态：原生协议的 ``choices[0].message`` 与 JSON 模式被摊平后的结构。
    """
    try:
        has_choices = isinstance(raw.get("choices"), list) and raw["choices"]
        choice = raw["choices"][0] if has_choices else raw
        message = choice.get("message", choice)
        finish = choice.get("finish_reason")
        content = message.get("content")
        reasoning = message.get("reasoning_content") or raw.get("reasoning_content") or ""
        usage = raw.get("usage")
    except (KeyError, IndexError, TypeError, AttributeError):
        return {}
    text = "" if content is None else str(content)
    # content_len 与 finish_reason 一起看，就能区分"被截断"和"模型真的只回了空白"。
    return {
        "finish_reason": finish,
        "content_len": len(text),
        "content_head": text[:limit],
        "reasoning_len": len(str(reasoning)),
        "usage": usage if isinstance(usage, dict) else None,
    }


def _message_of(raw: dict[str, Any]) -> dict[str, Any]:
    """取响应里的 message：原生协议在 ``choices[0].message``，JSON 模式则是摊平后的结构本身。"""
    if isinstance(raw.get("choices"), list) and raw["choices"]:
        return raw["choices"][0].get("message") or {}
    return raw


def _usage_metrics(raw: dict[str, Any], first_token_at: float | None, started: float) -> dict[str, Any]:
    """从响应里取出 token 用量与首字延迟，用于 ``model.finished`` 的轨迹。

    字段名跟随 OpenTelemetry 的现行命名（``input_tokens`` / ``output_tokens``）——
    ``prompt_tokens`` / ``completion_tokens`` 是被改名的那一代。
    上游没返回 usage 时**不写这两个字段**，而不是填 0：在成本统计里"没测到"与"测到是 0"是两回事。
    非流式调用没有"首字"可言，因此不填 ``ttft_ms``。
    """
    metrics: dict[str, Any] = {}
    usage = raw.get("usage") if isinstance(raw, dict) else None
    if isinstance(usage, dict):
        if isinstance(usage.get("prompt_tokens"), int):
            metrics["input_tokens"] = usage["prompt_tokens"]
        if isinstance(usage.get("completion_tokens"), int):
            metrics["output_tokens"] = usage["completion_tokens"]
    if first_token_at is not None:
        metrics["ttft_ms"] = round((first_token_at - started) * 1000, 2)
    return metrics


# 思考过程改由服务商的 reasoning_content 承载（界面单独呈现），因此不再要求模型把「思考：…」
# 写进正文首行——那会把推理混进用户可见的回答，也让流式输出平白多剥一层。
# 这里只保留真正必要的约束：回答与工具调用二者择一，不要互相夹带。
NATIVE_PROTOCOL_HINT = (
    "请使用标准的工具调用方式工作。需要调用工具时，只发出工具调用，不要在正文里夹带给用户的回答；"
    "直接回答时，给出完整、结构清晰的中文回答，不要复述用户问题，不要编造没有发生的动作。"
    "推理过程与回答一律使用中文。"
)


def _protocol_hint(mode: str, tools: list[dict[str, Any]]) -> str:
    """按模型协议给出这一步的格式约定。"""
    if mode == "json":
        return (
            "请严格返回一个 JSON 对象，字段固定为 decision_summary、action、answer、tool。"
            "decision_summary 必填：用一句简短中文说明这一步的判断依据，例如“需要先查天气，再判断是否创建待办”；"
            "不要复述用户问题，不要展开推理链，不要编造没有发生的动作，没有依据时填空字符串。"
            "直接回答时使用 action=final 并填写非空 answer，同时把工具类字段留空；"
            "调用工具时使用 action=tool_call，tool 为包含 name 与 arguments 的对象，且每次只填一个工具。"
            "可用工具如下：" + json.dumps(tools, ensure_ascii=False)
        )
    return NATIVE_PROTOCOL_HINT


def _repair_instruction(code: str, mode: str) -> str:
    """协议修复提示：JSON 模式下把格式要求说到最死，降低再次返回空白或散文的概率。"""
    if mode == "json":
        return (
            f"你上一次响应无效（{code}）。请只返回一个 JSON 对象：第一个字符必须是 {{，最后一个字符必须是 }}，"
            "字段固定为 decision_summary、action、answer、tool。"
            "不要输出空格、换行、Markdown 代码块或任何解释文字。"
        )
    return f"你上一次响应无效（{code}）。请重新返回一个有效的最终回答，或一个有效的工具调用。不要输出其他格式。"


class AgentRuntime:
    def __init__(
        self,
        store: Store,
        registry: ToolRegistry,
        resources: ResourceStore,
        model_factory: Callable[[str], tuple[ModelClient, str, int, int]],
        *,
        max_model_calls: int = 12,
        max_repairs: int = 2,
        max_summary_calls: int = 2,
        run_timeout: float = 180,
        safety_margin: int = 1024,
        soft_context_ratio: float = .70,
        hard_context_ratio: float = .85,
        target_context_ratio: float = .55,
        events: RunEventBus | None = None
    ) -> None:
        self.store, self.registry, self.resources = store, registry, resources
        # 事件总线：把流式增量实时投给该运行的 SSE 订阅者。默认自建一个，
        # 便于单独使用运行时（例如测试）时不依赖接口层装配。
        self.events = events or RunEventBus()
        # model_factory 按模型名返回 (客户端, 协议, 上下文窗口, 输出预留)，便于同一进程服务多个模型。
        self.model_factory = model_factory
        self.max_model_calls, self.max_repairs, self.max_summary_calls, self.run_timeout = (
            max_model_calls, max_repairs, max_summary_calls, run_timeout
        )
        self.safety_margin = safety_margin
        self.soft_context_ratio, self.hard_context_ratio, self.target_context_ratio = (
            soft_context_ratio, hard_context_ratio, target_context_ratio
        )
        # 串行化"创建运行"这一步：并发提交时靠它配合 start_run 的幂等键保证只落一条 run。
        self._submission_lock = asyncio.Lock()

    async def submit(
        self,
        session_id: str,
        message: str,
        request_key: str | None = None
    ) -> tuple[dict[str, Any], bool]:
        """受理一条用户消息：创建运行并把用户消息落库。返回 (run, 是否新建)。"""
        if not message or not message.strip():
            raise ValueError("message_required")
        session = await self.store.get_session(session_id)
        if not session:
            raise LookupError("session_not_found")
        async with self._submission_lock:
            run, created = await self.store.start_run(session_id, message, request_key)
        if created:
            visible_message = message
            try:
                _, _, context_window, output_reserve = self.model_factory(run["model_name"])
                input_budget = context_window - output_reserve - self.safety_margin
                if input_budget <= 0:
                    raise ValueError("model_context_budget_invalid")
                # 单条消息就快撑满上下文时，把它转成资源、只把索引提示写进上下文，
                # 让模型按需分页读取，而不是直接让这次请求超限失败。
                if estimate_tokens(message) >= input_budget * self.soft_context_ratio:
                    resource_id = await self.resources.save(session_id, message, "text")
                    visible_message = f"完整消息已保存为资源 {resource_id}。回答前请按需使用资源查找或资源读取工具查看。"
                await self.store.add_message(session_id, run["id"], "user", {"content": visible_message})
                # 会话第一条消息顺带当作标题；从这里取的是原始 message，而不是被替换后的资源提示。
                if await self.store.count_messages(session_id) == 1:
                    await self.store.set_session_title(session_id, message)
            except Exception as exc:
                # 准备阶段失败也要把 run 落到终态，否则会话会被永久判定为"忙碌"。
                # 错误码取真实原因（缺密钥、模型未配置、预算非法…），不要统一改写成
                # input_prepare_failed —— 那会让落库记录和上层提示都失去线索。
                code = error_code_of(exc)
                logger.warning("run %s failed while preparing message: %s", run["id"], code)
                await self.store.finish_run(run["id"], "failed", answer_for(code, "消息准备失败，请重试。"), {"code": code})
                raise
        return run, created

    async def _model_call(
        self,
        run_id: str,
        model: ModelClient,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: str,
        *,
        max_attempts: int = 3,
        phase: str = "response",
        iteration: int = 0,
        sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    ) -> tuple[dict[str, Any], int | None]:
        """调用模型，内置重试与协作式取消。返回 (原始响应, 本轮 model.started 的事件 id)。

        传 ``sink`` 时走流式：增量到达当下就交给它（落库 + 广播），而不是等整段生成完再回放。
        不传则是一次性调用——压缩摘要走的就是这条路，摘要不需要边生成边给用户看。
        两条路共用同一套重试与取消语义，避免"流式与非流式的错误处理各写一套"。

        返回事件 id 是为了让调用方把后续事件挂到"真正产生它们的那次模型请求"下面：
        工具调用是这一轮模型决定的，若平铺在运行下就看不出它属于第几轮。
        """
        last_error: Exception | None = None
        span_id: int | None = None
        for attempt in range(max_attempts):
            current = await self.store.get_run(run_id)
            # 调用次数上限要在这里再查一次：压缩摘要也走同一个计数，超限就整体停下。
            if not current or current["model_calls"] >= self.max_model_calls:
                raise RuntimeError("model_call_limit")
            await self.store.increment_model_calls(run_id)
            started = time.perf_counter()
            # 首个内容增量到达的时刻。TTFT 是流式下唯一能区分"服务慢"与"答案长"的指标：
            # 只记总耗时时，一个长回答会让每次调用都显得一样慢。
            first_token_at: float | None = None

            def metrics(**extra: Any) -> dict[str, Any]:
                """本轮轨迹事件的公共字段，额外字段由调用处补齐。"""
                base: dict[str, Any] = {
                    "attempt": attempt + 1,
                    "iteration": iteration,
                    "phase": phase,
                    "parent_id": span_id,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2)
                }
                base.update(extra)
                return base

            span_id = await self.store.add_trace(run_id, "model.started", {
                "attempt": attempt + 1,
                "iteration": iteration,
                "phase": phase,
                "tool_choice": tool_choice,
                "stream": sink is not None
            })
            try:
                if sink is None:
                    raw = await self._await_task(run_id, asyncio.create_task(model.complete(
                        messages,
                        tools,
                        tool_choice=tool_choice
                    )))
                else:
                    # 每次尝试都从零累计：上一次尝试可能已经流出去一部分，必须先让 sink 撤回它，
                    # 否则重试成功后内容会叠加成"半句 + 完整句"。
                    await sink({"type": "reset"})

                    async def timed(chunk: dict[str, Any]) -> None:
                        """记录首个增量到达的时刻，其余原样转交。"""
                        nonlocal first_token_at
                        if first_token_at is None and chunk.get("type") in {
                            "content",
                            "reasoning"
                        } and chunk.get("text"):
                            first_token_at = time.perf_counter()
                        await sink(chunk)

                    raw = await self._consume_stream(run_id, model, messages, tools, tool_choice, timed)
                await self.store.add_trace(
                    run_id,
                    "model.finished",
                    metrics(**_usage_metrics(raw, first_token_at, started))
                )
                return raw, span_id
            except httpx.TransportError as exc:
                # 覆盖 ConnectError、ReadError、RemoteProtocolError 等全部传输层故障；
                # 只捕获 TimeoutException/NetworkError 会漏掉协议层错误，导致直接抛出未分类异常。
                last_error = exc
                event = "model.retry" if attempt + 1 < max_attempts else "model.failed"
                await self.store.add_trace(run_id, event, metrics(code=type(exc).__name__, detail=str(exc)[:300]))
                if attempt + 1 < max_attempts:
                    # 退避 .5s → 1s → 2s…，避免服务端抖动时连环重试。
                    delay = _backoff_delay(attempt)
                    self._notify_retry(run_id, attempt + 2, delay, type(exc).__name__)
                    await asyncio.sleep(delay)
            except httpx.HTTPStatusError as exc:
                # 只对"可能自愈"的状态码重试；4xx 里的参数/鉴权错误重试没有意义，直接抛出。
                if exc.response.status_code not in {429, 500, 502, 503, 504}:
                    raise
                last_error = exc
                event = "model.retry" if attempt + 1 < max_attempts else "model.failed"
                await self.store.add_trace(run_id, event, metrics(
                    code=f"http_{exc.response.status_code}",
                    detail=str(exc)[:300]
                ))
                if attempt + 1 < max_attempts:
                    delay = _backoff_delay(attempt)
                    self._notify_retry(run_id, attempt + 2, delay, f"http_{exc.response.status_code}")
                    await asyncio.sleep(delay)
            except ModelServiceError as exc:
                # 鉴权、请求参数、响应结构类失败：重试没有意义，带稳定错误码直接上报。
                # 但仍要落一条 trace，否则会出现"运行明确失败、日志却查不到原因"。
                await self.store.add_trace(run_id, "model.failed", metrics(code=exc.code, detail=exc.detail[:300]))
                raise
        raise RuntimeError("model_unavailable") from last_error

    def _notify_retry(self, run_id: str, next_attempt: int, delay: float, reason: str) -> None:
        """把"正在重试"推给前端。

        上游长时间不响应时（实测有 20.9 秒才抛 RemoteProtocolError 的情况），数据库状态一直
        没有变化，快照也就一直推不出内容，界面看上去像卡死。这条提示与运行结果无关，
        只负责解释这段沉默；总线在没有订阅者时是空操作，所以无需判断连接状态。
        """
        self.events.publish(run_id, {
            "type": "notice",
            "code": "model_retry",
            "attempt": next_attempt,
            "delay_ms": round(delay * 1000),
            "reason": reason
        })

    async def _await_task(self, run_id: str, task: "asyncio.Task[Any]") -> Any:
        """等待任务完成，期间按 100ms 轮询取消标志 —— 协作式取消的落点。"""
        while not task.done():
            await asyncio.wait({task}, timeout=.1)
            if await self.store.is_cancel_requested(run_id):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise RuntimeError("cancelled")
        return await task

    async def _consume_stream(
        self,
        run_id: str,
        model: ModelClient,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: str,
        sink: Callable[[dict[str, Any]], Awaitable[None]]
    ) -> dict[str, Any]:
        """消费流式响应：每个增量立刻交给 sink，返回拼装好的完整响应。

        消费放在独立任务里、经队列与主循环衔接，是为了在"模型长时间不吐字"时仍能响应停止：
        若直接 `async for` 迭代，协程会一直挂在读取上，取消检查根本没有执行的机会。
        """
        streamer = getattr(model, "stream", None)
        if streamer is None:
            # 客户端不提供 stream（测试用的假实现，或只实现 complete 的适配器）：
            # 退化成一次普通调用，再把整段内容当成一次增量补发。语义与流式路径完全一致，只是看不到逐字效果。
            raw = await self._await_task(run_id, asyncio.create_task(model.complete(
                messages,
                tools,
                tool_choice=tool_choice
            )))
            message = _message_of(raw)
            reasoning = message.get("reasoning_content") or ""
            if reasoning:
                await sink({"type": "reasoning", "text": str(reasoning)})
            content = message.get("content")
            if isinstance(content, str) and content:
                await sink({"type": "content", "text": content})
            return raw
        pending: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def pump() -> None:
            try:
                async for chunk in streamer(messages, tools, tool_choice=tool_choice):
                    await pending.put(chunk)
            finally:
                # 哨兵：无论正常结束还是中途抛错，都要让消费循环退出；
                # pump 内部的异常会在下方 `await task` 处重新抛出，交给调用方按原有策略重试。
                await pending.put(None)

        task = asyncio.create_task(pump())
        raw: dict[str, Any] | None = None
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(pending.get(), timeout=.1)
                except asyncio.TimeoutError:
                    if await self.store.is_cancel_requested(run_id):
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        raise RuntimeError("cancelled")
                    continue
                if chunk is None:
                    break
                if chunk.get("type") == "done":
                    raw = chunk.get("raw")
                    continue
                await sink(chunk)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await task
        if not isinstance(raw, dict):
            # 上游提前断流（既没有 done 也没有抛错）也要当成可重试的传输故障，而不是静默返回半个响应。
            raise httpx.ReadError("流式响应意外结束，未收到结束标记")
        return raw

    async def _tool_call(self, run_id: str, name: str, args: dict[str, Any], context: ExecutionContext) -> ToolResult:
        """执行工具（同样支持取消）。"""
        task = asyncio.create_task(self.registry.execute(name, args, context))
        while not task.done():
            await asyncio.wait({task}, timeout=.1)
            # 本地写类工具（todo）不响应取消：它已经开了事务，中途取消会破坏写入一致性。
            if await self.store.is_cancel_requested(run_id) and name != "todo":
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                return ToolResult(False, error={"code": "cancelled", "message": "工具等待已取消。", "outcome": "unknown"})
        return await task

    async def _discard_partial(self, session_id: str, assistant_seq: int | None, run_id: str) -> None:
        """撤回流式输出到一半的助手消息。

        流式是"先写后判"：正文边收边落库，这一轮是否有效要等结束才知道。任何"这一轮作废"
        的出口（协议非法、重试、被取消、运行失败）都必须撤回已写出的内容，否则历史里会留下
        一条看着像完整回答的残句，而且它会被下一轮模型当成已完成的结果读进上下文。
        """
        if assistant_seq is None:
            return
        await self.store.delete_message(session_id, assistant_seq)
        self.events.publish(run_id, {"type": "discard", "seq": assistant_seq})

    async def _maybe_summarize(
        self,
        run_id: str,
        session_id: str,
        manager: ContextManager,
        model: ModelClient,
        mode: str,
        tools: list[dict[str, Any]],
        summary_calls: int,
        iteration: int
    ) -> int:
        """上下文超过软水位时，把较早的轮次压缩成摘要。返回累计的压缩调用次数。"""
        bundle = await manager.prepare(session_id, tools)
        if not bundle.over_soft or summary_calls >= self.max_summary_calls:
            return summary_calls
        candidate = await manager.compression_candidate(session_id)
        if not candidate:
            return summary_calls
        rows, covered = candidate
        previous = await self.store.latest_summary(session_id)
        summary_messages = manager.summary_messages(previous["content"] if previous else None, rows, mode == "json")
        # 待摘要内容自身也可能超预算：不断减半，直到装得下为止（至少留一条）。
        while len(rows) > 1 and estimate_tokens(summary_messages) > manager.input_budget:
            rows = rows[: len(rows) // 2]
            covered = int(rows[-1]["seq"])
            summary_messages = manager.summary_messages(previous["content"] if previous else None, rows, mode == "json")
        # 记录调用前的计数，用于把"摘要消耗的模型调用"也算进总预算。
        before = await self.store.get_run(run_id)
        try:
            raw, _summary_span = await self._model_call(
                run_id,
                model,
                summary_messages,
                [],
                "none",
                max_attempts=self.max_summary_calls - summary_calls,
                phase="summary",
                iteration=iteration,
            )
        except Exception as exc:
            after = await self.store.get_run(run_id)
            summary_calls += max(0, int(after["model_calls"]) - int(before["model_calls"])) if before and after else 0
            code = error_code_of(exc)
            # 取消、调用超限，以及鉴权/参数这类"整个运行都不可能成功"的失败必须向上抛：
            # 否则会带着错误配置把剩下的预算撞在同一堵墙上。其余失败只降级为"这次没压成"。
            if code in {"cancelled", "model_call_limit", "model_auth_failed", "model_request_invalid"}:
                raise
            logger.warning("run %s context compaction failed: %s", run_id, code)
            await self.store.add_trace(
                run_id,
                "context.compaction_failed",
                {"code": code, "summary_calls": summary_calls}
            )
            return summary_calls
        after = await self.store.get_run(run_id)
        summary_calls += max(0, int(after["model_calls"]) - int(before["model_calls"])) if before and after else 1
        event = parse_response(raw, mode)
        # 摘要过长说明模型没按要求收敛，宁可不落库（保留原历史）也不要塞进一份劣质摘要。
        if isinstance(event, Final) and len(event.answer.encode("utf-8")) <= 4096:
            await self.store.save_summary(session_id, covered, event.answer)
            await self.store.add_trace(run_id, "context.compacted", {
                "covered_through_seq": covered,
                "summary_calls": summary_calls
            })
        else:
            await self.store.add_trace(run_id, "context.compaction_failed", {
                "code": getattr(event, "code", "invalid_summary"),
                "summary_calls": summary_calls
            })
        return summary_calls

    async def execute(self, run_id: str) -> RunResult:
        """带总超时的执行入口：无论走到哪个出口，都要留下终态与回答。"""
        try:
            return await asyncio.wait_for(self._execute(run_id), timeout=self.run_timeout)
        except asyncio.TimeoutError:
            answer = answer_for("run_timeout", "本次运行已达到时间限制，已完成的工具操作仍然保留。")
            run = await self.store.get_run(run_id)
            if run:
                # 已经超时了，这里的收尾不能再被打断，用 shield 保护落库动作。
                await asyncio.shield(self.store.add_message(
                    run["session_id"],
                    run_id,
                    "assistant",
                    {"content": answer, "incomplete": True}
                ))
                await asyncio.shield(self.store.finish_run(run_id, "limit_reached", answer, {"code": "run_timeout"}))
                await asyncio.shield(self.store.add_trace(
                    run_id,
                    "run.finished",
                    {"status": "limit_reached", "code": "run_timeout"}
                ))
            return RunResult(run_id, run["session_id"] if run else "", "limit_reached", answer, {"code": "run_timeout"})
        except Exception as exc:
            # 兜底：把内部异常映射成用户可读的提示 + 稳定错误码，避免把原始堆栈暴露到界面。
            run = await self.store.get_run(run_id)
            session_id = run["session_id"] if run else ""
            code = error_code_of(exc)
            answer = answer_for(code, "Agent 未能启动或完成本次回答，请重试。")
            logger.warning("run %s ended before the loop completed: %s", run_id, code)
            if run:
                await self.store.finish_run(run_id, "failed", answer, {"code": code})
                await self.store.add_trace(run_id, "run.finished", {"status": "failed", "code": code})
            return RunResult(run_id, session_id, "failed", answer, {"code": code})

    async def _execute(self, run_id: str) -> RunResult:
        """四步执行循环本体。"""
        run = await self.store.get_run(run_id)
        if not run:
            raise LookupError("run_not_found")
        session_id = run["session_id"]
        session = await self.store.get_session(session_id)
        if not session:
            raise LookupError("session_not_found")
        # 模型在创建 run 时已快照，这里取到的就是本次运行固定使用的那个。
        model, mode, context_window, output_reserve = self.model_factory(run["model_name"])
        manager = ContextManager(
            self.store,
            context_window,
            output_reserve,
            self.safety_margin,
            soft_ratio=self.soft_context_ratio,
            hard_ratio=self.hard_context_ratio
        )
        tools = self.registry.definitions()
        repairs = summary_calls = 0
        repair_prompt: str | None = None
        # 上下文退化（长上下文下模型只返回空白正文）时，改用「只保留本轮消息」的最小上下文重试。
        minimal_context = False
        operations: list[dict[str, Any]] = []
        iteration = 0
        # 本轮流式输出的助手消息序号。定义在循环外，这样即使第一轮在流式开始前就失败，
        # 下面的失败出口也能安全地读它（否则会是未绑定变量）。
        assistant_seq: int | None = None
        await self.store.add_trace(run_id, "run.started", {})
        try:
            while True:
                iteration += 1
                # 第 1 步：取消检查点。
                if await self.store.is_cancel_requested(run_id):
                    answer = "已停止本次运行，已完成的工具操作仍然保留。"
                    await self.store.add_message(session_id, run_id, "assistant", {"content": answer})
                    await self.store.finish_run(run_id, "cancelled", answer)
                    await self.store.add_trace(run_id, "run.finished", {"status": "cancelled"})
                    return RunResult(run_id, session_id, "cancelled", answer, operations=operations)
                # 第 2 步：按需压缩上下文。
                summary_calls = await self._maybe_summarize(
                    run_id,
                    session_id,
                    manager,
                    model,
                    mode,
                    tools,
                    summary_calls,
                    iteration
                )
                recovery_run = run_id if minimal_context else None
                bundle = await manager.prepare(session_id, tools, repair_prompt, only_run_id=recovery_run)
                if bundle.over_hard:
                    # 到硬水位就必须裁剪：按轮次丢弃较早对话，回落到 target。
                    bundle = await manager.prepare(
                        session_id,
                        tools,
                        repair_prompt,
                        target_ratio=self.target_context_ratio,
                        only_run_id=recovery_run
                    )
                    if bundle.memory_incomplete:
                        # 有尚未摘要的历史被临时省略：记进 trace，让"记忆缺失"可查，
                        # 而不是只体现在模型后续回答的行为上。
                        await self.store.add_trace(run_id, "context.trimmed", {
                            "iteration": iteration,
                            "estimated_tokens": bundle.estimated_tokens,
                            "input_budget": bundle.input_budget
                        })
                if bundle.estimated_tokens > bundle.input_budget:
                    # 裁完仍装不下（例如单条工具结果过大），只能明确报错，不要发出必然被拒的请求。
                    raise RuntimeError("context_too_large")
                run_now = await self.store.get_run(run_id)
                # 剩最后一次调用机会时禁掉工具：必须收敛到一个回答，避免"调用上限"直接失败收场。
                tool_choice = "none" if run_now and run_now["model_calls"] >= self.max_model_calls - 1 else "auto"
                # 协议约定放在消息末尾：紧邻生成位置，模型遵从率明显高于混在开头 system 里。
                bundle.messages.append({"role": "system", "content": _protocol_hint(mode, tools)})
                # 语言约束再单独追加一条，成为模型的最后一条输入：只写在开头 SYSTEM_PROMPT
                # 或协议提示句尾时，实测第 1 轮仍会整轮用英文推理。
                bundle.messages.append({"role": "system", "content": LANGUAGE_HINT})
                # 第 3 步：流式调用模型。增量在到达当下就落库并广播，前端因此是逐字生长，
                # 而不是像过去那样"等整段生成完，再按固定步长把完整答案回放一遍"。
                content = thinking = ""
                assistant_seq = None
                last_flush = 0.0

                async def sink(chunk: dict[str, Any]) -> None:
                    """把一个增量并入本轮的助手消息：广播每一片，落库按节流。"""
                    nonlocal assistant_seq, content, thinking, last_flush
                    kind = chunk.get("type")
                    if kind == "reset":
                        # 作废本轮已流出的内容（重试前调用）：撤回消息并清空累计。
                        content = thinking = ""
                        if assistant_seq is not None:
                            await self.store.delete_message(session_id, assistant_seq)
                            self.events.publish(run_id, {"type": "discard", "seq": assistant_seq})
                            assistant_seq = None
                        return
                    if kind == "reasoning":
                        thinking += chunk["text"]
                    elif kind == "content":
                        content += chunk["text"]
                    else:
                        return
                    if assistant_seq is None:
                        # 消息在第一个增量到达时才创建：纯工具轮（既无思考也无正文）不会留下空壳消息。
                        # 先打上 incomplete 标记：此刻内容才流了一半，进程若在这里中断，
                        # 这条消息不能被后续上下文当成"已完成的回答"。
                        assistant_seq = await self.store.add_message(
                            session_id,
                            run_id,
                            "assistant",
                            {"incomplete": True}
                        )
                    now = time.perf_counter()
                    # 增量可能每秒几十条，逐片写库毫无必要：广播照发（内存操作，界面实时），
                    # 落库节流到 150ms 一次；结束前再补一次权威写入，数据库里一定是完整内容。
                    if now - last_flush >= .15:
                        last_flush = now
                        if not await self.store.update_message_fields(
                            session_id,
                            assistant_seq,
                            {"content": content, "thinking": thinking}
                        ):
                            # 消息在此刻被删掉（例如并发撤销）时不静默继续，留一条可查的线索。
                            logger.warning(
                                "run %s: streamed message seq=%s disappeared during flush",
                                run_id,
                                assistant_seq
                            )
                    self.events.publish(run_id, {
                        "type": "delta",
                        "seq": assistant_seq,
                        "content": content,
                        "thinking": thinking
                    })

                raw, span_id = await self._model_call(
                    run_id,
                    model,
                    bundle.messages,
                    tools,
                    tool_choice,
                    iteration=iteration,
                    sink=sink
                )
                event = parse_response(raw, mode)
                if isinstance(event, Final):
                    # 第 4 步之一：终答。正文已经边收边写过了，这里用解析结果做一次权威覆盖——
                    # 模型若仍写了首行决策说明，会被这一写剥掉，落库正文与协议解析结果始终一致。
                    if assistant_seq is None:
                        # 一个字都没流出来就直接终答（例如推理没有走 reasoning_content）：补一条消息，界面不能停在半空。
                        assistant_seq = await self.store.add_message(
                            session_id,
                            run_id,
                            "assistant",
                            {"incomplete": True}
                        )
                    final_thinking = thinking or event.decision_summary
                    # 落定：清掉 incomplete——这条消息已经是一个确定的回答。
                    # update_message_fields 会丢弃空值字段，因此 incomplete=False 等于把标记摘掉。
                    if not await self.store.update_message_fields(session_id, assistant_seq, {
                        "content": event.answer,
                        "thinking": final_thinking,
                        "incomplete": False
                    }):
                        # 权威写入失败：内存里的回答仍然会写进 runs.answer，但消息表是残缺的，必须留痕。
                        logger.warning(
                            "run %s: failed to persist final answer to message seq=%s",
                            run_id,
                            assistant_seq
                        )
                        await self.store.add_trace(
                            run_id,
                            "message.write_failed",
                            {"seq": assistant_seq, "stage": "final"}
                        )
                    self.events.publish(run_id, {
                        "type": "delta",
                        "seq": assistant_seq,
                        "content": event.answer,
                        "thinking": final_thinking or ""
                    })
                    await self.store.add_trace(run_id, "assistant.delta", {
                        "complete": True,
                        "chars": len(event.answer),
                        "iteration": iteration
                    })
                    await self.store.finish_run(run_id, "completed", event.answer)
                    await self.store.add_trace(run_id, "run.finished", {"status": "completed"})
                    return RunResult(run_id, session_id, "completed", event.answer, operations=operations)
                if isinstance(event, Invalid):
                    # 第 4 步之二：协议非法。这一轮整轮作废——已经流出去的部分必须撤回，
                    # 否则界面上会留下一条只说了一半的幽灵回答。
                    await self._discard_partial(session_id, assistant_seq, run_id)
                    assistant_seq = None
                    repairs += 1
                    await self.store.add_trace(run_id, "model.invalid", {
                        "code": event.code,
                        "reason": event.message[:200],
                        "output": _model_output_head(raw),
                        "iteration": iteration,
                        "parent_id": span_id
                    })
                    if repairs > self.max_repairs:
                        raise RuntimeError("model_protocol_error")
                    # 空白正文说明是上下文把模型带偏了，原样重试只会复现，直接换最小上下文；
                    # 其他格式错误先原样重试一次（代价更低且保留历史连贯性），最后一次再退到最小上下文。
                    minimal_context = minimal_context or event.code == "empty_response" or repairs >= self.max_repairs
                    repair_prompt = _repair_instruction(event.code, mode)
                    await self.store.add_trace(run_id, "model.repair", {
                        "repairs": repairs,
                        "code": event.code,
                        "minimal_context": minimal_context,
                        "iteration": iteration,
                        "parent_id": span_id
                    })
                    continue
                repair_prompt = None
                minimal_context = False
                # 第 4 步之三：工具调用。同一个 call_id 已有结果时走复用，而不是重复执行。
                existing = await self.store.get_tool_call(run_id, event.call_id)
                canonical_args = json.dumps(event.arguments, ensure_ascii=False, sort_keys=True)
                if existing:
                    if existing["name"] != event.name or existing["arguments"] != canonical_args:
                        repairs += 1
                        await self.store.add_trace(run_id, "model.invalid", {
                            "code": "tool_call_id_conflict",
                            "iteration": iteration,
                            "parent_id": span_id
                        })
                        if repairs > self.max_repairs:
                            raise RuntimeError("model_protocol_error")
                        repair_prompt = "同一个工具调用 ID 不能对应不同的工具或参数。请使用新的调用 ID，或直接给出最终回答。"
                        continue
                    result = ToolResult(**json.loads(existing["result"]))
                    repair_prompt = "这个工具调用已经执行完成。请使用已记录的工具结果继续处理，不要重复执行。"
                    await self.store.add_trace(run_id, "tool.reused", {
                        "call_id": event.call_id,
                        "name": event.name,
                        "iteration": iteration,
                        "parent_id": span_id
                    })
                else:
                    # 把模型这一步的工具调用原样记进历史（不是我们自己拼的），下一轮模型才能看到自己做过什么。
                    tool_payload: dict[str, Any] = {"content": event.decision_summary or None, "tool_calls": [{
                        "id": event.call_id,
                        "type": "function",
                        "function": {"name": event.name, "arguments": canonical_args}
                    }]}
                    # 思考模式要求把本轮调用的私有推理原样回传，否则下一次模型请求会被直接拒绝（400）。
                    # 它只用于协议回传，不进入界面展示，也不参与决策。
                    if event.reasoning_content:
                        tool_payload["reasoning_content"] = event.reasoning_content[:8000]
                    # 思考过程：把流式收下来的推理一并展示，比只留一句决策说明更接近模型实际在想什么。
                    if thinking:
                        tool_payload["thinking"] = thinking[:8000]
                    if assistant_seq is None:
                        # 模型没有产出任何增量（直接给出工具调用）：工具调用就是这一轮唯一的产物。
                        # 这里不回填 assistant_seq：它是"流式落过库、需要时可撤回"的标记，
                        # 而这条消息是工具调用的记录，必须一直留在历史里。
                        recorded_seq = await self.store.add_message(session_id, run_id, "assistant", tool_payload)
                    else:
                        # 流式期间已经落了本轮的助手消息（含思考过程）：把工具调用补到同一条上，
                        # 不要另起一条，否则界面上会出现"只有思考、没有下文"的空壳消息。
                        # 这一轮到此已经有了确定结论（调用工具），同样摘掉 incomplete 标记。
                        await self.store.update_message_fields(
                            session_id,
                            assistant_seq,
                            {**tool_payload, "incomplete": False}
                        )
                        recorded_seq = assistant_seq
                    # 这一轮已确定是工具调用，立刻把这个事实推给前端。否则前端要等运行结束拉全量
                    # 消息才知道，那期间会把决策说明当成回答渲染（与思考面板重复一遍），
                    # 而且"调用了 N 个工具"也要等整轮跑完才出现。
                    self.events.publish(run_id, {
                        "type": "delta",
                        "seq": recorded_seq,
                        "content": tool_payload.get("content") or "",
                        "thinking": tool_payload.get("thinking") or "",
                        "tool_calls": tool_payload["tool_calls"]
                    })
                    await self.store.add_trace(run_id, "tool.started", {
                        "call_id": event.call_id,
                        "name": event.name,
                        "iteration": iteration,
                        "parent_id": span_id
                    })
                    # 工具结果的预算按工具类型分配：检索类给得少（1%），其余给 10%，并受剩余上下文约束。
                    ratio = .02 if event.name == "resource_search" else .10
                    remaining = max(1, manager.input_budget - bundle.estimated_tokens)
                    result_budget = max(1, min(int(manager.input_budget * ratio), remaining))
                    spec = self.registry.get(event.name)
                    tool_started = time.perf_counter()
                    if spec and spec.effect == "local_write":
                        # 会写库的工具（如待办）走"单一事务"路径：工具执行、工具调用记录、结果消息、
                        # 轨迹事件全部挂在同一个 connection 上，要么一起成功要么一起回滚。
                        async with self.store.engine.begin() as connection:
                            context = ExecutionContext(
                                run_id,
                                session_id,
                                result_token_budget=result_budget,
                                db_connection=connection
                            )
                            result = await self._tool_call(run_id, event.name, event.arguments, context)
                            await self.store.save_tool_call(
                                run_id,
                                event.call_id,
                                event.name,
                                event.arguments,
                                result,
                                connection
                            )
                            await self.store.add_message(session_id, run_id, "tool", {
                                "tool_call_id": event.call_id,
                                "name": event.name,
                                "content": json.dumps(result.__dict__, ensure_ascii=False)
                            }, connection)
                            await self.store.add_trace(run_id, "tool.finished", {
                                "call_id": event.call_id,
                                "name": event.name,
                                "iteration": iteration,
                                "ok": result.ok,
                                "error_code": result.error.get("code") if result.error else None,
                                "duration_ms": round((time.perf_counter() - tool_started) * 1000, 2),
                                "parent_id": span_id
                            }, connection)
                    else:
                        result = await self._tool_call(
                            run_id,
                            event.name,
                            event.arguments,
                            ExecutionContext(run_id, session_id, result_token_budget=result_budget)
                        )
                        result_tokens = estimate_tokens(result.__dict__)
                        # 结果太大就转存为资源、只留一句摘要：避免一次工具调用就把上下文撑爆。
                        # resource_read/search 本身就是为了分页读大内容，不适用这条转存规则。
                        if event.name not in {
                            "resource_read",
                            "resource_search"
                        } and (result_tokens > manager.input_budget * .10 or result_tokens > remaining):
                            resource_id = await self.resources.save(
                                session_id,
                                json.dumps(result.__dict__, ensure_ascii=False),
                                "tool_result"
                            )
                            result = ToolResult(True, {
                                "resource_id": resource_id,
                                "summary": "工具结果较大，已保存为资源，可按需读取。"
                            }, mock=result.mock, truncated=True)
                        await self.store.save_tool_call(run_id, event.call_id, event.name, event.arguments, result)
                        await self.store.add_message(session_id, run_id, "tool", {
                            "tool_call_id": event.call_id,
                            "name": event.name,
                            "content": json.dumps(result.__dict__, ensure_ascii=False)
                        })
                        await self.store.add_trace(run_id, "tool.finished", {
                            "call_id": event.call_id,
                            "name": event.name,
                            "iteration": iteration,
                            "ok": result.ok,
                            "error_code": result.error.get("code") if result.error else None,
                            "duration_ms": round((time.perf_counter() - tool_started) * 1000, 2),
                            "parent_id": span_id
                        })
                operations.append({"call_id": event.call_id, "name": event.name, "result": result.__dict__})
        except RuntimeError as exc:
            # 运行期内可预期的失败都带稳定错误码，这里统一映射成状态与用户可读文案。
            code = error_code_of(exc)
            status = (
                "limit_reached" if code in {"model_call_limit"}
                else "cancelled" if code == "cancelled"
                else "failed"
            )
            answer = answer_for(code, "Agent 未能完成本次回答，请重试。")
            # 流式输出到一半才失败（被取消、模型掉线、协议纠错耗尽）时，那条半截消息必须撤回：
            # 否则界面上会留下一条看着像完整回答的残句，它还会被下一轮的模型当成已完成的结果。
            await self._discard_partial(session_id, assistant_seq, run_id)
            # 结尾统一补一条不完整的助手消息：界面上要有明确交代，不能让对话停在半空。
            await self.store.add_message(session_id, run_id, "assistant", {"content": answer, "incomplete": True})
            await self.store.finish_run(run_id, status, answer, {"code": code})
            await self.store.add_trace(run_id, "run.finished", {"status": status, "code": code})
            return RunResult(run_id, session_id, status, answer, {"code": code}, operations)
        except Exception as exc:
            # 未预期异常：对外只留异常类型名，堆栈必须进服务端日志，否则线上无从排查。
            code = error_code_of(exc)
            logger.exception("run %s failed with unexpected error", run_id)
            answer = "Agent 执行失败，已完成的工具操作仍然保留，请重试。"
            await self._discard_partial(session_id, assistant_seq, run_id)
            await self.store.add_message(session_id, run_id, "assistant", {"content": answer, "incomplete": True})
            await self.store.finish_run(run_id, "failed", answer, {"code": code})
            await self.store.add_trace(run_id, "run.finished", {"status": "failed", "code": code})
            return RunResult(run_id, session_id, "failed", answer, {"code": code}, operations)
