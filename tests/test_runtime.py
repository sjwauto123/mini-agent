import json
import asyncio
import re

import httpx

from mini_agent.contracts import ToolResult
from mini_agent.errors import ModelServiceError
from mini_agent.events import RunEventBus
from mini_agent.runtime import LANGUAGE_HINT, AgentRuntime
from mini_agent.tools import ToolSpec

from .fakes import ScriptedModel, final, tool


async def make_runtime(services, model):
    store, resources, registry = services
    runtime = AgentRuntime(store, registry, resources, lambda _: (model, "native", 16384, 2048))
    session_id = await store.create_session()
    return runtime, store, session_id


async def test_direct_answer_uses_no_tool(services):
    model = ScriptedModel([final("Hello")])
    runtime, store, session_id = await make_runtime(services, model)
    run, _ = await runtime.submit(session_id, "Hi", "key-1")
    result = await runtime.execute(run["id"])
    assert result.status == "completed" and result.answer == "Hello"
    assert len(model.calls) == 1
    assert await store.list_messages(session_id) == [
        {"role": "user", "seq": 1, "run_id": run["id"], "content": "Hi"},
        {"role": "assistant", "seq": 2, "run_id": run["id"], "content": "Hello"},
    ]


async def test_tool_loop_and_followup_context(services):
    model = ScriptedModel([tool("c1", "calculator", '{"expression":"125*8"}'), final("The answer is 1000."), tool("c2", "calculator", '{"expression":"1000/4"}'), final("250")])
    runtime, store, session_id = await make_runtime(services, model)
    first, _ = await runtime.submit(session_id, "Calculate 125 * 8")
    result1 = await runtime.execute(first["id"])
    second, _ = await runtime.submit(session_id, "Divide that by 4")
    result2 = await runtime.execute(second["id"])
    assert result1.operations[0]["result"]["data"]["value"] == 1000
    assert result2.operations[0]["result"]["data"]["value"] == 250.0
    second_context = model.calls[2]["messages"]
    assert any("1000" in json.dumps(message) for message in second_context)


async def test_thinking_summary_and_private_reasoning_stay_separate(services):
    """思考过程用决策摘要展示；思考模式的私有推理只按协议回传，不进界面、不进决策。"""
    model = ScriptedModel([
        tool("c1", "calculator", '{"expression":"1+1"}', content="先算一下。", reasoning="私有推理：这一步应该调用计算器。"),
        final("答案是 2。", reasoning="私有推理：已经拿到计算结果，可以直接回答。"),
    ])
    runtime, store, session_id = await make_runtime(services, model)
    run, _ = await runtime.submit(session_id, "1+1 等于几")
    result = await runtime.execute(run["id"])
    assert result.status == "completed"

    messages = await store.list_messages(session_id)
    tool_round = next(message for message in messages if message.get("tool_calls"))
    final_round = messages[-1]
    assert tool_round["content"] == "先算一下。"
    assert final_round["thinking"] == "私有推理：已经拿到计算结果，可以直接回答。"
    assert tool_round["reasoning_content"].startswith("私有推理：这一步应该调用计算器")

    second_context = model.calls[1]["messages"]
    echoed = next(message for message in second_context if message.get("tool_calls"))
    assert echoed["reasoning_content"] == tool_round["reasoning_content"]
    assert all("thinking" not in message for message in second_context)


async def test_weather_then_todo(services):
    from datetime import date, timedelta
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    model = ScriptedModel([
        tool("weather-1", "weather", json.dumps({"city": "北京", "date": tomorrow}, ensure_ascii=False)),
        tool("todo-1", "todo", json.dumps({"action": "add", "text": "明天带伞"}, ensure_ascii=False)),
        final("模拟天气显示有雨，已添加带伞待办。"),
    ])
    runtime, _, session_id = await make_runtime(services, model)
    run, _ = await runtime.submit(session_id, "查北京明天天气，如果下雨就记待办")
    result = await runtime.execute(run["id"])
    assert result.status == "completed"
    assert [operation["name"] for operation in result.operations] == ["weather", "todo"]
    assert result.operations[0]["result"]["mock"] is True


