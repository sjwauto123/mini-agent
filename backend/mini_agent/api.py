"""HTTP 接口层：FastAPI 应用装配与全部 REST/SSE 路由。

分工：
- ``AppServices`` 持有存储、工具注册表与运行时这些单例，并负责把"运行"丢到后台任务；
- ``create_app`` 完成装配（含单实例锁、静态前端托管）并声明路由；
- 路由只做"校验参数 → 调用领域对象 → 映射错误码"，业务逻辑都在 runtime/storage 里。

错误约定：对外一律返回 ``{"code": ..., "message": ...}``，其中 ``code`` 是稳定标识，
前端据此展示本地化文案；``message`` 只是兜底。
"""
import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import portalocker
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import AppConfig, load_config
from .model import HttpModelClient, ModelClient
from .runtime import AgentRuntime
from .storage import ResourceStore, Store, TodoStore
from .tools import build_registry

# 终态集合：运行落到这些状态后，SSE 就可以收尾、前端可以释放输入框。
TERMINAL = {"completed", "failed", "cancelled", "limit_reached", "interrupted"}
# 本层直接产生的错误码文案（运行时的错误码由前端另行映射）。
ERROR_MESSAGES = {
    "model_not_configured": "模型未配置。",
    "timezone_invalid": "时区配置无效。",
    "session_busy_or_missing": "当前会话正在运行或会话不存在。",
    "session_not_found": "会话不存在。",
    "session_busy": "当前会话正在处理其他消息，请稍后再试。",
    "run_not_found": "运行记录不存在。",
    "resource_too_large": "资源内容过大。",
    "request_key_conflict": "请求标识已被其他消息使用。",
    "message_required": "请输入消息。",
    "model_context_budget_invalid": "模型上下文配置无效。",
}


def error_detail(code: str) -> dict[str, str]:
    return {"code": code, "message": ERROR_MESSAGES.get(code, "请求处理失败，请稍后重试。")}


class SessionCreate(BaseModel):
    model_name: str
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=128)


class SessionUpdate(BaseModel):
    model_name: str


class RunCreate(BaseModel):
    # max_length 放宽到 10MB：超长消息由运行时转存为资源，而不是在这里被拒。
    message: str = Field(min_length=1, max_length=10_485_760)
    request_key: str | None = Field(default=None, max_length=128)


class ResourceCreate(BaseModel):
    content: str
    kind: str = Field(default="text", pattern="^(text|json)$")


class AppServices:
    """应用级单例集合：存储、资源、工具、运行时，以及进行中的后台任务。"""
    def __init__(self, config: AppConfig, model_overrides: dict[str, ModelClient] | None = None) -> None:
        self.config, self.model_overrides = config, model_overrides or {}
        self.store = Store(config.data_dir / "state.db")
        self.resources = ResourceStore(self.store, config.data_dir / "resources")
        self.registry = build_registry(TodoStore(self.store), self.resources, config.tool_timeout)
        # 记住进行中的任务，既避免被垃圾回收，也便于关闭时统一取消。
        self.tasks: dict[str, asyncio.Task[Any]] = {}

        def model_factory(name: str) -> tuple[ModelClient, str, int, int]:
            """按模型名构造客户端；测试通过 model_overrides 注入假实现。"""
            model_config = config.models.get(name)
            if not model_config:
                raise LookupError(f"model_not_configured: {name}")
            client = self.model_overrides.get(name)
            if not client:
                # 缺少密钥在这里就报错，错误码里带上环境变量名，方便定位配置问题。
                if not model_config.api_key:
                    raise RuntimeError(f"model_api_key_missing: {model_config.api_key_env}")
                client = HttpModelClient(model_config.endpoint, model_config.model, model_config.api_key, model_config.mode, config.model_timeout, model_config.output_reserve)
            return client, model_config.mode, model_config.context_window, model_config.output_reserve

        self.runtime = AgentRuntime(
            self.store,
            self.registry,
            self.resources,
            model_factory,
            max_model_calls=config.max_model_calls,
            max_repairs=config.max_protocol_repairs,
            max_summary_calls=config.max_summary_calls,
            run_timeout=config.run_timeout,
            safety_margin=config.safety_margin,
            soft_context_ratio=config.soft_context_ratio,
            hard_context_ratio=config.hard_context_ratio,
            target_context_ratio=config.target_context_ratio,
        )

    def launch(self, run_id: str) -> None:
        """把运行放到后台执行，接口立刻返回。"""
        task = asyncio.create_task(self.runtime.execute(run_id), name=f"run:{run_id}")
        self.tasks[run_id] = task
        # 完成后从表里摘掉，避免任务对象越积越多。
        task.add_done_callback(lambda _: self.tasks.pop(run_id, None))


