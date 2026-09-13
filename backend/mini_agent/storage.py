import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table, Text, UniqueConstraint, delete, event, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .contracts import ExecutionContext, ToolResult

metadata = MetaData()
sessions = Table("sessions", metadata, Column("id", String, primary_key=True), Column("model_name", String, nullable=False), Column("timezone", String, nullable=False), Column("title", String, nullable=True), Column("created_at", String, nullable=False))
runs = Table("runs", metadata, Column("id", String, primary_key=True), Column("session_id", ForeignKey("sessions.id"), nullable=False, index=True), Column("request_key", String), Column("input_hash", String, nullable=False), Column("input_preview", Text, nullable=False), Column("model_name", String, nullable=False), Column("status", String, nullable=False), Column("answer", Text), Column("error", Text), Column("cancel_requested", Integer, nullable=False, default=0), Column("model_calls", Integer, nullable=False, default=0), Column("created_at", String, nullable=False), Column("finished_at", String), UniqueConstraint("session_id", "request_key"))
messages = Table("messages", metadata, Column("id", String, primary_key=True), Column("session_id", ForeignKey("sessions.id"), nullable=False, index=True), Column("run_id", ForeignKey("runs.id")), Column("seq", Integer, nullable=False), Column("role", String, nullable=False), Column("payload", Text, nullable=False), Column("created_at", String, nullable=False), UniqueConstraint("session_id", "seq"))
tool_calls = Table("tool_calls", metadata, Column("run_id", ForeignKey("runs.id"), primary_key=True), Column("call_id", String, primary_key=True), Column("name", String, nullable=False), Column("arguments", Text, nullable=False), Column("result", Text), Column("status", String, nullable=False))
summaries = Table("summaries", metadata, Column("id", String, primary_key=True), Column("session_id", ForeignKey("sessions.id"), nullable=False, index=True), Column("version", Integer, nullable=False), Column("covered_through_seq", Integer, nullable=False), Column("content", Text, nullable=False), Column("created_at", String, nullable=False), UniqueConstraint("session_id", "version"))
todos = Table("todos", metadata, Column("id", String, primary_key=True), Column("session_id", ForeignKey("sessions.id"), nullable=False, index=True), Column("text", Text, nullable=False), Column("status", String, nullable=False), Column("created_at", String, nullable=False))
resources = Table("resources", metadata, Column("id", String, primary_key=True), Column("session_id", ForeignKey("sessions.id"), nullable=False, index=True), Column("kind", String, nullable=False), Column("file_key", String, nullable=False), Column("size", Integer, nullable=False), Column("created_at", String, nullable=False))
trace_events = Table("trace_events", metadata, Column("id", Integer, primary_key=True, autoincrement=True), Column("run_id", ForeignKey("runs.id"), nullable=False, index=True), Column("event_type", String, nullable=False), Column("payload", Text, nullable=False), Column("created_at", String, nullable=False))

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def migrate_database(db_path: Path) -> None:
    backend_dir = Path(__file__).resolve().parents[1]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "migrations"))
    config.attributes["connection_url"] = f"sqlite:///{db_path.resolve().as_posix()}"
    command.upgrade(config, "head")


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.engine: AsyncEngine = create_async_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}")
        @event.listens_for(self.engine.sync_engine, "connect")
        def enable_foreign_keys(dbapi_connection: Any, _: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    async def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with self.engine.begin() as conn:
            await conn.execute(update(runs).where(runs.c.status.in_(["running", "cancel_requested"])).values(status="interrupted", error=json.dumps({"code": "service_restarted"}), finished_at=utc_now()))

    async def close(self) -> None:
        await self.engine.dispose()

    async def create_session(self, model_name: str = "default", timezone_name: str = "Asia/Shanghai", title: str | None = None) -> str:
        session_id = str(uuid4())
        async with self.engine.begin() as conn:
            await conn.execute(insert(sessions).values(id=session_id, model_name=model_name, timezone=timezone_name, title=title, created_at=utc_now()))
        return session_id

    async def count_messages(self, session_id: str) -> int:
        async with self.engine.connect() as conn:
            return int(await conn.scalar(select(func.count()).select_from(messages).where(messages.c.session_id == session_id)))

    async def set_session_title(self, session_id: str, title: str) -> None:
        title = title.replace("\n", " ").replace("\r", " ").strip()[:200]
        if not title:
            return
        async with self.engine.begin() as conn:
            await conn.execute(update(sessions).where(sessions.c.id == session_id).values(title=title))

    async def delete_session(self, session_id: str) -> bool:
        async with self.engine.begin() as conn:
            active = await conn.scalar(select(func.count()).select_from(runs).where(runs.c.session_id == session_id, runs.c.status.in_(["running", "cancel_requested"])))
            if active:
                return False
            await conn.execute(delete(tool_calls).where(tool_calls.c.run_id.in_(select(runs.c.id).where(runs.c.session_id == session_id))))
            await conn.execute(delete(trace_events).where(trace_events.c.run_id.in_(select(runs.c.id).where(runs.c.session_id == session_id))))
            await conn.execute(delete(messages).where(messages.c.session_id == session_id))
            await conn.execute(delete(runs).where(runs.c.session_id == session_id))
            await conn.execute(delete(summaries).where(summaries.c.session_id == session_id))
            await conn.execute(delete(todos).where(todos.c.session_id == session_id))
            result = await conn.execute(delete(sessions).where(sessions.c.id == session_id))
            return bool(result.rowcount)

    async def list_sessions(self) -> list[dict[str, Any]]:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(select(sessions).order_by(sessions.c.created_at.desc()))).mappings()
            return [dict(row) for row in rows]

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(sessions).where(sessions.c.id == session_id))).mappings().first()
            return dict(row) if row else None

    async def set_session_model(self, session_id: str, model_name: str) -> bool:
        async with self.engine.begin() as conn:
            active = await conn.scalar(select(func.count()).select_from(runs).where(runs.c.session_id == session_id, runs.c.status.in_(["running", "cancel_requested"])))
            if active:
                return False
            result = await conn.execute(update(sessions).where(sessions.c.id == session_id).values(model_name=model_name))
            return bool(result.rowcount)

    async def add_message(self, session_id: str, run_id: str | None, role: str, payload: dict[str, Any], connection: Any = None) -> int:
        async def write(conn: Any) -> int:
            seq = int(await conn.scalar(select(func.coalesce(func.max(messages.c.seq), 0) + 1).where(messages.c.session_id == session_id)))
            await conn.execute(insert(messages).values(id=str(uuid4()), session_id=session_id, run_id=run_id, seq=seq, role=role, payload=json.dumps(payload, ensure_ascii=False), created_at=utc_now()))
            return seq
        if connection is not None:
            return await write(connection)
        async with self.engine.begin() as conn:
            return await write(conn)

    async def update_message_content(self, session_id: str, seq: int, content: str) -> bool:
        async with self.engine.begin() as conn:
            result = await conn.execute(update(messages).where(messages.c.session_id == session_id, messages.c.seq == seq, messages.c.role == "assistant").values(payload=json.dumps({"content": content}, ensure_ascii=False)))
            return bool(result.rowcount)

    async def list_messages(self, session_id: str) -> list[dict[str, Any]]:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(select(messages).where(messages.c.session_id == session_id).order_by(messages.c.seq))).mappings()
            return [{"role": row["role"], "seq": row["seq"], "run_id": row["run_id"], **json.loads(row["payload"])} for row in rows]

    async def start_run(self, session_id: str, text: str, request_key: str | None) -> tuple[dict[str, Any], bool]:
        input_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        async with self.engine.begin() as conn:
            if request_key:
                existing = (await conn.execute(select(runs).where(runs.c.session_id == session_id, runs.c.request_key == request_key))).mappings().first()
                if existing:
                    if existing["input_hash"] != input_hash:
                        raise ValueError("request_key_conflict")
                    return dict(existing), False
            active = await conn.scalar(select(func.count()).select_from(runs).where(runs.c.session_id == session_id, runs.c.status.in_(["running", "cancel_requested"])))
            if active:
                raise RuntimeError("session_busy")
            model_name = await conn.scalar(select(sessions.c.model_name).where(sessions.c.id == session_id))
            if model_name is None:
                raise LookupError("session_not_found")
            run = {"id": str(uuid4()), "session_id": session_id, "request_key": request_key, "input_hash": input_hash, "input_preview": text[:500], "model_name": model_name, "status": "running", "cancel_requested": 0, "model_calls": 0, "created_at": utc_now()}
            await conn.execute(insert(runs).values(**run))
            return run, True

    async def increment_model_calls(self, run_id: str) -> int:
        async with self.engine.begin() as conn:
            await conn.execute(update(runs).where(runs.c.id == run_id).values(model_calls=runs.c.model_calls + 1))
            return int(await conn.scalar(select(runs.c.model_calls).where(runs.c.id == run_id)))

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(runs).where(runs.c.id == run_id))).mappings().first()
            if not row:
                return None
            value = dict(row)
            value["error"] = json.loads(value["error"]) if value["error"] else None
            return value

    async def latest_run(self, session_id: str) -> dict[str, Any] | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(runs).where(runs.c.session_id == session_id).order_by(runs.c.created_at.desc()).limit(1))).mappings().first()
            if not row:
                return None
            value = dict(row)
            value["error"] = json.loads(value["error"]) if value["error"] else None
            return value

    async def list_runs(self, session_id: str) -> list[dict[str, Any]]:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(select(runs).where(runs.c.session_id == session_id).order_by(runs.c.created_at.desc()))).mappings()
            values = []
            for row in rows:
                value = dict(row)
                value["error"] = json.loads(value["error"]) if value["error"] else None
                values.append(value)
            return values

    async def request_cancel(self, run_id: str) -> bool:
        async with self.engine.begin() as conn:
            result = await conn.execute(update(runs).where(runs.c.id == run_id, runs.c.status == "running").values(status="cancel_requested", cancel_requested=1))
            return bool(result.rowcount)

    async def is_cancel_requested(self, run_id: str) -> bool:
        async with self.engine.connect() as conn:
            return bool(await conn.scalar(select(runs.c.cancel_requested).where(runs.c.id == run_id)))

    async def finish_run(self, run_id: str, status: str, answer: str = "", error: dict[str, Any] | None = None) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(update(runs).where(runs.c.id == run_id).values(status=status, answer=answer, error=json.dumps(error, ensure_ascii=False) if error else None, finished_at=utc_now()))

    async def add_trace(self, run_id: str, event_type: str, payload: dict[str, Any], connection: Any = None) -> None:
        statement = insert(trace_events).values(run_id=run_id, event_type=event_type, payload=json.dumps(payload, ensure_ascii=False), created_at=utc_now())
        if connection is not None:
            await connection.execute(statement)
            return
        async with self.engine.begin() as conn:
            await conn.execute(statement)

    async def list_trace(self, run_id: str) -> list[dict[str, Any]]:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(select(trace_events).where(trace_events.c.run_id == run_id).order_by(trace_events.c.id))).mappings()
            return [{"id": row["id"], "event_type": row["event_type"], "created_at": row["created_at"], "payload": json.loads(row["payload"])} for row in rows]

    async def list_session_trace(self, session_id: str) -> list[dict[str, Any]]:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(select(trace_events.c.id, trace_events.c.run_id, trace_events.c.event_type, trace_events.c.created_at, trace_events.c.payload).join(runs, runs.c.id == trace_events.c.run_id).where(runs.c.session_id == session_id).order_by(trace_events.c.id))).mappings()
            return [{"id": row["id"], "run_id": row["run_id"], "event_type": row["event_type"], "created_at": row["created_at"], "payload": json.loads(row["payload"])} for row in rows]

    async def get_tool_call(self, run_id: str, call_id: str) -> dict[str, Any] | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(tool_calls).where(tool_calls.c.run_id == run_id, tool_calls.c.call_id == call_id))).mappings().first()
            return dict(row) if row else None

    async def save_tool_call(self, run_id: str, call_id: str, name: str, args: dict[str, Any], result: ToolResult, connection: Any = None) -> None:
        statement = insert(tool_calls).values(run_id=run_id, call_id=call_id, name=name, arguments=json.dumps(args, ensure_ascii=False, sort_keys=True), result=json.dumps(result.__dict__, ensure_ascii=False), status="succeeded" if result.ok else "failed")
        if connection is not None:
            await connection.execute(statement)
            return
        async with self.engine.begin() as conn:
            await conn.execute(statement)

    async def latest_summary(self, session_id: str) -> dict[str, Any] | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(summaries).where(summaries.c.session_id == session_id).order_by(summaries.c.version.desc()).limit(1))).mappings().first()
            return dict(row) if row else None

    async def save_summary(self, session_id: str, covered_through_seq: int, content: str) -> None:
        async with self.engine.begin() as conn:
            version = int(await conn.scalar(select(func.coalesce(func.max(summaries.c.version), 0) + 1).where(summaries.c.session_id == session_id)))
            await conn.execute(insert(summaries).values(id=str(uuid4()), session_id=session_id, version=version, covered_through_seq=covered_through_seq, content=content, created_at=utc_now()))

