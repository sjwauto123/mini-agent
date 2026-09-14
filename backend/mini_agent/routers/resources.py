"""资源路由：把会话里的大块内容落成资源，避免塞进消息体。

与消息路由分开，是因为资源有自己的存储（``ResourceStore``）与失败语义——
体积超限回 413，而 kind 非法这类请求内容问题回 400。
"""
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..errors import error_code_of
from .common import ResourceCreate, error_detail, services

router = APIRouter(prefix="/api/sessions", tags=["resources"])


@router.post("/{session_id}/resources", status_code=201)
async def resource_create(session_id: str, body: ResourceCreate, request: Request) -> dict[str, Any]:
    if not await services(request).store.get_session(session_id):
        raise HTTPException(404, detail=error_detail("session_not_found"))
    try:
        resource_id = await services(request).resources.save(session_id, body.content, body.kind)
    except ValueError as exc:
        # 归一化出错误码再决定状态码：超大资源用 413，其余（如 kind 非法）用 400。
        code = error_code_of(exc)
        status_code = 413 if code == "resource_too_large" else 400
        raise HTTPException(status_code, detail=error_detail(code))
    return {"resource_id": resource_id, "kind": body.kind, "size": len(body.content.encode("utf-8"))}