async def test_request_idempotency_and_busy(services):
    model = ScriptedModel([final("ok")])
    runtime, _, session_id = await make_runtime(services, model)
    first, created = await runtime.submit(session_id, "hello", "same")
    repeated, repeated_created = await runtime.submit(session_id, "hello", "same")
    assert created and not repeated_created and repeated["id"] == first["id"]
    try:
        await runtime.submit(session_id, "another")
        assert False, "active session should reject another run"
    except RuntimeError as exc:
        assert str(exc) == "session_busy"


async def test_protocol_repairs_are_bounded(services):
    invalid = {"choices": [{"finish_reason": "stop", "message": {"content": ""}}]}
    model = ScriptedModel([invalid, invalid, invalid])
    runtime, _, session_id = await make_runtime(services, model)
    run, _ = await runtime.submit(session_id, "hello")
    result = await runtime.execute(run["id"])
    assert result.status == "failed"
    assert result.error["code"] == "model_protocol_error"
    assert len(model.calls) == 3


async def test_blank_body_retries_with_minimal_context(services):
    """模型在多轮历史下偶发返回空白正文时，应自动退到「只保留本轮消息」的最小上下文并恢复。"""
    blank = {"choices": [{"finish_reason": "stop", "message": {"content": "        "}}]}
    model = ScriptedModel([final("第一轮回答"), blank, final("恢复后的回答")])
    runtime, store, session_id = await make_runtime(services, model)

    first, _ = await runtime.submit(session_id, "第一个问题")
    assert (await runtime.execute(first["id"])).status == "completed"

    second, _ = await runtime.submit(session_id, "第二个问题")
    result = await runtime.execute(second["id"])
    assert result.status == "completed" and result.answer == "恢复后的回答"
    assert len(model.calls) == 3

    # 首次重试仍用完整上下文（带着第一轮历史），最后一次才退到最小上下文（只保留本轮消息）。
    assert "第一轮回答" in json.dumps(model.calls[1]["messages"], ensure_ascii=False)
    recovery = json.dumps(model.calls[2]["messages"], ensure_ascii=False)
    assert "第一轮回答" not in recovery and "第一个问题" not in recovery
    assert "第二个问题" in recovery

    trace = await store.list_trace(second["id"])
    repairs = [item for item in trace if item["event_type"] == "model.repair"]
    assert repairs and repairs[-1]["payload"]["minimal_context"] is True


async def test_model_unavailable_has_readable_error_and_trace(services):
    class UnavailableModel:
        async def complete(self, messages, tools, *, tool_choice="auto"):
            raise httpx.ConnectError("simulated network failure")

    model = UnavailableModel()
    runtime, store, session_id = await make_runtime(services, model)
    run, _ = await runtime.submit(session_id, "hello")
    result = await runtime.execute(run["id"])
    assert result.status == "failed"
    assert result.error["code"] == "model_unavailable"
    assert "模型服务暂时不可用" in result.answer
    trace = await store.list_trace(run["id"])
    failed = [item for item in trace if item["event_type"] == "model.failed"]
    assert failed and failed[-1]["payload"]["code"] == "ConnectError"
    assert "simulated network failure" in failed[-1]["payload"]["detail"]


async def test_protocol_level_transport_error_is_retried_not_unclassified(services):
    """协议层传输错误（RemoteProtocolError）属于可重试故障，必须走重试与友好提示，不能变成未分类异常。"""

    class FlakyModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, tools, *, tool_choice="auto"):
            self.calls += 1
            raise httpx.RemoteProtocolError("server disconnected without response")

    model = FlakyModel()
    runtime, store, session_id = await make_runtime(services, model)
    run, _ = await runtime.submit(session_id, "hello")
    result = await runtime.execute(run["id"])
    assert result.status == "failed"
    assert result.error["code"] == "model_unavailable"
    assert "模型服务暂时不可用" in result.answer
    assert model.calls == 3
    trace = await store.list_trace(run["id"])
    failed = [item for item in trace if item["event_type"] == "model.failed"]
    assert failed and failed[-1]["payload"]["code"] == "RemoteProtocolError"


