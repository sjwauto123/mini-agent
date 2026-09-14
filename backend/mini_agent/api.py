"""HTTP 接口层：FastAPI 应用装配与全部 REST/SSE 路由。

分工：
- ``AppServices`` 持有存储、工具注册表与运行时这些单例，并负责把"运行"丢到后台任务；
- ``create_app`` 完成装配（含单实例锁、静态前端托管）并声明路由；
- 路由只做"校验参数 → 调用领域对象 → 映射错误码"，业务逻辑都在 runtime/storage 里。

错误约定：对外一律返回 ``{"code": ..., "message": ...}``，其中 ``code`` 是稳定标识（来自
``errors`` 模块，前端据此展示本地化文案），``message`` 只是兜底。异常到错误码的转换统一走
``error_code_of``，本层不再自己拆字符串——那样会让同一个失败在不同层得出不同的码。
"""
import asyncio
import json
import logging
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
from .errors import KNOWN_CODES, SUBMIT_HTTP_STATUS, answer_for, error_code_of
from .events import RunEventBus
from .model import HttpModelClient, ModelClient
from .runtime import AgentRuntime
from .storage import ResourceStore, Store, TodoStore
from .tools import build_registry

# 终态集合：运行落到这些状态后，SSE 就可以收尾、前端可以释放输入框。
TERMINAL = {"completed", "failed", "cancelled", "limit_reached", "interrupted"}
# 运行记录里不对外暴露的服务端字段：哈希与幂等键只用于内部去重。
INTERNAL_RUN_FIELDS = frozenset({"input_hash", "request_key"})

logger = logging.getLogger(__name__)


def error_detail(code: str) -> dict[str, str]:
    """构造统一的错误响应体。文案取自 errors 模块，本层不再维护第二份表。"""
    return {"code": code, "message": answer_for(code, "请求处理失败，请稍后重试。")}


def _submission_status(code: str) -> int:
    """提交运行时：错误码 → HTTP 状态。

    未收录的码说明这是未预期的内部失败，按 500 上报——不能把服务端 bug 说成用户输入有问题；
    已收录但没特别约定状态的（缺消息、资源过大等）按请求内容问题处理，回 400。
    """
    status = SUBMIT_HTTP_STATUS.get(code)
    if status is not None:
        return status
    return 400 if code in KNOWN_CODES else 500


