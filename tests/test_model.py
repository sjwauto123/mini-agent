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


def test_json_protocol():
    raw = {"content": '{"action":"tool_call","decision_summary":"calc","tool":{"name":"calculator","arguments":{"expression":"4*5"}},"answer":null}'}
    parsed = parse_response(raw, "json")
    assert isinstance(parsed, ToolCall)
    assert parsed.name == "calculator"
    assert parsed.arguments["expression"] == "4*5"