async def test_retry_is_announced_and_language_hint_is_the_last_message(services):
    """两件事一起验：

    1. 上游挂起时（实测有 20.9 秒才抛 RemoteProtocolError 的情况）必须推一条"正在重试"的提示——
       那段沉默里数据库毫无变化，快照推不出内容，没有这条提示界面看着就是卡死；
    2. 语言约束必须是模型看到的**最后一条**消息：只写在开头 SYSTEM_PROMPT 或协议提示句尾时，
       实测第 1 轮仍会整轮返回英文推理（1000+ 字，会原样显示在思考面板里）。
    """

    class FlakyModel:
        def __init__(self) -> None:
            self.calls = 0
            self.seen: list[dict] = []

        async def complete(self, messages, tools, *, tool_choice="auto"):
            self.calls += 1
            self.seen = messages
            if self.calls == 1:
                raise httpx.RemoteProtocolError("server disconnected without response")
            return final("好了")

    store, resources, registry = services
    bus = RunEventBus()
    model = FlakyModel()
    runtime = AgentRuntime(store, registry, resources, lambda _: (model, "native", 16384, 2048), events=bus)
    session_id = await store.create_session()
    run, _ = await runtime.submit(session_id, "你好")
    queue = bus.subscribe(run["id"])
    result = await runtime.execute(run["id"])
    assert result.status == "completed" and model.calls == 2

    notices = []
    while not queue.empty():
        event = queue.get_nowait()
        if event.get("type") == "notice":
            notices.append(event)
    assert len(notices) == 1, notices
    assert notices[0]["code"] == "model_retry"
    assert notices[0]["attempt"] == 2 and notices[0]["delay_ms"] == 500

    # 模型输入的最后一条必须是语言约束：位置比措辞更关键。
    assert model.seen[-1]["role"] == "system"
    assert model.seen[-1]["content"] == LANGUAGE_HINT


async def test_model_call_limit_stops_loop(services):
    responses = [tool(f"c{i}", "calculator", '{"expression":"1+1"}') for i in range(20)]
    model = ScriptedModel(responses)
    runtime, _, session_id = await make_runtime(services, model)
    run, _ = await runtime.submit(session_id, "never finish")
    result = await runtime.execute(run["id"])
    assert result.status == "limit_reached"
    assert result.error["code"] == "model_call_limit"
    assert len(model.calls) == 12


async def test_large_tool_result_is_externalized(services):
    store, resources, registry = services
    async def huge(_, __):
        return ToolResult(True, {"content": "x" * 100_000})
    registry.register(ToolSpec("huge", "Return a large test result.", {"type": "object", "additionalProperties": False}, huge))
    model = ScriptedModel([tool("large-1", "huge", "{}"), final("stored")])
    runtime = AgentRuntime(store, registry, resources, lambda _: (model, "native", 16384, 2048))
    session_id = await store.create_session()
    run, _ = await runtime.submit(session_id, "get large data")
    result = await runtime.execute(run["id"])
    tool_result = result.operations[0]["result"]
    assert tool_result["truncated"] is True
    assert "resource_id" in tool_result["data"]


async def test_context_summary_is_triggered(services):
    store, resources, registry = services
    session_id = await store.create_session()
    for index in range(6):
        old, _ = await store.start_run(session_id, f"old-{index}", None)
        await store.add_message(session_id, old["id"], "user", {"content": f"fact {index} " + "x" * 1000})
        await store.add_message(session_id, old["id"], "assistant", {"content": f"noted {index} " + "y" * 1000})
        await store.finish_run(old["id"], "completed", "noted")
    model = ScriptedModel([final("facts 0 and 1 were discussed"), final("continued")])
    runtime = AgentRuntime(store, registry, resources, lambda _: (model, "native", 5000, 500))
    run, _ = await runtime.submit(session_id, "continue")
    result = await runtime.execute(run["id"])
    assert result.status == "completed"
    assert await store.latest_summary(session_id) is not None
    assert any(event["event_type"] == "context.compacted" for event in await store.list_trace(run["id"]))


async def test_cancel_interrupts_model_wait(services):
    class SlowModel:
        async def complete(self, messages, tools, *, tool_choice="auto"):
            await asyncio.sleep(30)
            return final("too late")
    runtime, store, session_id = await make_runtime(services, SlowModel())
    run, _ = await runtime.submit(session_id, "wait")
    task = asyncio.create_task(runtime.execute(run["id"]))
    await asyncio.sleep(.15)
    assert await store.request_cancel(run["id"])
    result = await asyncio.wait_for(task, timeout=2)
    assert result.status == "cancelled"


