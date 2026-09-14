"""持久层：SQLite（默认）/Postgres（通过 Alembic 迁移）之上的数据访问。

包含三个类：

- ``Store``：会话、运行、消息、工具调用、摘要、轨迹事件的主体读写；
- ``TodoStore``：待办工具的实现（依赖"与消息同事务"的写入路径）；
- ``ResourceStore``：把大文本存成磁盘文件 + 数据库索引，支持分页读取与检索。

约定：
- 所有写操作都走 ``engine.begin()``（自带提交/回滚），读操作走 ``engine.connect()``；
- 需要"工具写入与消息落库同生共死"时，由调用方传入 ``connection`` 复用同一个事务。
"""
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

# 表结构在这里以 Core 方式声明（不引入 ORM 映射），与实际 DDL 的权威来源是 migrations/。
metadata = MetaData()
# 会话：一次对话的容器，绑定一个模型与一个时区。
sessions = Table("sessions", metadata, Column("id", String, primary_key=True), Column("model_name", String, nullable=False), Column("timezone", String, nullable=False), Column("title", String, nullable=True), Column("created_at", String, nullable=False))
# 运行：一条用户消息对应一次 run。(session_id, request_key) 唯一 —— 幂等去重的落点。
runs = Table("runs", metadata, Column("id", String, primary_key=True), Column("session_id", ForeignKey("sessions.id"), nullable=False, index=True), Column("request_key", String), Column("input_hash", String, nullable=False), Column("input_preview", Text, nullable=False), Column("model_name", String, nullable=False), Column("status", String, nullable=False), Column("answer", Text), Column("error", Text), Column("cancel_requested", Integer, nullable=False, default=0), Column("model_calls", Integer, nullable=False, default=0), Column("created_at", String, nullable=False), Column("finished_at", String), UniqueConstraint("session_id", "request_key"))
# 消息：payload 存 JSON（content / tool_calls / thinking 等）。seq 是会话内递增序号，前端按它定位消息。
messages = Table("messages", metadata, Column("id", String, primary_key=True), Column("session_id", ForeignKey("sessions.id"), nullable=False, index=True), Column("run_id", ForeignKey("runs.id")), Column("seq", Integer, nullable=False), Column("role", String, nullable=False), Column("payload", Text, nullable=False), Column("created_at", String, nullable=False), UniqueConstraint("session_id", "seq"))
# 工具调用：(run_id, call_id) 联合主键，同一个调用重复出现时可以查回来做"复用"提示。
tool_calls = Table("tool_calls", metadata, Column("run_id", ForeignKey("runs.id"), primary_key=True), Column("call_id", String, primary_key=True), Column("name", String, nullable=False), Column("arguments", Text, nullable=False), Column("result", Text), Column("status", String, nullable=False))
# 摘要：version 递增保留历史版本，covered_through_seq 记录"已压缩到哪一条消息"。
summaries = Table("summaries", metadata, Column("id", String, primary_key=True), Column("session_id", ForeignKey("sessions.id"), nullable=False, index=True), Column("version", Integer, nullable=False), Column("covered_through_seq", Integer, nullable=False), Column("content", Text, nullable=False), Column("created_at", String, nullable=False), UniqueConstraint("session_id", "version"))
# 待办：按会话隔离。
todos = Table("todos", metadata, Column("id", String, primary_key=True), Column("session_id", ForeignKey("sessions.id"), nullable=False, index=True), Column("text", Text, nullable=False), Column("status", String, nullable=False), Column("created_at", String, nullable=False))
# 资源：正文存磁盘文件（file_key），这里只留索引与大小。
resources = Table("resources", metadata, Column("id", String, primary_key=True), Column("session_id", ForeignKey("sessions.id"), nullable=False, index=True), Column("kind", String, nullable=False), Column("file_key", String, nullable=False), Column("size", Integer, nullable=False), Column("created_at", String, nullable=False))
# 轨迹事件：执行日志的数据源，id 自增保证按发生顺序读取。
trace_events = Table("trace_events", metadata, Column("id", Integer, primary_key=True, autoincrement=True), Column("run_id", ForeignKey("runs.id"), nullable=False, index=True), Column("event_type", String, nullable=False), Column("payload", Text, nullable=False), Column("created_at", String, nullable=False))

