"""运行事件流（SSE）：把事件总线的增量与数据库快照串成一条推送。

分工要点：总线负责"运行时刚产出的内容"（一次内存传递，回答因此逐字生长）；
数据库负责"状态快照与终态收尾"（唯一真相，落库有节流时靠它补齐最后几片）。
"""
import asyncio
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from .common import TERMINAL, error_detail, public_run, services

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.get("/{run_id}/events")
async def run_events(run_id: str, request: Request) -> StreamingResponse:
    """以 SSE 推送运行状态与流式回答。"""
    if not await services(request).store.get_run(run_id):
        raise HTTPException(404, detail=error_detail("run_not_found"))

    async def events() -> AsyncIterator[str]:
        previous = ""
        previous_message = ""
        queue = services(request).events.subscribe(run_id)
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
                run = public_run(await services(request).store.get_run(run_id))
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
                    messages = await services(request).store.list_messages(run["session_id"])
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
            services(request).events.unsubscribe(run_id, queue)

    # no-cache：SSE 必须禁掉中间层缓存，否则推送会被缓冲住。
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