async def test_concurrent_submissions_allow_only_one_run(services):
    model = ScriptedModel([final("ok")])
    runtime, _, session_id = await make_runtime(services, model)
    results = await asyncio.gather(
        runtime.submit(session_id, "first"),
        runtime.submit(session_id, "second"),
        return_exceptions=True,
    )
    accepted = [item for item in results if isinstance(item, tuple)]
    rejected = [item for item in results if isinstance(item, RuntimeError)]
    assert len(accepted) == 1
    assert len(rejected) == 1 and str(rejected[0]) == "session_busy"


async def test_long_input_is_externalized_before_model_call(services):
    model = ScriptedModel([final("I will inspect the resource as needed.")])
    runtime, store, session_id = await make_runtime(services, model)
    original = "x" * 50_000
    run, _ = await runtime.submit(session_id, original)
    result = await runtime.execute(run["id"])
    assert result.status == "completed"
    user_message = (await store.list_messages(session_id))[0]["content"]
    resource_id = re.search(r"资源 ([0-9a-f-]{36})", user_message).group(1)
    assert original not in json.dumps(model.calls[0]["messages"])
    restored = await runtime.registry.execute("resource_read", {"resource_id": resource_id, "limit": 10}, runtime_context(run["id"], session_id))
    assert restored.ok and restored.data["content"] == "x" * 10
    assert run["input_preview"] == original[:500]


async def test_resource_read_is_budgeted_and_not_externalized_again(services):
    store, resources, registry = services
    session_id = await store.create_session()
    resource_id = await resources.save(session_id, "z" * 20_000)
    model = ScriptedModel([tool("read-1", "resource_read", json.dumps({"resource_id": resource_id, "limit": 20_000})), final("read a page")])
    runtime = AgentRuntime(store, registry, resources, lambda _: (model, "native", 16384, 2048))
    run, _ = await runtime.submit(session_id, "read the resource")
    result = await runtime.execute(run["id"])
    tool_result = result.operations[0]["result"]
    assert result.status == "completed"
    assert tool_result["truncated"] is False
    assert 0 < len(tool_result["data"]["content"]) < 20_000
    assert tool_result["data"]["next_cursor"] == len(tool_result["data"]["content"])


async def test_search_prompt_injection_remains_tool_data(services):
    model = ScriptedModel([tool("search-1", "search", '{"query":"untrusted"}'), final("The mock result contains untrusted text.")])
    runtime, _, session_id = await make_runtime(services, model)
    run, _ = await runtime.submit(session_id, "find the untrusted sample")
    result = await runtime.execute(run["id"])
    second_call = model.calls[1]["messages"]
    tool_message = next(message for message in second_call if message["role"] == "tool")
    assert "Ignore previous instructions" in tool_message["content"]
    assert result.answer == "The mock result contains untrusted text."


async def test_summary_failures_use_two_attempts_then_fall_back(services):
    store, resources, registry = services
    session_id = await store.create_session()
    for index in range(6):
        old, _ = await store.start_run(session_id, f"old-{index}", None)
        await store.add_message(session_id, old["id"], "user", {"content": f"fact {index} " + "x" * 1000})
        await store.add_message(session_id, old["id"], "assistant", {"content": f"noted {index} " + "y" * 1000})
        await store.finish_run(old["id"], "completed", "noted")

    class SummaryFailureModel:
        def __init__(self):
            self.calls = []

        async def complete(self, messages, tools, *, tool_choice="auto"):
            self.calls.append({"messages": messages, "tools": tools, "tool_choice": tool_choice})
            if messages[0]["content"].startswith("请忠实摘要"):
                raise httpx.ConnectError("offline")
            return final("continued with bounded history")

    model = SummaryFailureModel()
    runtime = AgentRuntime(store, registry, resources, lambda _: (model, "native", 5000, 500))
    run, _ = await runtime.submit(session_id, "continue")
    result = await runtime.execute(run["id"])
    trace = await store.list_trace(run["id"])
    summary_starts = [item for item in trace if item["event_type"] == "model.started" and item["payload"]["phase"] == "summary"]
    assert result.status == "completed"
    assert len(summary_starts) == 2
    assert len(model.calls) == 3
    assert await store.latest_summary(session_id) is None
    assert any(item["event_type"] == "context.compaction_failed" and item["payload"]["summary_calls"] == 2 for item in trace)