def public_run(run: dict[str, Any] | None) -> dict[str, Any] | None:
    """剔除服务端内部字段后再把运行记录交给客户端。"""
    if run is None:
        return None
    return {key: value for key, value in run.items() if key not in INTERNAL_RUN_FIELDS}


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
        # 运行事件总线：运行时把流式增量投给它、SSE 路由订阅它。两者由此解耦——
        # 增量不再需要"先落库、再等接口层轮询发现"，推送粒度也就不再被轮询间隔锁死。
        self.events = RunEventBus()
        # 记住进行中的任务，既避免被垃圾回收，也便于关闭时统一取消。
        self.tasks: dict[str, asyncio.Task[Any]] = {}

        def model_factory(name: str) -> tuple[ModelClient, str, int, int]:
            """按模型名构造客户端；测试通过 model_overrides 注入假实现。"""
            model_config = config.models.get(name)
            if not model_config:
                # 用 ValueError 而不是 LookupError：后者在领域里专指"按标识查不到会话/运行"，
                # 混用会让"模型未配置"被映射成 404 会话不存在。
                raise ValueError(f"model_not_configured: {name}")
            client = self.model_overrides.get(name)
            if not client:
                # 缺少密钥在这里就报错，错误码里带上环境变量名，方便定位配置问题。
                if not model_config.api_key:
                    raise RuntimeError(f"model_api_key_missing: {model_config.api_key_env}")
                client = HttpModelClient(
                    model_config.endpoint,
                    model_config.model,
                    model_config.api_key,
                    model_config.mode,
                    config.model_timeout,
                    model_config.output_reserve
                )
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
            events=self.events,
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
        return [{
            "name": item.name,
            "model": item.model,
            "mode": item.mode,
            "context_window": item.context_window
        } for item in svc(request).config.models.values()]

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
        # 忙碌判定必须排在删除动作之前：被拒绝的请求不能留下任何副作用。
        # 数据行的删除在一个事务里完成（含资源索引，见 Store.delete_session），
        # 磁盘文件只能在事务提交之后清理——反过来会留下"有记录、无文件"的不可读资源。
        purged = await svc(request).store.delete_session(session_id)
        if purged is None:
            raise HTTPException(409, detail=error_detail("session_busy"))
        await svc(request).resources.purge_files(purged)
        return {"id": session_id, "deleted": True}

    @app.patch("/api/sessions/{session_id}")
    async def session_update(session_id: str, body: SessionUpdate, request: Request) -> dict[str, Any]:
        if body.model_name not in svc(request).config.models:
            raise HTTPException(400, detail=error_detail("model_not_configured"))
        # 这个接口没有副作用，所以"不存在"与"忙碌"可以分开报：前者 404、后者 409，
        # 客户端不必靠猜来区分两种完全不同的处置方式。
        if not await svc(request).store.get_session(session_id):
            raise HTTPException(404, detail=error_detail("session_not_found"))
        if not await svc(request).store.set_session_model(session_id, body.model_name):
            raise HTTPException(409, detail=error_detail("session_busy"))
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
        except Exception as exc:
            # 归一化成稳定错误码后再决定状态码：同一个失败不能因为异常类型不同而时 404 时 409。
            # 会话冲突 → 409；请求内容问题 → 400；服务端配置/依赖问题 → 503。
            code = error_code_of(exc)
            logger.warning("session %s run submission rejected: %s", session_id, code)
            raise HTTPException(_submission_status(code), detail=error_detail(code))
        # 幂等命中时不重复启动，避免同一条消息跑两遍。
        if created:
            svc(request).launch(run["id"])
        return {"run_id": run["id"], "created": created, "status": run["status"]}

    @app.get("/api/runs/{run_id}")
    async def run_get(run_id: str, request: Request) -> dict[str, Any]:
        run = public_run(await svc(request).store.get_run(run_id))
        if not run:
            raise HTTPException(404, detail=error_detail("run_not_found"))
        return run

    @app.get("/api/sessions/{session_id}/runs/latest")
    async def latest_run(session_id: str, request: Request) -> dict[str, Any] | None:
        # 前端刷新后用这个恢复"正在跑 / 已结束"，可能没有运行记录，因此允许返回 null。
        if not await svc(request).store.get_session(session_id):
            raise HTTPException(404, detail=error_detail("session_not_found"))
        return public_run(await svc(request).store.latest_run(session_id))

    @app.get("/api/sessions/{session_id}/runs")
    async def run_list(session_id: str, request: Request) -> list[dict[str, Any]]:
        if not await svc(request).store.get_session(session_id):
            raise HTTPException(404, detail=error_detail("session_not_found"))
        runs = await svc(request).store.list_runs(session_id)
        return [public_run(run) or {} for run in runs]

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
            queue = svc(request).events.subscribe(run_id)
            try:
                while True:
                    # 优先投递总线上的增量：它代表"运行时刚刚产出的内容"，只需一次内存传递，
                    # 因此回答是逐字生长，而不是等下一次轮询才被看见。
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=.2)
                    except asyncio.TimeoutError:
                        event = None
                    if event is not None:
                        if event.get("type") == "delta":
                            message = {
                                "seq": event.get("seq"),
                                "content": event.get("content") or "",
                                "thinking": event.get("thinking") or ""
                            }
                            # 工具轮在结束的那一刻就带上 tool_calls：前端据此立即改显示成
                            # "调用了 N 个工具"，而不会继续把决策说明当回答渲染（那会与思考面板重复）。
                            if event.get("tool_calls"):
                                message["tool_calls"] = event["tool_calls"]
                            payload = json.dumps(message, ensure_ascii=False)
                            if payload != previous_message:
                                yield f"event: message\ndata: {payload}\n\n"
                                previous_message = payload
                        elif event.get("type") == "discard":
                            # 这一轮被作废（协议非法，或重试要从头再流一遍）：让前端把该条消息撤掉。
                            yield (
                                "event: discard\ndata: "
                                f"{json.dumps({'seq': event.get('seq')}, ensure_ascii=False)}\n\n"
                            )
                        elif event.get("type") == "notice":
                            # 过程性提示（如"上游无响应，正在重试"）：不改变任何数据，
                            # 只是让长时间没有增量的沉默期在界面上有解释。
                            notice = {key: value for key, value in event.items() if key != "type"}
                            yield f"event: notice\ndata: {json.dumps(notice, ensure_ascii=False)}\n\n"
                    # 增量之外再对一次数据库：运行状态快照与终态收尾始终以数据库为唯一真相。
                    run = public_run(await svc(request).store.get_run(run_id))
                    if not run:
                        return
                    snapshot = json.dumps(run, ensure_ascii=False, sort_keys=True)
                    if snapshot != previous:
                        yield f"event: snapshot\ndata: {snapshot}\n\n"
                        previous = snapshot
                    if run["status"] in TERMINAL:
                        # 终态做一次权威推送：落库有节流，增量可能少了最后几片，数据库里的一定是完整内容。
                        # 载荷带 seq，前端据此精确定位要更新的那条消息，不做“最后一条助手消息”的猜测。
                        # 工具轮的正文是决策说明而非答案，这里照旧不当作回答推送。
                        messages = await svc(request).store.list_messages(run["session_id"])
                        assistant = next((
                            item for item in reversed(messages)
                            if item.get("run_id") == run_id and item.get("role") == "assistant"
                        ), None)
                        if assistant is not None:
                            content = "" if assistant.get("tool_calls") else (assistant.get("content") or "")
                            thinking = assistant.get("thinking") or ""
                            if content or thinking:
                                payload = json.dumps({
                                    "seq": assistant.get("seq"),
                                    "content": content,
                                    "thinking": thinking
                                }, ensure_ascii=False)
                                if payload != previous_message:
                                    yield f"event: message\ndata: {payload}\n\n"
                        return
            except Exception:
                # 推送过程中出错时，客户端只会看到连接断开，所以原因必须留在服务端日志里；
                # 运行状态本身已落库，前端重连或重新拉快照都能拿到终态，不需要在这里补发事件。
                logger.exception("run %s event stream aborted", run_id)
            finally:
                # 客户端断开（生成器被关闭）时必须退订，否则频道里会一直留着一个没人读的队列。
                svc(request).events.unsubscribe(run_id, queue)

        # no-cache：SSE 必须禁掉中间层缓存，否则推送会被缓冲住。
        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.post("/api/sessions/{session_id}/resources", status_code=201)
    async def resource_create(session_id: str, body: ResourceCreate, request: Request) -> dict[str, Any]:
        if not await svc(request).store.get_session(session_id):
            raise HTTPException(404, detail=error_detail("session_not_found"))
        try:
            resource_id = await svc(request).resources.save(session_id, body.content, body.kind)
        except ValueError as exc:
            # 归一化出错误码再决定状态码：超大资源用 413，其余（如 kind 非法）用 400。
            code = error_code_of(exc)
            status_code = 413 if code == "resource_too_large" else 400
            raise HTTPException(status_code, detail=error_detail(code))
        return {"resource_id": resource_id, "kind": body.kind, "size": len(body.content.encode("utf-8"))}

    # 生产式运行：把前端构建产物挂到根路径。开发时目录不存在，就只提供 API。
    frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if frontend_dist.exists():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")

    return app


# 供 `uvicorn mini_agent.api:app` 直接使用。
app = create_app()
