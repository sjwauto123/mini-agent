"""运行路由：提交消息、查询运行与轨迹、取消运行。

这一组路径跨两个资源前缀（``/api/runs/...`` 与 ``/api/sessions/{id}/runs...``），
所以 router 只声明公共前缀 ``/api``，各自写全子路径。
"""
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..errors import error_code_of
from .common import RunCreate, error_detail, public_run, services, submission_status

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["runs"])


@router.post("/sessions/{session_id}/runs", status_code=202)
async def run_create(session_id: str, body: RunCreate, request: Request) -> dict[str, Any]:
    """提交一条消息。返回 202 —— 此时只是"受理"，回答要靠 SSE 或轮询获取。"""
    try:
        run, created = await services(request).runtime.submit(session_id, body.message, body.request_key)
    except Exception as exc:
        # 归一化成稳定错误码后再决定状态码：同一个失败不能因为异常类型不同而时 404 时 409。
        # 会话冲突 → 409；请求内容问题 → 400；服务端配置/依赖问题 → 503。
        code = error_code_of(exc)
        logger.warning("session %s run submission rejected: %s", session_id, code)
        raise HTTPException(submission_status(code), detail=error_detail(code))
    # 幂等命中时不重复启动，避免同一条消息跑两遍。
    if created:
        services(request).launch(run["id"])
    return {"run_id": run["id"], "created": created, "status": run["status"]}


@router.get("/runs/{run_id}")
async def run_get(run_id: str, request: Request) -> dict[str, Any]:
    run = public_run(await services(request).store.get_run(run_id))
    if not run:
        raise HTTPException(404, detail=error_detail("run_not_found"))
    return run


@router.get("/sessions/{session_id}/runs/latest")
async def latest_run(session_id: str, request: Request) -> dict[str, Any] | None:
    # 前端刷新后用这个恢复"正在跑 / 已结束"，可能没有运行记录，因此允许返回 null。
    if not await services(request).store.get_session(session_id):
        raise HTTPException(404, detail=error_detail("session_not_found"))
    return public_run(await services(request).store.latest_run(session_id))


@router.get("/sessions/{session_id}/runs")
async def run_list(session_id: str, request: Request) -> list[dict[str, Any]]:
    if not await services(request).store.get_session(session_id):
        raise HTTPException(404, detail=error_detail("session_not_found"))
    runs = await services(request).store.list_runs(session_id)
    return [public_run(run) or {} for run in runs]


@router.get("/sessions/{session_id}/trace")
async def session_trace(session_id: str, request: Request) -> list[dict[str, Any]]:
    if not await services(request).store.get_session(session_id):
        raise HTTPException(404, detail=error_detail("session_not_found"))
    return await services(request).store.list_session_trace(session_id)


@router.post("/runs/{run_id}/cancel", status_code=202)
async def run_cancel(run_id: str, request: Request) -> dict[str, Any]:
    # accepted=False 也可能是"已经结束了"，所以要先确认 run 是否存在才能报 404。
    accepted = await services(request).store.request_cancel(run_id)
    if not accepted and not await services(request).store.get_run(run_id):
        raise HTTPException(404, detail=error_detail("run_not_found"))
    return {"run_id": run_id, "accepted": accepted}


@router.get("/runs/{run_id}/trace")
async def trace_get(run_id: str, request: Request) -> list[dict[str, Any]]:
    if not await services(request).store.get_run(run_id):
        raise HTTPException(404, detail=error_detail("run_not_found"))
    return await services(request).store.list_trace(run_id)
