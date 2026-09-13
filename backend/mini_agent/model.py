import json
from collections.abc import AsyncIterator
from typing import Any, Protocol
from uuid import uuid4

import httpx

from .contracts import Final, Invalid, ModelEvent, ToolCall

class ModelClient(Protocol):
    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, tool_choice: str = "auto") -> dict[str, Any]: ...

def _strict_object(value: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in items:
            if key in result: raise ValueError(f"JSON 中存在重复字段：{key}")
            result[key] = item
        return result
    parsed = json.loads(value, object_pairs_hook=pairs, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    if not isinstance(parsed, dict): raise ValueError("工具参数必须是 JSON 对象")
    return parsed

def parse_response(raw: dict[str, Any], mode: str = "native") -> ModelEvent:
    try:
        if mode == "json":
            content = raw.get("content", "")
            payload = _strict_object(content)
            expected = {"action", "decision_summary", "tool", "answer"}
            if set(payload) != expected or not isinstance(payload.get("decision_summary"), str):
                return Invalid("model_protocol_error", "JSON 响应字段不符合约定格式")
            if payload.get("action") == "final" and payload.get("tool") is None and isinstance(payload.get("answer"), str) and payload["answer"].strip():
                return Final(payload["answer"].strip())
            tool = payload.get("tool")
            if payload.get("action") == "tool_call" and payload.get("answer") is None and isinstance(tool, dict) and set(tool) == {"name", "arguments"} and isinstance(tool.get("name"), str) and isinstance(tool.get("arguments"), dict):
                return ToolCall(str(uuid4()), tool["name"], tool["arguments"], str(payload.get("decision_summary") or "")[:500])
            return Invalid("model_protocol_error", "JSON 响应格式无效")
        choice = raw["choices"][0]
        if choice.get("finish_reason") == "length": return Invalid("model_truncated", "模型输出被截断")
        message = choice["message"]
        calls = message.get("tool_calls") or []
        if calls:
            if len(calls) != 1: return Invalid("multiple_tool_calls", "每次最多只能调用一个工具")
            call = calls[0]; function = call["function"]
            return ToolCall(call["id"], function["name"], _strict_object(function["arguments"]), str(message.get("content") or "")[:500])
        content = message.get("content")
        if isinstance(content, str) and content.strip(): return Final(content.strip())
        return Invalid("empty_response", "模型没有返回回答或工具调用")
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return Invalid("model_protocol_error", str(exc))

class HttpModelClient:
    def __init__(self, endpoint: str, model: str, api_key: str, mode: str = "native", timeout: float = 60, max_tokens: int = 2048) -> None:
        self.endpoint, self.model, self.api_key, self.mode, self.timeout, self.max_tokens = endpoint, model, api_key, mode, timeout, max_tokens

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, tool_choice: str = "auto") -> dict[str, Any]:
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "max_tokens": self.max_tokens}
        if self.mode == "native": payload.update({"tools": tools, "tool_choice": tool_choice, "parallel_tool_calls": False})
        else: payload["response_format"] = {"type": "json_object"}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self.endpoint, headers={"Authorization": f"Bearer {self.api_key}"}, json=payload)
            response.raise_for_status()
            data = response.json()
        return data if self.mode == "native" else {"content": data["choices"][0]["message"]["content"]}
