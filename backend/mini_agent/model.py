"""模型协议层：把服务商返回的原始响应解析成运行时认识的 ModelEvent。

支持两套协议（由 ``mode`` 决定）：

- ``native``：原生 function calling，响应就是 OpenAI 风格结构；
- ``json``：要求模型输出固定 JSON（``{"action": "final"|"tool_call", ...}``）。

本层的核心原则是"宁可报可恢复错误，也不猜测"。任何不符合协议的地方都返回 ``Invalid`` 并带稳定
错误码，由运行时决定重试或要求模型重答；空白正文会被单独识别（``empty_response``），因为思考型
模型在长上下文下会偶发只吐空白且正常结束，此时必须换更短的上下文，而不是原地重试。
"""
import json
import re
from collections.abc import AsyncIterator
from typing import Any, Protocol
from uuid import uuid4

import httpx

from .contracts import Final, Invalid, ModelEvent, ToolCall
from .errors import ModelServiceError

# 值得重试的状态码：限流与服务端暂时故障。其余 4xx 属于配置或参数问题，重试没有意义。
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

def _check_response(response: httpx.Response) -> None:
    """校验状态码，把失败分成"可重试"与"不可重试且带稳定错误码"两类。

    可重试的仍抛 httpx 异常，交给运行时的有限重试统一处理；鉴权与参数类失败归一成
    ``ModelServiceError``，这样上层能给出可读原因，而不是笼统的"执行失败"。
    """
    if response.status_code in RETRYABLE_STATUS:
        response.raise_for_status()
    if response.is_error:
        code = "model_auth_failed" if response.status_code in {401, 403} else "model_request_invalid"
        raise ModelServiceError(code, f"HTTP {response.status_code}")

def _decode_json(response: httpx.Response) -> dict[str, Any]:
    """解析 JSON 响应；非 JSON 正文（例如被中间层换成了 HTML 错误页）归为协议错误。"""
    try:
        data = response.json()
    except ValueError as exc:
        # response.json() 内部抛 JSONDecodeError，它是 ValueError 的子类。
        raise ModelServiceError("model_protocol_error", f"响应不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ModelServiceError("model_protocol_error", "响应不是 JSON 对象")
    return data

class ModelClient(Protocol):
    """模型客户端的结构类型；测试里用假实现替换，无需继承。"""
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        tool_choice: str = "auto"
    ) -> dict[str, Any]: ...

