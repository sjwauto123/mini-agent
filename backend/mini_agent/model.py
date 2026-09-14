import json
import re
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

DECISION_PREFIX = re.compile(r"^\s*(?:思考|决策说明|决策)\s*[:：]\s*")


def split_decision_note(content: str) -> tuple[str, str]:
    """拆出原生模式的首行决策说明。

    原生工具协议没有 decision_summary 字段，因此约定模型在首行用「思考：…」给出决策说明，
    其余才是给用户的回答正文。未按约定输出时原样返回，不猜测、不伪造。
    """
    text = content.strip()
    if not text:
        return "", ""
    lines = text.splitlines()
    first = lines[0].strip()
    if not DECISION_PREFIX.match(first):
        return "", text
    return DECISION_PREFIX.sub("", first).strip(), "\n".join(lines[1:]).strip()


def _unwrap_json_text(value: str) -> str:
    """容忍模型把 JSON 包在 Markdown 代码块里，只剥掉最外层包裹，不做语义修补。"""
    text = value.strip()
    if text.startswith("```"):
        text = text[3:]
        if text[:4].lower() == "json":
            text = text[4:]
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def parse_response(raw: dict[str, Any], mode: str = "native") -> ModelEvent:
    try:
        if mode == "json":
            raw_content = raw.get("content", "")
            content = raw_content if isinstance(raw_content, str) else ""
            reasoning = str(raw.get("reasoning_content") or "").strip()
            # JSON 模式同样要把「被截断」「空白正文」「响应不合法」区分开：
            # 推理模型在长上下文下会偶发地只吐出空白正文并正常结束；同样的上下文只会重复同样的空白输出，
            # 所以必须识别出来，改用一个更短的上下文重试，否则会一直重试到耗尽修复次数。
            if raw.get("finish_reason") == "length":
                return Invalid("model_truncated", "模型输出被截断")
            if not content.strip():
                return Invalid("empty_response", "模型返回了空白正文，没有可解析的 JSON")
            text = _unwrap_json_text(content)
            try:
                payload = _strict_object(text)
            except ValueError as exc:
                return Invalid("model_protocol_error", f"{exc}；正文开头：{text[:120]!r}")
            action = payload.get("action")
            if not isinstance(action, str):
                return Invalid("model_protocol_error", "JSON 响应缺少 action")
            ds_raw = payload.get("decision_summary")
            ds = ds_raw.strip() if isinstance(ds_raw, str) else ""
            if not ds:
                ds = reasoning[:500]
            if action == "final":
                answer = payload.get("answer")
                if isinstance(answer, str) and answer.strip():
                    return Final(answer.strip(), ds[:500])
                return Invalid("model_protocol_error", "JSON final 缺少合法 answer")
            if action == "tool_call":
                tool = payload.get("tool")
                if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not isinstance(tool.get("arguments"), dict):
                    return Invalid("model_protocol_error", "JSON tool_call 字段不合法")
                return ToolCall(str(uuid4()), tool["name"], tool["arguments"], ds[:500], reasoning)
            return Invalid("model_protocol_error", f"JSON action 未知：{action}")
        choice = raw["choices"][0]
        if choice.get("finish_reason") == "length": return Invalid("model_truncated", "模型输出被截断")
        message = choice["message"]
        reasoning = str(message.get("reasoning_content") or "").strip()
        calls = message.get("tool_calls") or []
        raw_content = message.get("content")
        note, body = split_decision_note(raw_content if isinstance(raw_content, str) else "")
        if calls:
            if len(calls) != 1: return Invalid("multiple_tool_calls", "每次最多只能调用一个工具")
            call = calls[0]; function = call["function"]
            thinking = (note or body or reasoning)[:500]
            return ToolCall(call["id"], function["name"], _strict_object(function["arguments"]), thinking, reasoning)
        if body: return Final(body, (note or reasoning)[:500])
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
        if self.mode == "native":
            return data
        choice = data["choices"][0]
        message = choice["message"]
        # finish_reason 与 usage 必须一并带出，否则无法判断「空白正文」是截断还是模型退化。
        return {
            "content": message.get("content"),
            "reasoning_content": message.get("reasoning_content") or "",
            "finish_reason": choice.get("finish_reason"),
            "usage": data.get("usage"),
        }
