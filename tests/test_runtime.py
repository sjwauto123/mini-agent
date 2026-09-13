import json
import asyncio
import re

import httpx

from mini_agent.contracts import ToolResult
from mini_agent.runtime import AgentRuntime
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


def runtime_context(run_id: str, session_id: str):
    from mini_agent.contracts import ExecutionContext

    return ExecutionContext(run_id, session_id, result_token_budget=200)
