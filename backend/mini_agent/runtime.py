import asyncio
import json
import time
from typing import Any, Callable

import httpx

from .context import ContextManager, estimate_tokens
from .contracts import ExecutionContext, Final, Invalid, RunResult, ToolCall, ToolResult
from .model import ModelClient, parse_response
from .storage import ResourceStore, Store
from .tools import ToolRegistry


def _model_output_head(raw: dict[str, Any], limit: int = 160) -> dict[str, Any]:
    """取出模型正文开头、结束原因与用量，仅用于在 trace 里定位协议错误原因。"""
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
    return {
        "finish_reason": finish,
        "content_len": len(text),
        "content_head": text[:limit],
        "reasoning_len": len(str(reasoning)),
        "usage": usage if isinstance(usage, dict) else None,
    }


NATIVE_PROTOCOL_HINT = (
    "请使用标准的工具调用方式工作。每次回复的第一行都必须是决策说明，格式固定为「思考：<一句简短中文判断依据>」，"
    "第二行起才是给用户的最终回答；需要调用工具时，第一行仍然写这条决策说明，然后再发出工具调用。"
    "决策说明只写这一步的判断依据：一句话讲清楚即可，不要复述用户问题，不要展开推理链，不要编造没有发生的动作。"
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
    def __init__(self, store: Store, registry: ToolRegistry, resources: ResourceStore, model_factory: Callable[[str], tuple[ModelClient, str, int, int]], *, max_model_calls: int = 12, max_repairs: int = 2, max_summary_calls: int = 2, run_timeout: float = 180, safety_margin: int = 1024, soft_context_ratio: float = .70, hard_context_ratio: float = .85, target_context_ratio: float = .55) -> None:
        self.store, self.registry, self.resources = store, registry, resources
        self.model_factory = model_factory
        self.max_model_calls, self.max_repairs, self.max_summary_calls, self.run_timeout = max_model_calls, max_repairs, max_summary_calls, run_timeout
        self.safety_margin = safety_margin
        self.soft_context_ratio, self.hard_context_ratio, self.target_context_ratio = soft_context_ratio, hard_context_ratio, target_context_ratio
        self._submission_lock = asyncio.Lock()

    async def submit(self, session_id: str, message: str, request_key: str | None = None) -> tuple[dict[str, Any], bool]:
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
                if estimate_tokens(message) >= input_budget * self.soft_context_ratio:
                    resource_id = await self.resources.save(session_id, message, "text")
                    visible_message = f"完整消息已保存为资源 {resource_id}。回答前请按需使用资源查找或资源读取工具查看。"
                await self.store.add_message(session_id, run["id"], "user", {"content": visible_message})
                if await self.store.count_messages(session_id) == 1:
                    await self.store.set_session_title(session_id, message)
            except Exception:
                await self.store.finish_run(run["id"], "failed", "消息准备失败，请重试。", {"code": "input_prepare_failed"})
                raise
        return run, created

    async def _model_call(self, run_id: str, model: ModelClient, messages: list[dict[str, Any]], tools: list[dict[str, Any]], tool_choice: str, *, max_attempts: int = 3, phase: str = "response", iteration: int = 0) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(max_attempts):
            current = await self.store.get_run(run_id)
            if not current or current["model_calls"] >= self.max_model_calls:
                raise RuntimeError("model_call_limit")
            await self.store.increment_model_calls(run_id)
            started = time.perf_counter()
            await self.store.add_trace(run_id, "model.started", {"attempt": attempt + 1, "iteration": iteration, "phase": phase, "tool_choice": tool_choice})
            try:
                task = asyncio.create_task(model.complete(messages, tools, tool_choice=tool_choice))
                while not task.done():
                    await asyncio.wait({task}, timeout=.1)
                    if await self.store.is_cancel_requested(run_id):
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        raise RuntimeError("cancelled")
                raw = await task
                await self.store.add_trace(run_id, "model.finished", {"attempt": attempt + 1, "iteration": iteration, "phase": phase, "duration_ms": round((time.perf_counter() - started) * 1000, 2)})
                return raw
            except httpx.TransportError as exc:
                # 覆盖 ConnectError、ReadError、RemoteProtocolError 等全部传输层故障；
                # 只捕获 TimeoutException/NetworkError 会漏掉协议层错误，导致直接抛出未分类异常。
                last_error = exc
                event = "model.retry" if attempt + 1 < max_attempts else "model.failed"
                await self.store.add_trace(run_id, event, {"attempt": attempt + 1, "iteration": iteration, "phase": phase, "code": type(exc).__name__, "detail": str(exc)[:300], "duration_ms": round((time.perf_counter() - started) * 1000, 2)})
                if attempt + 1 < max_attempts:
                    await asyncio.sleep((.5, 1)[attempt])
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in {429, 500, 502, 503, 504}:
                    raise
                last_error = exc
                event = "model.retry" if attempt + 1 < max_attempts else "model.failed"
                await self.store.add_trace(run_id, event, {"attempt": attempt + 1, "iteration": iteration, "phase": phase, "code": f"http_{exc.response.status_code}", "detail": str(exc)[:300], "duration_ms": round((time.perf_counter() - started) * 1000, 2)})
                if attempt + 1 < max_attempts:
                    await asyncio.sleep((.5, 1)[attempt])
        raise RuntimeError("model_unavailable") from last_error

    async def _tool_call(self, run_id: str, name: str, args: dict[str, Any], context: ExecutionContext) -> ToolResult:
        task = asyncio.create_task(self.registry.execute(name, args, context))
        while not task.done():
            await asyncio.wait({task}, timeout=.1)
            if await self.store.is_cancel_requested(run_id) and name != "todo":
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                return ToolResult(False, error={"code": "cancelled", "message": "工具等待已取消。", "outcome": "unknown"})
        return await task

    async def _maybe_summarize(self, run_id: str, session_id: str, manager: ContextManager, model: ModelClient, mode: str, tools: list[dict[str, Any]], summary_calls: int, iteration: int) -> int:
        bundle = await manager.prepare(session_id, tools)
        if not bundle.over_soft or summary_calls >= self.max_summary_calls:
            return summary_calls
        candidate = await manager.compression_candidate(session_id)
        if not candidate:
            return summary_calls
        rows, covered = candidate
        previous = await self.store.latest_summary(session_id)
        summary_messages = manager.summary_messages(previous["content"] if previous else None, rows, mode == "json")
        while len(rows) > 1 and estimate_tokens(summary_messages) > manager.input_budget:
            rows = rows[: len(rows) // 2]
            covered = int(rows[-1]["seq"])
            summary_messages = manager.summary_messages(previous["content"] if previous else None, rows, mode == "json")
        before = await self.store.get_run(run_id)
        try:
            raw = await self._model_call(
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
            code = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
            if code in {"cancelled", "model_call_limit"}:
                raise
            await self.store.add_trace(run_id, "context.compaction_failed", {"code": code, "summary_calls": summary_calls})
            return summary_calls
        after = await self.store.get_run(run_id)
        summary_calls += max(0, int(after["model_calls"]) - int(before["model_calls"])) if before and after else 1
        event = parse_response(raw, mode)
        if isinstance(event, Final) and len(event.answer.encode("utf-8")) <= 4096:
            await self.store.save_summary(session_id, covered, event.answer)
            await self.store.add_trace(run_id, "context.compacted", {"covered_through_seq": covered, "summary_calls": summary_calls})
        else:
            await self.store.add_trace(run_id, "context.compaction_failed", {"code": getattr(event, "code", "invalid_summary"), "summary_calls": summary_calls})
        return summary_calls

    async def execute(self, run_id: str) -> RunResult:
        try:
            return await asyncio.wait_for(self._execute(run_id), timeout=self.run_timeout)
        except asyncio.TimeoutError:
            answer = "本次运行已达到时间限制，已完成的工具操作仍然保留。"
            run = await self.store.get_run(run_id)
            if run:
                await asyncio.shield(self.store.add_message(run["session_id"], run_id, "assistant", {"content": answer, "incomplete": True}))
                await asyncio.shield(self.store.finish_run(run_id, "limit_reached", answer, {"code": "run_timeout"}))
                await asyncio.shield(self.store.add_trace(run_id, "run.finished", {"status": "limit_reached", "code": "run_timeout"}))
            return RunResult(run_id, run["session_id"] if run else "", "limit_reached", answer, {"code": "run_timeout"})
        except Exception as exc:
            run = await self.store.get_run(run_id)
            session_id = run["session_id"] if run else ""
            raw_code = str(exc)
            prefix = raw_code.split(":", 1)[0]
            known_codes = {"model_api_key_missing", "model_unavailable", "model_protocol_error", "context_too_large", "model_call_limit"}
            code = prefix if prefix in known_codes else type(exc).__name__
            answer = {
                "model_api_key_missing": "模型 API 密钥未配置，请检查服务端环境变量。",
                "model_unavailable": "模型服务暂时不可用，请检查网络连接和 API 配置后重试。",
                "model_protocol_error": "模型返回了无法解析的响应，请重试。",
                "context_too_large": "对话内容超过模型上下文限制，请缩短消息或新建会话后重试。",
            }.get(code, "Agent 未能启动或完成本次回答，请重试。")
            if run:
                await self.store.finish_run(run_id, "failed", answer, {"code": code})
                await self.store.add_trace(run_id, "run.finished", {"status": "failed", "code": code})
            return RunResult(run_id, session_id, "failed", answer, {"code": code})

    async def _execute(self, run_id: str) -> RunResult:
        run = await self.store.get_run(run_id)
        if not run:
            raise LookupError("run_not_found")
        session_id = run["session_id"]
        session = await self.store.get_session(session_id)
        if not session:
            raise LookupError("session_not_found")
        model, mode, context_window, output_reserve = self.model_factory(run["model_name"])
        manager = ContextManager(self.store, context_window, output_reserve, self.safety_margin, soft_ratio=self.soft_context_ratio, hard_ratio=self.hard_context_ratio)
        tools = self.registry.definitions()
        repairs = summary_calls = 0
        repair_prompt: str | None = None
        # 上下文退化（长上下文下模型只返回空白正文）时，改用「只保留本轮消息」的最小上下文重试。
        minimal_context = False
        operations: list[dict[str, Any]] = []
        iteration = 0
        await self.store.add_trace(run_id, "run.started", {})
        try:
            while True:
                iteration += 1
                if await self.store.is_cancel_requested(run_id):
                    answer = "已停止本次运行，已完成的工具操作仍然保留。"
                    await self.store.add_message(session_id, run_id, "assistant", {"content": answer})
                    await self.store.finish_run(run_id, "cancelled", answer)
                    await self.store.add_trace(run_id, "run.finished", {"status": "cancelled"})
                    return RunResult(run_id, session_id, "cancelled", answer, operations=operations)
                summary_calls = await self._maybe_summarize(run_id, session_id, manager, model, mode, tools, summary_calls, iteration)
                recovery_run = run_id if minimal_context else None
                bundle = await manager.prepare(session_id, tools, repair_prompt, only_run_id=recovery_run)
                if bundle.over_hard:
                    bundle = await manager.prepare(session_id, tools, repair_prompt, target_ratio=self.target_context_ratio, only_run_id=recovery_run)
                if bundle.estimated_tokens > bundle.input_budget:
                    raise RuntimeError("context_too_large")
                run_now = await self.store.get_run(run_id)
                tool_choice = "none" if run_now and run_now["model_calls"] >= self.max_model_calls - 1 else "auto"
                # 协议约定放在消息末尾：紧邻生成位置，模型遵从率明显高于混在开头 system 里。
                bundle.messages.append({"role": "system", "content": _protocol_hint(mode, tools)})
                raw = await self._model_call(run_id, model, bundle.messages, tools, tool_choice, iteration=iteration)
                event = parse_response(raw, mode)
                if isinstance(event, Final):
                    initial_payload: dict[str, Any] = {"content": ""}
                    if event.decision_summary:
                        initial_payload["thinking"] = event.decision_summary.strip()
                    assistant_seq = await self.store.add_message(session_id, run_id, "assistant", initial_payload)
                    streamed = ""
                    for index in range(0, len(event.answer), 24):
                        if await self.store.is_cancel_requested(run_id):
                            raise RuntimeError("cancelled")
                        streamed += event.answer[index:index + 24]
                        await self.store.update_message_content(session_id, assistant_seq, streamed)
                        await self.store.add_trace(run_id, "assistant.delta", {"content": streamed, "complete": index + 24 >= len(event.answer)})
                        await asyncio.sleep(.02)
                    await self.store.finish_run(run_id, "completed", event.answer)
                    await self.store.add_trace(run_id, "run.finished", {"status": "completed"})
                    return RunResult(run_id, session_id, "completed", event.answer, operations=operations)
                if isinstance(event, Invalid):
                    repairs += 1
                    await self.store.add_trace(run_id, "model.invalid", {"code": event.code, "reason": event.message[:200], "output": _model_output_head(raw), "iteration": iteration})
                    if repairs > self.max_repairs:
                        raise RuntimeError("model_protocol_error")
                    # 空白正文说明是上下文把模型带偏了，原样重试只会复现，直接换最小上下文；
                    # 其他格式错误先原样重试一次（代价更低且保留历史连贯性），最后一次再退到最小上下文。
                    minimal_context = minimal_context or event.code == "empty_response" or repairs >= self.max_repairs
                    repair_prompt = _repair_instruction(event.code, mode)
                    await self.store.add_trace(run_id, "model.repair", {"repairs": repairs, "code": event.code, "minimal_context": minimal_context, "iteration": iteration})
                    continue
                repair_prompt = None
                minimal_context = False
                existing = await self.store.get_tool_call(run_id, event.call_id)
                canonical_args = json.dumps(event.arguments, ensure_ascii=False, sort_keys=True)
                if existing:
                    if existing["name"] != event.name or existing["arguments"] != canonical_args:
                        repairs += 1
                        await self.store.add_trace(run_id, "model.invalid", {"code": "tool_call_id_conflict", "iteration": iteration})
                        if repairs > self.max_repairs:
                            raise RuntimeError("model_protocol_error")
                        repair_prompt = "同一个工具调用 ID 不能对应不同的工具或参数。请使用新的调用 ID，或直接给出最终回答。"
                        continue
                    result = ToolResult(**json.loads(existing["result"]))
                    repair_prompt = "这个工具调用已经执行完成。请使用已记录的工具结果继续处理，不要重复执行。"
                    await self.store.add_trace(run_id, "tool.reused", {"call_id": event.call_id, "name": event.name, "iteration": iteration})
                else:
                    tool_payload: dict[str, Any] = {"content": event.decision_summary or None, "tool_calls": [{"id": event.call_id, "type": "function", "function": {"name": event.name, "arguments": canonical_args}}]}
                    # 思考模式要求把本轮调用的私有推理原样回传，否则下一次模型请求会被直接拒绝（400）。
                    # 它只用于协议回传，不进入界面展示，也不参与决策。
                    if event.reasoning_content:
                        tool_payload["reasoning_content"] = event.reasoning_content[:8000]
                    await self.store.add_message(session_id, run_id, "assistant", tool_payload)
                    await self.store.add_trace(run_id, "tool.started", {"call_id": event.call_id, "name": event.name, "iteration": iteration})
                    ratio = .02 if event.name == "resource_search" else .10
                    remaining = max(1, manager.input_budget - bundle.estimated_tokens)
                    result_budget = max(1, min(int(manager.input_budget * ratio), remaining))
                    spec = self.registry.get(event.name)
                    tool_started = time.perf_counter()
                    if spec and spec.effect == "local_write":
                        async with self.store.engine.begin() as connection:
                            context = ExecutionContext(run_id, session_id, result_token_budget=result_budget, db_connection=connection)
                            result = await self._tool_call(run_id, event.name, event.arguments, context)
                            await self.store.save_tool_call(run_id, event.call_id, event.name, event.arguments, result, connection)
                            await self.store.add_message(session_id, run_id, "tool", {"tool_call_id": event.call_id, "name": event.name, "content": json.dumps(result.__dict__, ensure_ascii=False)}, connection)
                            await self.store.add_trace(run_id, "tool.finished", {"call_id": event.call_id, "name": event.name, "iteration": iteration, "ok": result.ok, "error_code": result.error.get("code") if result.error else None, "duration_ms": round((time.perf_counter() - tool_started) * 1000, 2)}, connection)
                    else:
                        result = await self._tool_call(run_id, event.name, event.arguments, ExecutionContext(run_id, session_id, result_token_budget=result_budget))
                        result_tokens = estimate_tokens(result.__dict__)
                        if event.name not in {"resource_read", "resource_search"} and (result_tokens > manager.input_budget * .10 or result_tokens > remaining):
                            resource_id = await self.resources.save(session_id, json.dumps(result.__dict__, ensure_ascii=False), "tool_result")
                            result = ToolResult(True, {"resource_id": resource_id, "summary": "工具结果较大，已保存为资源，可按需读取。"}, mock=result.mock, truncated=True)
                        await self.store.save_tool_call(run_id, event.call_id, event.name, event.arguments, result)
                        await self.store.add_message(session_id, run_id, "tool", {"tool_call_id": event.call_id, "name": event.name, "content": json.dumps(result.__dict__, ensure_ascii=False)})
                        await self.store.add_trace(run_id, "tool.finished", {"call_id": event.call_id, "name": event.name, "iteration": iteration, "ok": result.ok, "error_code": result.error.get("code") if result.error else None, "duration_ms": round((time.perf_counter() - tool_started) * 1000, 2)})
                operations.append({"call_id": event.call_id, "name": event.name, "result": result.__dict__})
        except RuntimeError as exc:
            code = str(exc)
            status = "limit_reached" if code in {"model_call_limit"} else "cancelled" if code == "cancelled" else "failed"
            answers = {
                "context_too_large": "对话内容超过模型上下文限制，请缩短消息或新建会话后重试。",
                "model_api_key_missing": "模型 API 密钥未配置，请检查服务端环境变量。",
                "model_call_limit": "本次运行已达到模型调用次数上限。",
                "model_protocol_error": "模型返回了无法解析的响应，请重试。",
                "model_unavailable": "模型服务暂时不可用，请检查网络连接和 API 配置后重试。",
                "cancelled": "已停止本次运行，已完成的工具操作仍然保留。",
            }
            answer = answers.get(code, "Agent 未能完成本次回答，请重试。")
            await self.store.add_message(session_id, run_id, "assistant", {"content": answer, "incomplete": True})
            await self.store.finish_run(run_id, status, answer, {"code": code})
            await self.store.add_trace(run_id, "run.finished", {"status": status, "code": code})
            return RunResult(run_id, session_id, status, answer, {"code": code}, operations)
        except Exception as exc:
            answer = "Agent 执行失败，已完成的工具操作仍然保留，请重试。"
            await self.store.add_message(session_id, run_id, "assistant", {"content": answer, "incomplete": True})
            await self.store.finish_run(run_id, "failed", answer, {"code": type(exc).__name__})
            await self.store.add_trace(run_id, "run.finished", {"status": "failed", "code": type(exc).__name__})
            return RunResult(run_id, session_id, "failed", answer, {"code": type(exc).__name__}, operations)