async def test_todo_and_tool_record_roll_back_together(services, monkeypatch):
    store, _, registry = services
    model = ScriptedModel([tool("todo-rollback", "todo", '{"action":"add","text":"must roll back"}')])
    runtime, _, session_id = await make_runtime(services, model)

    async def fail_record(*args, **kwargs):
        raise RuntimeError("injected_record_failure")

    monkeypatch.setattr(store, "save_tool_call", fail_record)
    run, _ = await runtime.submit(session_id, "add a todo")
    result = await runtime.execute(run["id"])
    listed = await registry.execute("todo", {"action": "list"}, runtime_context("check", session_id))
    assert result.status == "failed"
    assert listed.data["items"] == []


async def test_conflicting_reused_call_id_is_bounded(services):
    model = ScriptedModel([
        tool("same", "calculator", '{"expression":"1+1"}'),
        tool("same", "calculator", '{"expression":"2+2"}'),
        tool("same", "calculator", '{"expression":"3+3"}'),
        tool("same", "calculator", '{"expression":"4+4"}'),
    ])
    runtime, _, session_id = await make_runtime(services, model)
    run, _ = await runtime.submit(session_id, "keep conflicting")
    result = await runtime.execute(run["id"])
    assert result.status == "failed"
    assert result.error["code"] == "model_protocol_error"
    assert len(model.calls) == 4


class StreamingModel:
    """只实现 stream 的假模型：验证"边收边写"的落库与收尾标记。"""

    def __init__(self, chunks: list[str], answer: str) -> None:
        self.chunks, self.answer = chunks, answer

    async def stream(self, messages, tools, *, tool_choice="auto"):
        for text in self.chunks:
            yield {"type": "content", "text": text}
        yield {"type": "done", "raw": final(self.answer)}


async def test_streamed_answer_is_persisted_once_and_not_marked_incomplete(services):
    """流式落库结束后库里只有一条完整回答，且不残留 incomplete 标记。

    incomplete 只在"流到一半"的中间态为真，它是给中断恢复用的：如果收尾时忘了摘掉，
    接口层和后续上下文都会把正常回答当成没写完的内容。
    """
    model = StreamingModel(["你好", "，世界"], "你好，世界")
    runtime, store, session_id = await make_runtime(services, model)
    run, _ = await runtime.submit(session_id, "打个招呼")
    result = await runtime.execute(run["id"])
    assert result.status == "completed" and result.answer == "你好，世界"
    assert await store.list_messages(session_id) == [
        {"role": "user", "seq": 1, "run_id": run["id"], "content": "打个招呼"},
        {"role": "assistant", "seq": 2, "run_id": run["id"], "content": "你好，世界"},
    ]


async def test_cancel_discards_partial_streamed_message(services):
    """流到一半被取消时半截回答必须撤回：历史里只能留下一条"已停止"的交代。"""

    class HangingStreamModel:
        async def stream(self, messages, tools, *, tool_choice="auto"):
            yield {"type": "content", "text": "这条回答还没写完"}
            await asyncio.sleep(30)
            yield {"type": "done", "raw": final("永远到不了这里")}

    runtime, store, session_id = await make_runtime(services, HangingStreamModel())
    run, _ = await runtime.submit(session_id, "给我一段长回答")
    task = asyncio.create_task(runtime.execute(run["id"]))
    await asyncio.sleep(.3)
    assert await store.request_cancel(run["id"])
    result = await asyncio.wait_for(task, timeout=3)
    assert result.status == "cancelled"
    messages = await store.list_messages(session_id)
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[-1]["content"] == "已停止本次运行，已完成的工具操作仍然保留。"


async def test_model_auth_failure_reports_stable_code_and_trace(services):
    """鉴权失败属于配置问题：要给出稳定错误码、可读文案，并在 trace 里留下原因。"""

    class UnauthorizedModel:
        async def complete(self, messages, tools, *, tool_choice="auto"):
            raise ModelServiceError("model_auth_failed", "HTTP 401")

    runtime, store, session_id = await make_runtime(services, UnauthorizedModel())
    run, _ = await runtime.submit(session_id, "hello")
    result = await runtime.execute(run["id"])
    assert result.status == "failed" and result.error["code"] == "model_auth_failed"
    assert "密钥" in result.answer
    trace = await store.list_trace(run["id"])
    failed = [item for item in trace if item["event_type"] == "model.failed"]
    assert failed and failed[-1]["payload"]["code"] == "model_auth_failed"


def runtime_context(run_id: str, session_id: str):
    from mini_agent.contracts import ExecutionContext

    return ExecutionContext(run_id, session_id, result_token_budget=200)
