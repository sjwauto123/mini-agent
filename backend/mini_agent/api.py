"""HTTP 接口层：FastAPI 应用装配与公开符号的再导出。

分工：
- ``AppServices`` 持有存储、工具注册表与运行时这些单例，并负责把"运行"丢到后台任务；
- ``routers`` 包按资源拆分路由，每个模块只做"校验参数 → 调用领域对象 → 映射错误码"，
  业务逻辑都在 runtime/storage 里；
- ``create_app`` 完成装配（单实例锁、中间件、路由登记、静态前端托管）。

错误约定：对外一律返回 ``{"code": ..., "message": ...}``，其中 ``code`` 是稳定标识（来自
``errors`` 模块，前端据此展示本地化文案），``message`` 只是兜底。异常到错误码的转换统一走
``error_code_of``，本层不再自己拆字符串——那样会让同一个失败在不同层得出不同的码。

历史兼容：``error_detail`` / ``public_run`` / ``_submission_status`` / 请求体模型等原先定义在
本模块，拆分后搬到 ``routers.common``，这里原样再导出，既有调用方（含测试）无需改动。
"""
import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import portalocker
from fastapi import FastAPI
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles

from .config import AppConfig, load_config
from .events import RunEventBus
from .model import HttpModelClient, ModelClient
from .routers import ALL_ROUTERS
from .routers.common import (
    INTERNAL_RUN_FIELDS,
    TERMINAL,
    ResourceCreate,
    RunCreate,
    SessionCreate,
    SessionUpdate,
    error_detail,
    public_run,
    submission_status as _submission_status,
)
from .runtime import AgentRuntime
from .storage import ResourceStore, Store, TodoStore
from .tools import build_registry

__all__ = [
    "AppServices",
    "INTERNAL_RUN_FIELDS",
    "ResourceCreate",
    "RunCreate",
    "SessionCreate",
    "SessionUpdate",
    "TERMINAL",
    "_submission_status",
    "app",
    "create_app",
    "error_detail",
    "public_run",
]


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

    # 路由登记：各模块只声明自己的路径，装配顺序（先 API、后静态托管）由这里统一决定。
    for router in ALL_ROUTERS:
        app.include_router(router)

    # 生产式运行：把前端构建产物挂到根路径。开发时目录不存在，就只提供 API。
    frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if frontend_dist.exists():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")

    return app


# 供 `uvicorn mini_agent.api:app` 直接使用。
app = create_app()