def create_app(config: AppConfig | None = None, model_overrides: dict[str, ModelClient] | None = None) -> FastAPI:
    config = config or load_config()
    services = AppServices(config, model_overrides)
    lock_path = config.data_dir / "instance.lock"

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        config.data_dir.mkdir(parents=True, exist_ok=True)
        # 单实例锁：同一个数据目录被两个进程打开会让 SQLite 竞争、状态互相覆盖。
        lock = portalocker.Lock(str(lock_path), mode="a", timeout=0)
        try:
            lock.acquire()
        except portalocker.exceptions.LockException as exc:
            raise RuntimeError(f"data directory is already in use: {config.data_dir}") from exc
        # 启动收尾：把上次残留的"运行中"标记为已中断。
        await services.store.init()
        app.state.services = services
        try:
            yield
        finally:
            # 关闭时先取消后台运行并等它们退出，再释放连接与锁，避免写半截。
            for task in list(services.tasks.values()):
                task.cancel()
            if services.tasks:
                await asyncio.gather(*services.tasks.values(), return_exceptions=True)
            await services.store.close()
            lock.release()

    app = FastAPI(title="Mini Agent", version="0.1.0", lifespan=lifespan)
    # 只接受本机主机名，避免被当成对外服务使用时出现 Host 头攻击。
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    def svc(request: Request) -> AppServices:
        return request.app.state.services

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/models")
    async def model_list(request: Request) -> list[dict[str, Any]]:
        # 不返回 endpoint / api_key_env，避免把服务端配置暴露给浏览器。
        return [{"name": item.name, "model": item.model, "mode": item.mode, "context_window": item.context_window} for item in svc(request).config.models.values()]

    @app.post("/api/sessions", status_code=201)
    async def session_create(body: SessionCreate, request: Request) -> dict[str, Any]:
        if body.model_name not in svc(request).config.models:
            raise HTTPException(400, detail=error_detail("model_not_configured"))
        # 时区字符串由客户端提供，先验证再落库，否则后续算"今天"时会炸。
        try:
            ZoneInfo(body.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(400, detail=error_detail("timezone_invalid"))
        session_id = await svc(request).store.create_session(body.model_name, body.timezone)
        return {"id": session_id, "model_name": body.model_name, "timezone": body.timezone, "title": None}

    @app.get("/api/sessions")
    async def session_list(request: Request) -> list[dict[str, Any]]:
        return await svc(request).store.list_sessions()

    @app.delete("/api/sessions/{session_id}")
    async def session_delete(session_id: str, request: Request) -> dict[str, Any]:
        if not await svc(request).store.get_session(session_id):
            raise HTTPException(404, detail=error_detail("session_not_found"))
        # 先删资源文件，再删数据行；若会话正在运行，随后的 delete_session 会返回 False 并给出 409。
        await svc(request).resources.delete_for_session(session_id)
        deleted = await svc(request).store.delete_session(session_id)
        if not deleted:
            raise HTTPException(409, detail=error_detail("session_busy"))
        return {"id": session_id, "deleted": True}

    @app.patch("/api/sessions/{session_id}")
    async def session_update(session_id: str, body: SessionUpdate, request: Request) -> dict[str, Any]:
        if body.model_name not in svc(request).config.models:
            raise HTTPException(400, detail=error_detail("model_not_configured"))
        # 会话忙碌或不存在都会返回 409，调用方无需区分（前端提示一致）。
        if not await svc(request).store.set_session_model(session_id, body.model_name):
            raise HTTPException(409, detail=error_detail("session_busy_or_missing"))
        return {"id": session_id, "model_name": body.model_name}

    @app.get("/api/sessions/{session_id}/messages")
    async def message_list(session_id: str, request: Request) -> list[dict[str, Any]]:
        if not await svc(request).store.get_session(session_id):
            raise HTTPException(404, detail=error_detail("session_not_found"))
        return await svc(request).store.list_messages(session_id)

    @app.post("/api/sessions/{session_id}/runs", status_code=202)
    async def run_create(session_id: str, body: RunCreate, request: Request) -> dict[str, Any]:
        """提交一条消息。返回 202 —— 此时只是"受理"，回答要靠 SSE 或轮询获取。"""
        try:
            run, created = await svc(request).runtime.submit(session_id, body.message, body.request_key)
        except LookupError:
            raise HTTPException(404, detail=error_detail("session_not_found"))
        except RuntimeError as exc:
            # session_busy / model_call_limit 之类都走 409。
            raise HTTPException(409, detail=error_detail(str(exc)))
        except ValueError as exc:
            raise HTTPException(400, detail=error_detail(str(exc)))
        # 幂等命中时不重复启动，避免同一条消息跑两遍。
        if created:
            svc(request).launch(run["id"])
        return {"run_id": run["id"], "created": created, "status": run["status"]}

    @app.get("/api/runs/{run_id}")
    async def run_get(run_id: str, request: Request) -> dict[str, Any]:
        run = await svc(request).store.get_run(run_id)
        if not run:
            raise HTTPException(404, detail=error_detail("run_not_found"))
        return run

    @app.get("/api/sessions/{session_id}/runs/latest")
    async def latest_run(session_id: str, request: Request) -> dict[str, Any] | None:
        # 前端刷新后用这个恢复"正在跑 / 已结束"，可能没有运行记录，因此允许返回 null。
        if not await svc(request).store.get_session(session_id):
            raise HTTPException(404, detail=error_detail("session_not_found"))
        return await svc(request).store.latest_run(session_id)

    @app.get("/api/sessions/{session_id}/runs")
    async def run_list(session_id: str, request: Request) -> list[dict[str, Any]]:
        if not await svc(request).store.get_session(session_id):
            raise HTTPException(404, detail=error_detail("session_not_found"))
        return await svc(request).store.list_runs(session_id)

    @app.get("/api/sessions/{session_id}/trace")
    async def session_trace(session_id: str, request: Request) -> list[dict[str, Any]]:
        if not await svc(request).store.get_session(session_id):
            raise HTTPException(404, detail=error_detail("session_not_found"))
        return await svc(request).store.list_session_trace(session_id)

    @app.post("/api/runs/{run_id}/cancel", status_code=202)
    async def run_cancel(run_id: str, request: Request) -> dict[str, Any]:
        # accepted=False 也可能是"已经结束了"，所以要先确认 run 是否存在才能报 404。
        accepted = await svc(request).store.request_cancel(run_id)
        if not accepted and not await svc(request).store.get_run(run_id):
            raise HTTPException(404, detail=error_detail("run_not_found"))
        return {"run_id": run_id, "accepted": accepted}

    @app.get("/api/runs/{run_id}/trace")
    async def trace_get(run_id: str, request: Request) -> list[dict[str, Any]]:
        if not await svc(request).store.get_run(run_id):
            raise HTTPException(404, detail=error_detail("run_not_found"))
        return await svc(request).store.list_trace(run_id)

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request) -> StreamingResponse:
        """以 SSE 推送运行状态与流式回答。"""
        if not await svc(request).store.get_run(run_id):
            raise HTTPException(404, detail=error_detail("run_not_found"))

        async def events() -> AsyncIterator[str]:
            previous = ""
            previous_message = ""
            while True:
                # 每 200ms 拉一次数据库状态；只在内容变化时才推送，空闲时保持连接但不发多余数据。
                run = await svc(request).store.get_run(run_id)
                if not run:
                    return
                snapshot = json.dumps(run, ensure_ascii=False, sort_keys=True)
                if snapshot != previous:
                    yield f"event: snapshot\ndata: {snapshot}\n\n"
                    previous = snapshot
                messages = await svc(request).store.list_messages(run["session_id"])
                assistant = next((item for item in reversed(messages) if item.get("run_id") == run_id and item.get("role") == "assistant"), None)
                # 本次运行还没有助手消息时什么都不推，否则前端会把上一条回答误当成流式目标。
                # 载荷带 seq，前端据此精确定位要更新的那条消息，不做“最后一条助手消息”的猜测。
                # 工具轮的正文是决策说明，结束前不当作答案流推送；只推最终回答与决策摘要。
                if assistant is not None:
                    content = "" if assistant.get("tool_calls") else (assistant.get("content") or "")
                    thinking = assistant.get("thinking") or ""
                    if content or thinking:
                        payload = json.dumps({"seq": assistant.get("seq"), "content": content, "thinking": thinking}, ensure_ascii=False)
                        if payload != previous_message:
                            yield f"event: message\ndata: {payload}\n\n"
                            previous_message = payload
                # 终态才收尾；否则继续轮询。
                if run["status"] in TERMINAL:
                    return
                await asyncio.sleep(.2)

        # no-cache：SSE 必须禁掉中间层缓存，否则推送会被缓冲住。
        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.post("/api/sessions/{session_id}/resources", status_code=201)
    async def resource_create(session_id: str, body: ResourceCreate, request: Request) -> dict[str, Any]:
        if not await svc(request).store.get_session(session_id):
            raise HTTPException(404, detail=error_detail("session_not_found"))
        try:
            resource_id = await svc(request).resources.save(session_id, body.content, body.kind)
        except ValueError as exc:
            # 超大资源用 413，其余（如 kind 非法）用 400。
            status_code = 413 if str(exc) == "resource_too_large" else 400
            raise HTTPException(status_code, detail=error_detail(str(exc)))
        return {"resource_id": resource_id, "kind": body.kind, "size": len(body.content.encode("utf-8"))}

    # 生产式运行：把前端构建产物挂到根路径。开发时目录不存在，就只提供 API。
    frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if frontend_dist.exists():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")

    return app


# 供 `uvicorn mini_agent.api:app` 直接使用。
app = create_app()
