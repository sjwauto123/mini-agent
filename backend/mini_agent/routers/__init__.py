"""HTTP 路由集合。

``create_app`` 只负责装配（lifespan、中间件、静态托管），具体接口按资源拆到各模块：
每个模块导出一个 ``router``，由 ``ALL_ROUTERS`` 统一登记。加接口只需新增模块并登记，
不必再去动 ``create_app`` 的 250 行长函数。
"""
from .resources import router as resources_router
from .runs import router as runs_router
from .sessions import router as sessions_router
from .sse import router as sse_router
from .system import router as system_router

# 登记顺序即路由注册顺序：先系统/元信息，再会话、运行、事件流、资源。
ALL_ROUTERS = (system_router, sessions_router, runs_router, sse_router, resources_router)

__all__ = ["ALL_ROUTERS"]
