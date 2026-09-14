"""``TodoStore``：待办工具的读写实现。

写入需要与消息落库同事务，因此优先复用调用方传入的连接。
"""
from typing import Any
from uuid import uuid4

from sqlalchemy import insert, select, update

from ..contracts import ExecutionContext, ToolResult
from .schema import todos, utc_now
from .store import Store


class TodoStore:
    """待办工具的实现。写入需要与消息落库同事务，因此优先复用传入的连接。"""
    def __init__(self, store: Store) -> None:
        self.store = store

    async def handler(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:
        # 有外部连接就复用（与工具结果消息同事务）；否则自己开一个事务。
        if ctx.db_connection is not None:
            return await self._handle(args, ctx, ctx.db_connection)
        async with self.store.engine.begin() as conn:
            return await self._handle(args, ctx, conn)

    async def _handle(self, args: dict[str, Any], ctx: ExecutionContext, conn: Any) -> ToolResult:
        action = args["action"]
        if action == "add":
            text = str(args.get("text", "")).strip()
            if not text:
                return ToolResult(False, error={
                    "code": "invalid_arguments",
                    "message": "添加待办时必须提供文本内容。",
                    "outcome": "not_executed"
                })
            todo_id = str(uuid4())
            await conn.execute(insert(todos).values(
                id=todo_id,
                session_id=ctx.session_id,
                text=text,
                status="open",
                created_at=utc_now()
            ))
            return ToolResult(True, {"todo_id": todo_id, "text": text, "status": "open"})
        if action == "list":
            # 只列当前会话的待办，会话之间互相隔离。
            rows = (await conn.execute(select(
                todos.c.id,
                todos.c.text,
                todos.c.status
            ).where(todos.c.session_id == ctx.session_id).order_by(todos.c.created_at))).mappings()
            return ToolResult(True, {"items": [dict(row) for row in rows]})
        # 剩下的动作只可能是"完成待办"。
        todo_id = args.get("todo_id")
        if not todo_id:
            return ToolResult(False, error={
                "code": "invalid_arguments",
                "message": "完成待办时必须提供待办 ID。",
                "outcome": "not_executed"
            })
        # 条件里带上 session_id：防止跨会话改到别人的待办。
        result = await conn.execute(update(todos).where(
            todos.c.id == todo_id,
            todos.c.session_id == ctx.session_id
        ).values(status="completed"))
        return ToolResult(True, {"todo_id": todo_id, "status": "completed"}) if result.rowcount else ToolResult(
            False,
            error={"code": "todo_not_found", "message": "找不到指定的待办事项。", "outcome": "failed"}
        )