def utc_now() -> str:
    """统一的时间戳格式（ISO 8601 UTC），字符串排序即时间排序。"""
    return datetime.now(timezone.utc).isoformat()


def migrate_database(db_path: Path) -> None:
    """把数据库升级到最新迁移版本。

    用 Alembic 而不是 ``create_all``，保证本地 SQLite 与线上 Postgres 走同一套 DDL 演进。
    注意这里用的是同步连接串：迁移在应用启动前跑，不需要 async。
    """
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
        def configure_sqlite(dbapi_connection: Any, _: Any) -> None:
            # 每条新建连接都要设一遍 PRAGMA：这些选项是"连接级"的，不设就只有默认值。
            cursor = dbapi_connection.cursor()
            # WAL：读写互不阻塞。客户端异常断开时（例如取消 SSE 流），连接可能在关闭途中被取消，
            # 残留一个未释放的读事务；回滚日志模式（delete）下这会永久堵死后续所有写提交，
            # 表现为创建会话等写接口持续 500「database is locked」。WAL 下读不再阻塞写，可规避该故障。
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
            except Exception:
                pass
            # NORMAL 在 WAL 下是推荐档：进程崩溃不丢数据，只在断电时可能丢最后一次提交。
            cursor.execute("PRAGMA synchronous=NORMAL")
            # 打开外键约束（SQLite 默认关闭），否则删除会话时不会级联报错。
            cursor.execute("PRAGMA foreign_keys=ON")
            # 写锁冲突时最多等 5 秒再报错，避免瞬时并发直接失败。
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    async def init(self) -> None:
        """启动时收尾：把上次进程留下的"运行中"状态标记为已中断。

        服务重启后内存里的执行任务已经不存在，这些 run 永远不会再有结果；
        留着 running 会让前端一直转圈、也会让会话被判定为忙碌而无法再发消息。
        """
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
        # 标题来自用户消息首行，先压平换行并截断，避免把多行内容塞进标题栏。
        title = title.replace("\n", " ").replace("\r", " ").strip()[:200]
        if not title:
            return
        async with self.engine.begin() as conn:
            await conn.execute(update(sessions).where(sessions.c.id == session_id).values(title=title))

    async def delete_session(self, session_id: str) -> bool:
        """删除会话及其全部从属数据。返回 False 表示会话正在跑，拒绝删除。"""
        async with self.engine.begin() as conn:
            # 有活跃运行时不允许删除，否则正在执行的 run 会写进已被清空的数据里。
            active = await conn.scalar(select(func.count()).select_from(runs).where(runs.c.session_id == session_id, runs.c.status.in_(["running", "cancel_requested"])))
            if active:
                return False
            # 删除顺序必须由"叶子"到"根"：先删引用 runs 的表，再删 messages/runs，最后删会话，
            # 否则会被外键约束挡住。
            await conn.execute(delete(tool_calls).where(tool_calls.c.run_id.in_(select(runs.c.id).where(runs.c.session_id == session_id))))
            await conn.execute(delete(trace_events).where(trace_events.c.run_id.in_(select(runs.c.id).where(runs.c.session_id == session_id))))
            await conn.execute(delete(messages).where(messages.c.session_id == session_id))
            await conn.execute(delete(runs).where(runs.c.session_id == session_id))
            await conn.execute(delete(summaries).where(summaries.c.session_id == session_id))
            await conn.execute(delete(todos).where(todos.c.session_id == session_id))
            result = await conn.execute(delete(sessions).where(sessions.c.id == session_id))
            return bool(result.rowcount)

    async def list_sessions(self) -> list[dict[str, Any]]:
        # 按创建时间倒序：最新的会话排在最前面。
        async with self.engine.connect() as conn:
            rows = (await conn.execute(select(sessions).order_by(sessions.c.created_at.desc()))).mappings()
            return [dict(row) for row in rows]

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(sessions).where(sessions.c.id == session_id))).mappings().first()
            return dict(row) if row else None

    async def set_session_model(self, session_id: str, model_name: str) -> bool:
        """切换会话所用模型；同样在会话忙碌时拒绝。"""
        async with self.engine.begin() as conn:
            active = await conn.scalar(select(func.count()).select_from(runs).where(runs.c.session_id == session_id, runs.c.status.in_(["running", "cancel_requested"])))
            if active:
                return False
            result = await conn.execute(update(sessions).where(sessions.c.id == session_id).values(model_name=model_name))
            return bool(result.rowcount)

    async def add_message(self, session_id: str, run_id: str | None, role: str, payload: dict[str, Any], connection: Any = None) -> int:
        """追加一条消息并返回它的 seq。

        seq 在会话内单调递增：既用于排序，也是前端的消息定位键（SSE 推送靠它对上号）。
        传入 ``connection`` 时可以并入调用方的事务（工具写入 + 消息落库要么同时成功、要么同时回滚）。
        """
        async def write(conn: Any) -> int:
            # max(seq) + 1 在同一个写事务里计算，配合 (session_id, seq) 唯一约束避免并发下撞号。
            seq = int(await conn.scalar(select(func.coalesce(func.max(messages.c.seq), 0) + 1).where(messages.c.session_id == session_id)))
            await conn.execute(insert(messages).values(id=str(uuid4()), session_id=session_id, run_id=run_id, seq=seq, role=role, payload=json.dumps(payload, ensure_ascii=False), created_at=utc_now()))
            return seq
        if connection is not None:
            return await write(connection)
        async with self.engine.begin() as conn:
            return await write(conn)

    async def update_message_fields(self, session_id: str, seq: int, fields: dict[str, Any], connection: Any = None) -> bool:
        """按字段合并更新某条助手消息（保留未提到的字段）。

        流式输出需要"同一条消息被反复续写"：正文与思考过程分别增长，工具轮还要往同一条消息上补
        ``tool_calls``。用合并而不是整条替换，才能让这几路写入互不覆盖。
        """
        async def write(conn: Any) -> bool:
            existing = (await conn.execute(select(messages.c.payload).where(messages.c.session_id == session_id, messages.c.seq == seq, messages.c.role == "assistant"))).first()
            if not existing:
                return False
            current = json.loads(existing[0]) if existing[0] else {}
            current.update(fields)
            # 空值代表"这一路还没有内容"：直接删掉字段而不是留空值，
            # 否则界面与测试都会看到一堆无意义的空字段。
            for key, value in fields.items():
                if not value:
                    current.pop(key, None)
            result = await conn.execute(update(messages).where(messages.c.session_id == session_id, messages.c.seq == seq, messages.c.role == "assistant").values(payload=json.dumps(current, ensure_ascii=False)))
            return bool(result.rowcount)
        if connection is not None:
            return await write(connection)
        async with self.engine.begin() as conn:
            return await write(conn)

    async def update_message_content(self, session_id: str, seq: int, content: str) -> bool:
        """就地更新某条助手消息的正文（保留其余字段）。"""
        return await self.update_message_fields(session_id, seq, {"content": content})

    async def delete_message(self, session_id: str, seq: int) -> bool:
        """删除一条消息。

        流式输出是"先写后判"：内容边收边落库，等这一轮结束才知道模型给的是不是合法响应。
        判定为非法（或重试要从头再流一遍）时，必须把已经写出去的那半句撤回，
        否则界面上会留下一条幽灵回答。
        """
        async with self.engine.begin() as conn:
            result = await conn.execute(delete(messages).where(messages.c.session_id == session_id, messages.c.seq == seq))
            return bool(result.rowcount)

    async def list_messages(self, session_id: str) -> list[dict[str, Any]]:
        """按 seq 升序返回消息，并把 JSON payload 摊平到顶层（role/seq/run_id 一并带出）。"""
        async with self.engine.connect() as conn:
            rows = (await conn.execute(select(messages).where(messages.c.session_id == session_id).order_by(messages.c.seq))).mappings()
            return [{"role": row["role"], "seq": row["seq"], "run_id": row["run_id"], **json.loads(row["payload"])} for row in rows]

    async def start_run(self, session_id: str, text: str, request_key: str | None) -> tuple[dict[str, Any], bool]:
        """创建一次运行，返回 (run, 是否新建)。

        幂等：带相同 ``request_key`` 重复提交时直接返回已存在的 run（``created=False``），
        避免网络重试或重复点击产生两次回答。若同一个 key 却对应了不同内容，说明客户端用错了 key，
        直接报冲突而不是静默复用。
        """
        input_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        async with self.engine.begin() as conn:
            if request_key:
                existing = (await conn.execute(select(runs).where(runs.c.session_id == session_id, runs.c.request_key == request_key))).mappings().first()
                if existing:
                    if existing["input_hash"] != input_hash:
                        raise ValueError("request_key_conflict")
                    return dict(existing), False
            # 一个会话同时只能跑一个 run：并发会让消息顺序与模型上下文错乱。
            active = await conn.scalar(select(func.count()).select_from(runs).where(runs.c.session_id == session_id, runs.c.status.in_(["running", "cancel_requested"])))
            if active:
                raise RuntimeError("session_busy")
            # 模型名在创建 run 时快照下来：之后再改会话模型，不影响已经开始的运行。
            model_name = await conn.scalar(select(sessions.c.model_name).where(sessions.c.id == session_id))
            if model_name is None:
                raise LookupError("session_not_found")
            run = {"id": str(uuid4()), "session_id": session_id, "request_key": request_key, "input_hash": input_hash, "input_preview": text[:500], "model_name": model_name, "status": "running", "cancel_requested": 0, "model_calls": 0, "created_at": utc_now()}
            await conn.execute(insert(runs).values(**run))
            return run, True

    async def increment_model_calls(self, run_id: str) -> int:
        # 用 SQL 自增而不是"读-改-写"，避免计数在多处调用时丢失。
        async with self.engine.begin() as conn:
            await conn.execute(update(runs).where(runs.c.id == run_id).values(model_calls=runs.c.model_calls + 1))
            return int(await conn.scalar(select(runs.c.model_calls).where(runs.c.id == run_id)))

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(runs).where(runs.c.id == run_id))).mappings().first()
            if not row:
                return None
            value = dict(row)
            # error 以 JSON 文本落库，出库时还原成对象，方便前端直接读 error.code。
            value["error"] = json.loads(value["error"]) if value["error"] else None
            return value

    async def latest_run(self, session_id: str) -> dict[str, Any] | None:
        """取最近一次运行 —— 前端刷新后据此恢复"正在跑还是已结束"。"""
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
        """标记"请求取消"。只置标志位，由运行时在循环里自行退出，避免强杀导致状态不一致。"""
        async with self.engine.begin() as conn:
            result = await conn.execute(update(runs).where(runs.c.id == run_id, runs.c.status == "running").values(status="cancel_requested", cancel_requested=1))
            return bool(result.rowcount)

    async def is_cancel_requested(self, run_id: str) -> bool:
        # 运行时每轮循环前查一次，作为协作式取消的检查点。
        async with self.engine.connect() as conn:
            return bool(await conn.scalar(select(runs.c.cancel_requested).where(runs.c.id == run_id)))

    async def finish_run(self, run_id: str, status: str, answer: str = "", error: dict[str, Any] | None = None) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(update(runs).where(runs.c.id == run_id).values(status=status, answer=answer, error=json.dumps(error, ensure_ascii=False) if error else None, finished_at=utc_now()))

    async def add_trace(self, run_id: str, event_type: str, payload: dict[str, Any], connection: Any = None) -> None:
        """记录一条执行轨迹。同样支持并入调用方事务，保证轨迹与业务数据一致。"""
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
        """整个会话的轨迹（执行日志页用），按事件 id 升序即时间顺序。"""
        async with self.engine.connect() as conn:
            rows = (await conn.execute(select(trace_events.c.id, trace_events.c.run_id, trace_events.c.event_type, trace_events.c.created_at, trace_events.c.payload).join(runs, runs.c.id == trace_events.c.run_id).where(runs.c.session_id == session_id).order_by(trace_events.c.id))).mappings()
            return [{"id": row["id"], "run_id": row["run_id"], "event_type": row["event_type"], "created_at": row["created_at"], "payload": json.loads(row["payload"])} for row in rows]

    async def get_tool_call(self, run_id: str, call_id: str) -> dict[str, Any] | None:
        """查已有的工具调用记录：模型重复请求同一个调用时，直接复用结果而不重复执行。"""
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(tool_calls).where(tool_calls.c.run_id == run_id, tool_calls.c.call_id == call_id))).mappings().first()
            return dict(row) if row else None

    async def save_tool_call(self, run_id: str, call_id: str, name: str, args: dict[str, Any], result: ToolResult, connection: Any = None) -> None:
        # 参数排序后序列化：同一组参数在不同顺序下入库存成同一串，便于比对与排查。
        statement = insert(tool_calls).values(run_id=run_id, call_id=call_id, name=name, arguments=json.dumps(args, ensure_ascii=False, sort_keys=True), result=json.dumps(result.__dict__, ensure_ascii=False), status="succeeded" if result.ok else "failed")
        if connection is not None:
            await connection.execute(statement)
            return
        async with self.engine.begin() as conn:
            await conn.execute(statement)

    async def latest_summary(self, session_id: str) -> dict[str, Any] | None:
        # 取版本号最大的那份；covered_through_seq 决定哪些历史已被摘要覆盖。
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(summaries).where(summaries.c.session_id == session_id).order_by(summaries.c.version.desc()).limit(1))).mappings().first()
            return dict(row) if row else None

    async def save_summary(self, session_id: str, covered_through_seq: int, content: str) -> None:
        # 只做追加、不覆盖旧版本，便于回溯"当时摘要成了什么"。
        async with self.engine.begin() as conn:
            version = int(await conn.scalar(select(func.coalesce(func.max(summaries.c.version), 0) + 1).where(summaries.c.session_id == session_id)))
            await conn.execute(insert(summaries).values(id=str(uuid4()), session_id=session_id, version=version, covered_through_seq=covered_through_seq, content=content, created_at=utc_now()))

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
                return ToolResult(False, error={"code": "invalid_arguments", "message": "添加待办时必须提供文本内容。", "outcome": "not_executed"})
            todo_id = str(uuid4())
            await conn.execute(insert(todos).values(id=todo_id, session_id=ctx.session_id, text=text, status="open", created_at=utc_now()))
            return ToolResult(True, {"todo_id": todo_id, "text": text, "status": "open"})
        if action == "list":
            # 只列当前会话的待办，会话之间互相隔离。
            rows = (await conn.execute(select(todos.c.id, todos.c.text, todos.c.status).where(todos.c.session_id == ctx.session_id).order_by(todos.c.created_at))).mappings()
            return ToolResult(True, {"items": [dict(row) for row in rows]})
        # 剩下的动作只可能是"完成待办"。
        todo_id = args.get("todo_id")
        if not todo_id:
            return ToolResult(False, error={"code": "invalid_arguments", "message": "完成待办时必须提供待办 ID。", "outcome": "not_executed"})
        # 条件里带上 session_id：防止跨会话改到别人的待办。
        result = await conn.execute(update(todos).where(todos.c.id == todo_id, todos.c.session_id == ctx.session_id).values(status="completed"))
        return ToolResult(True, {"todo_id": todo_id, "status": "completed"}) if result.rowcount else ToolResult(False, error={"code": "todo_not_found", "message": "找不到指定的待办事项。", "outcome": "failed"})

