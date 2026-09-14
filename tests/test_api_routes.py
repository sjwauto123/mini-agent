"""路由表契约：公开 HTTP 接口的路径、方法、处理函数名与成功状态码。

拆分 api.py、调整路由登记顺序或改动装饰器时，最容易出的错不是抛异常，而是某个接口
悄悄消失、改名或换了成功状态码——调用方要到运行时才发现。这里把整张表冻结下来，
任何增删改都必须同时更新这份清单，从而在评审里被看见。
"""
from pathlib import Path

from mini_agent.api import create_app
from mini_agent.config import AppConfig

# (方法, 路径, 处理函数名, 成功状态码)
EXPECTED_ROUTES = frozenset({
    ("GET", "/api/health", "health", 200),
    ("GET", "/api/models", "model_list", 200),
    ("POST", "/api/sessions", "session_create", 201),
    ("GET", "/api/sessions", "session_list", 200),
    ("DELETE", "/api/sessions/{session_id}", "session_delete", 200),
    ("PATCH", "/api/sessions/{session_id}", "session_update", 200),
    ("GET", "/api/sessions/{session_id}/messages", "message_list", 200),
    ("POST", "/api/sessions/{session_id}/resources", "resource_create", 201),
    ("POST", "/api/sessions/{session_id}/runs", "run_create", 202),
    ("GET", "/api/sessions/{session_id}/runs", "run_list", 200),
    ("GET", "/api/sessions/{session_id}/runs/latest", "latest_run", 200),
    ("GET", "/api/sessions/{session_id}/trace", "session_trace", 200),
    ("GET", "/api/runs/{run_id}", "run_get", 200),
    ("POST", "/api/runs/{run_id}/cancel", "run_cancel", 202),
    ("GET", "/api/runs/{run_id}/trace", "trace_get", 200),
    ("GET", "/api/runs/{run_id}/events", "run_events", 200),
})


def collected_routes(app) -> set[tuple[str, str, str, int]]:
    """把 app 上的 /api 路由收敛成与 EXPECTED_ROUTES 同形的集合。"""
    routes = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api"):
            continue
        for method in getattr(route, "methods", None) or ():
            if method in {"HEAD", "OPTIONS"}:
                continue
            # APIRoute.status_code 默认 None，语义上等价于 200。
            routes.add((method, path, route.name, getattr(route, "status_code", None) or 200))
    return routes


def test_public_route_table_is_frozen(tmp_path: Path):
    app = create_app(AppConfig(data_dir=tmp_path, models={}))
    assert collected_routes(app) == EXPECTED_ROUTES
