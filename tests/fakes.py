from typing import Any


class ScriptedModel:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def complete(self, messages, tools, *, tool_choice="auto"):
        self.calls.append({"messages": messages, "tools": tools, "tool_choice": tool_choice})
        if not self.responses:
            raise AssertionError("fake model has no response")
        return self.responses.pop(0)


def final(text: str, reasoning: str = "") -> dict[str, Any]:
    message: dict[str, Any] = {"content": text}
    if reasoning:
        message["reasoning_content"] = reasoning
    return {"choices": [{"finish_reason": "stop", "message": message}]}


def tool(call_id: str, name: str, arguments: str, content: str | None = None, reasoning: str = "") -> dict[str, Any]:
    message: dict[str, Any] = {"content": content, "tool_calls": [{
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments}
    }]}
    if reasoning:
        message["reasoning_content"] = reasoning
    return {"choices": [{"finish_reason": "tool_calls", "message": message}]}
