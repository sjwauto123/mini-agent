import httpx
import pytest

from mini_agent.contracts import Final, Invalid, ToolCall
from mini_agent.errors import ModelServiceError
from mini_agent.model import HttpModelClient, parse_response

from .fakes import final, tool


def mock_client(handler, mode: str = "native") -> HttpModelClient:
    """构造一个不联网的 HttpModelClient：用 MockTransport 接管全部请求。"""
    return HttpModelClient(
        "https://example.invalid/v1/chat/completions",
        "test-model",
        "key",
        mode,
        transport=httpx.MockTransport(handler)
    )


def test_native_final_and_tool_call():
    answer = parse_response(final("hello"))
    call = parse_response(tool("c1", "calculator", '{"expression":"2+2"}', "checking"))
    assert isinstance(answer, Final) and answer.answer == "hello"
    assert isinstance(call, ToolCall) and call.arguments == {"expression": "2+2"}
    assert call.decision_summary == "checking"


def test_rejects_multiple_calls_and_duplicate_json_keys():
    raw = tool("c1", "calculator", '{"expression":"2+2"}')
    raw["choices"][0]["message"]["tool_calls"].append(raw["choices"][0]["message"]["tool_calls"][0])
    assert isinstance(parse_response(raw), Invalid)
    duplicate = tool("c1", "calculator", '{"expression":"2+2","expression":"3+3"}')
    assert isinstance(parse_response(duplicate), Invalid)


def test_native_decision_note_is_split_from_answer():
    """原生模式没有 decision_summary 字段，首行「思考：…」要拆成决策说明，回答正文保持干净。"""
    parsed = parse_response(final("思考：需要先问候用户。\n\n你好，我可以帮你查天气、做计算。"))
    assert isinstance(parsed, Final)
    assert parsed.answer == "你好，我可以帮你查天气、做计算。"
    assert parsed.decision_summary == "需要先问候用户。"


def test_native_answer_without_note_is_kept_verbatim():
    parsed = parse_response(final("你好，我可以帮你查天气。"))
    assert isinstance(parsed, Final)
    assert parsed.answer == "你好，我可以帮你查天气。"
    assert parsed.decision_summary == ""


def test_native_note_without_body_is_invalid():
    parsed = parse_response(final("思考：先查一下北京天气。"))
    assert isinstance(parsed, Invalid) and parsed.code == "empty_response"


def test_native_tool_call_uses_decision_note_as_thinking():
    parsed = parse_response(tool("c1", "calculator", '{"expression":"1+1"}', content="思考：需要做一次加法。"))
    assert isinstance(parsed, ToolCall) and parsed.decision_summary == "需要做一次加法。"


def test_json_protocol():
    raw = {"content": (
        '{"action":"tool_call","decision_summary":"calc",'
        '"tool":{"name":"calculator","arguments":{"expression":"4*5"}},"answer":null}'
    )}
    parsed = parse_response(raw, "json")
    assert isinstance(parsed, ToolCall)
    assert parsed.name == "calculator"
    assert parsed.arguments["expression"] == "4*5"


def test_json_blank_body_is_reported_as_empty_response():
    """长上下文下推理模型可能只返回空白正文（finish_reason=stop），必须识别为可恢复的 empty_response。"""
    event = parse_response({"content": "        ", "reasoning_content": "想了一下", "finish_reason": "stop"}, "json")
    assert isinstance(event, Invalid) and event.code == "empty_response"


def test_json_accepts_fenced_object_and_flags_truncation():
    fenced = {
        "content": '```json\n{"action":"final","decision_summary":"","tool":null,"answer":"好了"}\n```',
        "finish_reason": "stop"
    }
    parsed = parse_response(fenced, "json")
    assert isinstance(parsed, Final) and parsed.answer == "好了"
    truncated = parse_response({"content": '{"action":"final","ans', "finish_reason": "length"}, "json")
    assert isinstance(truncated, Invalid) and truncated.code == "model_truncated"


async def test_auth_and_request_failures_get_stable_codes():
    """401/403 与 400 必须区分开：前者是密钥问题，后者是配置问题，不能都变成"未知失败"。"""
    with pytest.raises(ModelServiceError) as unauthorized:
        await mock_client(lambda _: httpx.Response(401, content=b"unauthorized")).complete([], [])
    with pytest.raises(ModelServiceError) as rejected:
        await mock_client(lambda _: httpx.Response(400, content=b"bad model")).complete([], [])
    assert unauthorized.value.code == "model_auth_failed"
    assert rejected.value.code == "model_request_invalid"


async def test_retryable_status_still_raises_httpx_error():
    """429/5xx 由运行时的有限重试处理，模型层不要把它们吞成"配置错误"。"""
    with pytest.raises(httpx.HTTPStatusError):
        await mock_client(lambda _: httpx.Response(429, content=b"slow down")).complete([], [])


async def test_non_json_and_incomplete_body_are_protocol_errors():
    """200 + 非 JSON 正文（例如被中间层换成 HTML 错误页）与结构缺失都属于协议错误。"""
    with pytest.raises(ModelServiceError) as html:
        await mock_client(lambda _: httpx.Response(200, content=b"<html>bad gateway</html>"), "json").complete([], [])
    with pytest.raises(ModelServiceError) as shapeless:
        await mock_client(lambda _: httpx.Response(200, json={"id": "x"}), "json").complete([], [])
    assert html.value.code == "model_protocol_error"
    assert shapeless.value.code == "model_protocol_error"