class TodoStore:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def handler(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:
        if ctx.db_connection is not None:
            return await self._handle(args, ctx, ctx.db_connection)
        async with self.store.engine.begin() as conn:
            return await self._handle(args, ctx, conn)

    async def _handle(self, args: dict[str, Any], ctx: ExecutionContext, conn: Any) -> ToolResult:
        action = args["action"]
        if action == "add":
            text = str(args.get("text", "")).strip()
            if not text:
                return ToolResult(False, error={"code": "invalid_arguments", "message": "添加待办时必须提供文本内容。", "outcome": "not_executed"})
            todo_id = str(uuid4())
            await conn.execute(insert(todos).values(id=todo_id, session_id=ctx.session_id, text=text, status="open", created_at=utc_now()))
            return ToolResult(True, {"todo_id": todo_id, "text": text, "status": "open"})
        if action == "list":
            rows = (await conn.execute(select(todos.c.id, todos.c.text, todos.c.status).where(todos.c.session_id == ctx.session_id).order_by(todos.c.created_at))).mappings()
            return ToolResult(True, {"items": [dict(row) for row in rows]})
        todo_id = args.get("todo_id")
        if not todo_id:
            return ToolResult(False, error={"code": "invalid_arguments", "message": "完成待办时必须提供待办 ID。", "outcome": "not_executed"})
        result = await conn.execute(update(todos).where(todos.c.id == todo_id, todos.c.session_id == ctx.session_id).values(status="completed"))
        return ToolResult(True, {"todo_id": todo_id, "status": "completed"}) if result.rowcount else ToolResult(False, error={"code": "todo_not_found", "message": "找不到指定的待办事项。", "outcome": "failed"})

class ResourceStore:
    MAX_BYTES = 10 * 1024 * 1024

    def __init__(self, store: Store, root: Path) -> None:
        self.store, self.root = store, root
        self.root.mkdir(parents=True, exist_ok=True)

    async def save(self, session_id: str, content: str, kind: str = "text") -> str:
        data = content.encode("utf-8")
        if len(data) > self.MAX_BYTES:
            raise ValueError("resource_too_large")
        resource_id, file_key = str(uuid4()), f"{uuid4()}.txt"
        final_path, temp_path = self.root / file_key, self.root / f".{file_key}.tmp"
        await asyncio.to_thread(temp_path.write_bytes, data)
        await asyncio.to_thread(temp_path.replace, final_path)
        try:
            async with self.store.engine.begin() as conn:
                await conn.execute(insert(resources).values(id=resource_id, session_id=session_id, kind=kind, file_key=file_key, size=len(data), created_at=utc_now()))
        except Exception:
            await asyncio.to_thread(final_path.unlink, missing_ok=True)
            raise
        return resource_id

    async def delete_for_session(self, session_id: str) -> int:
        rows = []
        async with self.store.engine.connect() as conn:
            rows = (await conn.execute(select(resources.c.file_key).where(resources.c.session_id == session_id))).fetchall()
        for (file_key,) in rows:
            try:
                (self.root / file_key).unlink(missing_ok=True)
            except OSError:
                pass
        async with self.store.engine.begin() as conn:
            await conn.execute(delete(resources).where(resources.c.session_id == session_id))
        return len(rows)

    async def _load(self, resource_id: str, session_id: str) -> str | None:
        async with self.store.engine.connect() as conn:
            row = (await conn.execute(select(resources.c.file_key).where(resources.c.id == resource_id, resources.c.session_id == session_id))).first()
        if not row:
            return None
        try:
            return await asyncio.to_thread((self.root / row[0]).read_text, encoding="utf-8")
        except FileNotFoundError:
            return None

    async def read_handler(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:
        content = await self._load(args["resource_id"], ctx.session_id)
        if content is None:
            return ToolResult(False, error={"code": "resource_not_found", "message": "找不到指定资源，或资源不属于当前会话。", "outcome": "failed"})
        budget_chars = max(64, (ctx.result_token_budget or 4000) * 3 - 384)
        cursor, requested = args.get("cursor", 0), min(args.get("limit", 4000), budget_chars, 20_000)
        chunk = content[cursor:cursor + requested]
        next_cursor = cursor + len(chunk)
        return ToolResult(True, {"content": chunk, "cursor": cursor, "next_cursor": next_cursor if next_cursor < len(content) else None, "end": next_cursor >= len(content)})

    async def search_handler(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:
        content = await self._load(args["resource_id"], ctx.session_id)
        if content is None:
            return ToolResult(False, error={"code": "resource_not_found", "message": "找不到指定资源，或资源不属于当前会话。", "outcome": "failed"})
        start = content.find(args["query"], args.get("cursor", 0))
        if start < 0:
            return ToolResult(True, {"matches": [], "next_cursor": None})
        budget_chars = max(64, (ctx.result_token_budget or 800) * 3 - 384)
        context_chars = max(32, min(400, budget_chars - len(args["query"])))
        left, right = max(0, start - context_chars // 2), min(len(content), start + len(args["query"]) + context_chars // 2)
        return ToolResult(True, {"matches": [{"position": start, "snippet": content[left:right]}], "next_cursor": start + len(args["query"])})
