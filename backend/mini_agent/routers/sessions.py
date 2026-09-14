"""会话路由：会话的创建/列表/删除/改模型，以及会话历史消息读取。

删除会话会连带清理它名下的磁盘资源，因此这里的顺序要求是刚性的：
被拒绝的请求不留任何副作用，而数据行的删除必须早于磁盘文件清理（理由见 ``Store.delete_session``）。
"""
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Request

from .common import SessionCreate, SessionUpdate, error_detail, services

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.post("", status_code=201)
async def session_create(body: SessionCreate, request: Request) -> dict[str, Any]:
    if body.model_name not in services(request).config.models:
        raise HTTPException(400, detail=error_detail("model_not_configured"))
    # 时区字符串由客户端提供，先验证再落库，否则后续算"今天"时会炸。
    try:
        ZoneInfo(body.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(400, detail=error_detail("timezone_invalid"))
    session_id = await services(request).store.create_session(body.model_name, body.timezone)
    return {"id": session_id, "model_name": body.model_name, "timezone": body.timezone, "title": None}


@router.get("")
async def session_list(request: Request) -> list[dict[str, Any]]:
    return await services(request).store.list_sessions()


@router.delete("/{session_id}")
async def session_delete(session_id: str, request: Request) -> dict[str, Any]:
    if not await services(request).store.get_session(session_id):
        raise HTTPException(404, detail=error_detail("session_not_found"))
    # 忙碌判定必须排在删除动作之前：被拒绝的请求不能留下任何副作用。
    # 数据行的删除在一个事务里完成（含资源索引，见 Store.delete_session），
    # 磁盘文件只能在事务提交之后清理——反过来会留下"有记录、无文件"的不可读资源。
    purged = await services(request).store.delete_session(session_id)
    if purged is None:
        raise HTTPException(409, detail=error_detail("session_busy"))
    await services(request).resources.purge_files(purged)
    return {"id": session_id, "deleted": True}


@router.patch("/{session_id}")
async def session_update(session_id: str, body: SessionUpdate, request: Request) -> dict[str, Any]:
    if body.model_name not in services(request).config.models:
        raise HTTPException(400, detail=error_detail("model_not_configured"))
    # 这个接口没有副作用，所以"不存在"与"忙碌"可以分开报：前者 404、后者 409，
    # 客户端不必靠猜来区分两种完全不同的处置方式。
    if not await services(request).store.get_session(session_id):
        raise HTTPException(404, detail=error_detail("session_not_found"))
    if not await services(request).store.set_session_model(session_id, body.model_name):
        raise HTTPException(409, detail=error_detail("session_busy"))
    return {"id": session_id, "model_name": body.model_name}


@router.get("/{session_id}/messages")
async def message_list(session_id: str, request: Request) -> list[dict[str, Any]]:
    if not await services(request).store.get_session(session_id):
        raise HTTPException(404, detail=error_detail("session_not_found"))
    return await services(request).store.list_messages(session_id)
