"""``python -m mini_agent`` 的入口：直接以本机地址起服务。

只绑定 127.0.0.1（配合 API 层的 TrustedHost 中间件），默认不对外开放；
``reload=False`` 是有意的：开发期用 uvicorn CLI 自行加 --reload 更灵活，
而这里的默认行为要贴近生产（避免热重载中断正在执行的运行）。
"""
import uvicorn

uvicorn.run("mini_agent.api:app", host="127.0.0.1", port=8000, reload=False)
