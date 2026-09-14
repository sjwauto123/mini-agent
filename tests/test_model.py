from mini_agent.contracts import Final, Invalid, ToolCall
from mini_agent.model import parse_response

from .fakes import final, tool


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
    raw = {"content": '{"action":"tool_call","decision_summary":"calc","tool":{"name":"calculator","arguments":{"expression":"4*5"}},"answer":null}'}
    parsed = parse_response(raw, "json")
    assert isinstance(parsed, ToolCall)
    assert parsed.name == "calculator"
    assert parsed.arguments["expression"] == "4*5"


def test_json_blank_body_is_reported_as_empty_response():
    """长上下文下推理模型可能只返回空白正文（finish_reason=stop），必须识别为可恢复的 empty_response。"""
    event = parse_response({"content": "        ", "reasoning_content": "想了一下", "finish_reason": "stop"}, "json")
    assert isinstance(event, Invalid) and event.code == "empty_response"


def test_json_accepts_fenced_object_and_flags_truncation():
    fenced = {"content": '```json\n{"action":"final","decision_summary":"","tool":null,"answer":"好了"}\n```', "finish_reason": "stop"}
    parsed = parse_response(fenced, "json")
    assert isinstance(parsed, Final) and parsed.answer == "好了"
    truncated = parse_response({"content": '{"action":"final","ans', "finish_reason": "length"}, "json")
    assert isinstance(truncated, Invalid) and truncated.code == "model_truncated"