def _strict_object(value: str) -> dict[str, Any]:
    """严格解析工具参数 JSON。

    比 ``json.loads`` 更严：重复字段直接报错（避免后者静默覆盖前者），并禁止 NaN/Infinity
    （它们不是合法 JSON，放进去会让下游工具拿到脏数据）。
    """
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in items:
            if key in result:
                raise ValueError(f"JSON 中存在重复字段：{key}")
            result[key] = item
        return result
    parsed = json.loads(value, object_pairs_hook=pairs, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    if not isinstance(parsed, dict):
        raise ValueError("工具参数必须是 JSON 对象")
    return parsed

# 首行决策说明的识别规则：容忍「思考」「决策说明」「决策」三种叫法，中英文冒号都接受。
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
        # 模型没按约定走：不硬拆，整段都当回答正文。
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
    """把原始响应解析成 Final / ToolCall / Invalid 三选一。"""
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
                # 带上正文开头，便于从 trace 直接看出模型到底回了什么。
                return Invalid("model_protocol_error", f"{exc}；正文开头：{text[:120]!r}")
            action = payload.get("action")
            if not isinstance(action, str):
                return Invalid("model_protocol_error", "JSON 响应缺少 action")
            ds_raw = payload.get("decision_summary")
            ds = ds_raw.strip() if isinstance(ds_raw, str) else ""
            if not ds:
                # 模型没给决策摘要时，退回服务商私有推理的前 500 字（仅用于界面展示）。
                ds = reasoning[:500]
            if action == "final":
                answer = payload.get("answer")
                if isinstance(answer, str) and answer.strip():
                    return Final(answer.strip(), ds[:500])
                return Invalid("model_protocol_error", "JSON final 缺少合法 answer")
            if action == "tool_call":
                tool = payload.get("tool")
                if not isinstance(tool, dict) or not isinstance(
                    tool.get("name"),
                    str
                ) or not isinstance(tool.get("arguments"), dict):
                    return Invalid("model_protocol_error", "JSON tool_call 字段不合法")
                # JSON 协议不返回 call_id，本地生成一个即可（同一轮内唯一）。
                return ToolCall(str(uuid4()), tool["name"], tool["arguments"], ds[:500], reasoning)
            return Invalid("model_protocol_error", f"JSON action 未知：{action}")
        choice = raw["choices"][0]
        if choice.get("finish_reason") == "length":
            return Invalid("model_truncated", "模型输出被截断")
        message = choice["message"]
        reasoning = str(message.get("reasoning_content") or "").strip()
        calls = message.get("tool_calls") or []
        raw_content = message.get("content")
        note, body = split_decision_note(raw_content if isinstance(raw_content, str) else "")
        if calls:
            # 只允许单工具调用：串行执行比并发更容易保证"工具结果与消息落在同一事务"。
            if len(calls) != 1:
                return Invalid("multiple_tool_calls", "每次最多只能调用一个工具")
            call = calls[0]
            function = call["function"]
            # 决策说明优先用首行约定；模型没写就退回正文、再退回私有推理。
            thinking = (note or body or reasoning)[:500]
            return ToolCall(call["id"], function["name"], _strict_object(function["arguments"]), thinking, reasoning)
        if body:
            return Final(body, (note or reasoning)[:500])
        # 既没有工具调用、也没有正文：典型的"模型退化"，由运行时换最小上下文重试。
        return Invalid("empty_response", "模型没有返回回答或工具调用")
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        # 结构缺失/类型不对一律归为协议错误，交给上层决定重试策略。
        return Invalid("model_protocol_error", str(exc))

class HttpModelClient:
    """基于 HTTP 的模型客户端，直接对接 OpenAI 兼容接口。"""
    def __init__(
        self,
        endpoint: str,
        model: str,
        api_key: str,
        mode: str = "native",
        timeout: float = 60,
        max_tokens: int = 2048,
        transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.endpoint, self.model, self.api_key, self.mode, self.timeout, self.max_tokens = (
            endpoint, model, api_key, mode, timeout, max_tokens
        )
        # transport 是给测试用的注入点（MockTransport）；生产路径保持 None，走 httpx 默认实现。
        self.transport = transport

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        tool_choice: str = "auto"
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "max_tokens": self.max_tokens}
        # native 走工具协议；json 走 response_format 约束。两者不要混用。
        if self.mode == "native":
            payload.update({
                "tools": tools,
                "tool_choice": tool_choice,
                "parallel_tool_calls": False
            })
        else:
            payload["response_format"] = {"type": "json_object"}
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            response = await client.post(
                self.endpoint,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload
            )
            # 先判状态码再解析正文：4xx/5xx 的正文通常是错误说明，不是模型输出。
            _check_response(response)
            data = _decode_json(response)
        if self.mode == "native":
            # native 保留原始结构，parse_response 认识它的 choices/message 形态。
            return data
        # 逐一取字段而不是直接下标：服务商偶发返回结构不全时，也要给稳定错误码而不是 KeyError。
        choices = data.get("choices") or []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ModelServiceError("model_protocol_error", "响应缺少 choices[0].message")
        # finish_reason 与 usage 必须一并带出，否则无法判断「空白正文」是截断还是模型退化。
        return {
            "content": message.get("content"),
            "reasoning_content": message.get("reasoning_content") or "",
            "finish_reason": choice.get("finish_reason"),
            "usage": data.get("usage"),
        }

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        tool_choice: str = "auto"
    ) -> AsyncIterator[dict[str, Any]]:
        """流式调用模型：逐块产出增量，最后产出一份与 ``complete()`` 同构的完整响应。

        产出三类 chunk：

        - ``{"type": "reasoning", "text": ...}`` 私有推理增量（``reasoning_content``），只用于界面展示；
        - ``{"type": "content", "text": ...}`` 回答正文增量；
        - ``{"type": "done", "raw": {...}}`` 拼装好的完整响应，交给 ``parse_response`` 做协议判定。

        关键点：**协议判定仍然只做一次，并且只用最终拼装结果**。增量只是"边收边给前端看"，
        真正决定"这是终答还是工具调用"的依旧是 ``parse_response``——否则流式会让协议判定分裂成两套逻辑。

        ``tool_calls`` 在流式协议里是分片下发的（同一个 index 的 name/arguments 会在多个 chunk 里续接），
        因此这里按 index 归并后再拼字符串，不能简单覆盖。
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True}
        }
        if self.mode == "native":
            payload.update({"tools": tools, "tool_choice": tool_choice, "parallel_tool_calls": False})
        else:
            payload["response_format"] = {"type": "json_object"}
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        usage: dict[str, Any] | None = None
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            async with client.stream(
                "POST",
                self.endpoint,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload
            ) as response:
                # 流式响应同样要先校验状态码，否则 4xx/5xx 会被当成"空流"静默走完。
                _check_response(response)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        # 上游偶发的心跳/注释行：跳过而不是让整次请求失败。
                        continue
                    if isinstance(chunk.get("usage"), dict):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        finish_reason = choice.get("finish_reason") or finish_reason
                        delta = choice.get("delta") or {}
                        reasoning = delta.get("reasoning_content")
                        if reasoning:
                            reasoning_parts.append(reasoning)
                            yield {"type": "reasoning", "text": reasoning}
                        content = delta.get("content")
                        if content:
                            content_parts.append(content)
                            yield {"type": "content", "text": content}
                        for call in delta.get("tool_calls") or []:
                            index = int(call.get("index") or 0)
                            slot = calls.setdefault(index, {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""}
                            })
                            if call.get("id"):
                                slot["id"] = call["id"]
                            function = call.get("function") or {}
                            # name 一般只在首个分片出现，arguments 则会被切成很多片续接。
                            if function.get("name"):
                                slot["function"]["name"] += function["name"]
                            if function.get("arguments"):
                                slot["function"]["arguments"] += function["arguments"]
        message: dict[str, Any] = {
            "content": "".join(content_parts) or None,
            "reasoning_content": "".join(reasoning_parts)
        }
        if calls:
            message["tool_calls"] = [calls[index] for index in sorted(calls)]
        if self.mode == "native":
            yield {"type": "done", "raw": {"choices": [{
                "finish_reason": finish_reason,
                "message": message
            }], "usage": usage}}
        else:
            # JSON 模式摊平成与 complete() 一致的形态，parse_response 只认这一套字段。
            yield {"type": "done", "raw": {
                "content": message["content"],
                "reasoning_content": message["reasoning_content"],
                "finish_reason": finish_reason,
                "usage": usage
            }}