class ResourceStore:
    """大文本的存取：磁盘文件 + 数据库索引。

    典型来源是"工具结果太大"时转存，之后模型用 resource_read / resource_search 分页取用，
    避免一次性把大段内容塞进上下文。
    """
    MAX_BYTES = 10 * 1024 * 1024

    def __init__(self, store: Store, root: Path) -> None:
        self.store, self.root = store, root
        self.root.mkdir(parents=True, exist_ok=True)

    async def save(self, session_id: str, content: str, kind: str = "text") -> str:
        data = content.encode("utf-8")
        if len(data) > self.MAX_BYTES:
            raise ValueError("resource_too_large")
        resource_id, file_key = str(uuid4()), f"{uuid4()}.txt"
        # 先写临时文件再原子替换：中途失败不会留下半截文件被后续读取。
        final_path, temp_path = self.root / file_key, self.root / f".{file_key}.tmp"
        await asyncio.to_thread(temp_path.write_bytes, data)
        await asyncio.to_thread(temp_path.replace, final_path)
        try:
            async with self.store.engine.begin() as conn:
                await conn.execute(insert(resources).values(id=resource_id, session_id=session_id, kind=kind, file_key=file_key, size=len(data), created_at=utc_now()))
        except Exception:
            # 索引写失败就把刚落的文件删掉，避免出现"有文件没记录"的孤儿。
            await asyncio.to_thread(final_path.unlink, missing_ok=True)
            raise
        return resource_id

    async def delete_for_session(self, session_id: str) -> int:
        """删除会话关联的全部资源文件与索引，返回文件数。"""
        rows = []
        async with self.store.engine.connect() as conn:
            rows = (await conn.execute(select(resources.c.file_key).where(resources.c.session_id == session_id))).fetchall()
        # 先删文件：文件删不掉不影响数据库记录的清理（反之会留下孤儿文件）。
        for (file_key,) in rows:
            try:
                (self.root / file_key).unlink(missing_ok=True)
            except OSError:
                pass
        async with self.store.engine.begin() as conn:
            await conn.execute(delete(resources).where(resources.c.session_id == session_id))
        return len(rows)

    async def _load(self, resource_id: str, session_id: str) -> str | None:
        # 查询条件带上 session_id：资源不可跨会话读取。
        async with self.store.engine.connect() as conn:
            row = (await conn.execute(select(resources.c.file_key).where(resources.c.id == resource_id, resources.c.session_id == session_id))).first()
        if not row:
            return None
        try:
            return await asyncio.to_thread((self.root / row[0]).read_text, encoding="utf-8")
        except FileNotFoundError:
            # 索引还在但文件被外部删掉了，按"找不到"处理而不是抛错。
            return None

    async def read_handler(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:
        """按游标分页读取资源内容。"""
        content = await self._load(args["resource_id"], ctx.session_id)
        if content is None:
            return ToolResult(False, error={"code": "resource_not_found", "message": "找不到指定资源，或资源不属于当前会话。", "outcome": "failed"})
        # token 预算换算成字符数（≈1 token 3 字符），再扣掉一点信封开销，保证返回结果不超预算。
        budget_chars = max(64, (ctx.result_token_budget or 4000) * 3 - 384)
        cursor, requested = args.get("cursor", 0), min(args.get("limit", 4000), budget_chars, 20_000)
        chunk = content[cursor:cursor + requested]
        next_cursor = cursor + len(chunk)
        # next_cursor 为 None 明确的告诉模型"已经读完了"，避免它无谓地继续翻页。
        return ToolResult(True, {"content": chunk, "cursor": cursor, "next_cursor": next_cursor if next_cursor < len(content) else None, "end": next_cursor >= len(content)})

    async def search_handler(self, args: dict[str, Any], ctx: ExecutionContext) -> ToolResult:
        """在资源里定位关键词，返回带上下文的片段与下一次搜索的起点。"""
        content = await self._load(args["resource_id"], ctx.session_id)
        if content is None:
            return ToolResult(False, error={"code": "resource_not_found", "message": "找不到指定资源，或资源不属于当前会话。", "outcome": "failed"})
        start = content.find(args["query"], args.get("cursor", 0))
        if start < 0:
            return ToolResult(True, {"matches": [], "next_cursor": None})
        budget_chars = max(64, (ctx.result_token_budget or 800) * 3 - 384)
        # 片段长度也受预算约束：片段 = 关键词 + 左右各一半上下文。
        context_chars = max(32, min(400, budget_chars - len(args["query"])))
        left, right = max(0, start - context_chars // 2), min(len(content), start + len(args["query"]) + context_chars // 2)
        # next_cursor 指向本次匹配之后，便于模型继续找下一处。
        return ToolResult(True, {"matches": [{"position": start, "snippet": content[left:right]}], "next_cursor": start + len(args["query"])})
