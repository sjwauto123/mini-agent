"""系统与元信息路由：健康检查、可用模型清单。

这里只暴露"客户端需要知道什么"（名字、上下文窗口），不暴露服务端连接细节。
"""
from typing import Any

from fastapi import APIRouter, Request

from .common import services

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/models")
async def model_list(request: Request) -> list[dict[str, Any]]:
    # 不返回 endpoint / api_key_env，避免把服务端配置暴露给浏览器。
    return [{
        "name": item.name,
        "model": item.model,
        "mode": item.mode,
        "context_window": item.context_window
    } for item in services(request).config.models.values()]
