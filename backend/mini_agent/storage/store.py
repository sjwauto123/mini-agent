"""``Store``：会话、运行、消息、工具调用、摘要、轨迹事件的主体读写。

写操作走 ``engine.begin()``（自带提交/回滚），读操作走 ``engine.connect()``；
需要"工具写入与消息落库同生共死"时，由调用方传入 ``connection`` 复用同一个事务。
"""
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, event, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ..contracts import ToolResult
from ..errors import ERROR_ANSWERS
from .schema import (
    _inserted_id,
    messages,
    resources,
    runs,
    sessions,
    summaries,
    todos,
    tool_calls,
    trace_events,
    utc_now,
)

# 允许落库的消息角色。模型的角色由调用点决定（用户输入只会以 user 落库），这里再加一道白名单：
# model.py 是把 messages 原样透传给上游的、没有任何二次过滤，所以"会不会落进一条 role=system
# 的消息"只能在这一层挡住。当前所有写入都出自 runtime，取值本身不会越界；这道校验是为了让将来
# 新增"导入对话""用户可写消息"之类的接口时，提权在数据层就被拒绝，而不是靠调用纪律兜着。
MESSAGE_ROLES = frozenset({"user", "assistant", "tool"})


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

        收尾必须写满三处，否则这次运行在界面上看起来像"悄无声息地断在半路"：
        runs 的状态、执行日志的收尾事件、以及回答尚未落地时的一条说明消息。
        三处并入同一个事务——半套收尾比不收尾更难排查。
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with self.engine.begin() as conn:
            # 先查再改：只有拿到具体的 run_id 与 session_id，才能给它们补轨迹与消息。
            abandoned = (await conn.execute(select(
                runs.c.id,
                runs.c.session_id
            ).where(runs.c.status.in_(["running", "cancel_requested"])))).fetchall()
            if not abandoned:
                return
            await conn.execute(update(runs).where(runs.c.id.in_([row[0] for row in abandoned])).values(
                status="interrupted",
                error=json.dumps({"code": "service_restarted"}),
                finished_at=utc_now()
            ))
            for run_id, session_id in abandoned:
                await self.add_trace(run_id, "run.finished", {
                    "status": "interrupted",
                    "code": "service_restarted"
                }, connection=conn)
                # 流式输出开跑时就落了一条 incomplete 的助手消息；只在完全没有助手消息时补一条，
                # 否则同一次运行会在界面上出现两条助手气泡。
                spoken = await conn.scalar(select(func.count()).select_from(messages).where(
                    messages.c.run_id == run_id,
                    messages.c.role == "assistant"
                ))
                if not spoken:
                    await self.add_message(session_id, run_id, "assistant", {
                        "content": ERROR_ANSWERS["service_restarted"],
                        "interrupted": True
                    }, connection=conn)

    async def close(self) -> None:
        await self.engine.dispose()

    async def create_session(
        self,
        model_name: str = "default",
        timezone_name: str = "Asia/Shanghai",
        title: str | None = None
    ) -> str:
        session_id = str(uuid4())
        async with self.engine.begin() as conn:
            await conn.execute(insert(sessions).values(
                id=session_id,
                model_name=model_name,
                timezone=timezone_name,
                title=title,
                created_at=utc_now()
            ))
        return session_id

    async def count_messages(self, session_id: str) -> int:
        async with self.engine.connect() as conn:
            return int(await conn.scalar(
                select(func.count()).select_from(messages).where(messages.c.session_id == session_id)
            ))

    async def set_session_title(self, session_id: str, title: str) -> None:
        # 标题来自用户消息首行，先压平换行并截断，避免把多行内容塞进标题栏。
        title = title.replace("\n", " ").replace("\r", " ").strip()[:200]
        if not title:
            return
        async with self.engine.begin() as conn:
            await conn.execute(update(sessions).where(sessions.c.id == session_id).values(title=title))

    async def delete_session(self, session_id: str) -> list[str] | None:
        """删除会话及其全部从属数据，返回待清理的资源文件键。

        返回 ``None`` 表示拒绝删除（会话正在运行），此时不留任何副作用。
        忙碌判定与所有数据行的删除必须落在同一个事务里，否则"判定通过 → 删除失败"和
        "判定拒绝 → 却已经删掉了别的东西"都无法回滚。
        资源文件本身不能在这里删——事务还没提交，磁盘清理交给调用方拿到文件键之后做。
        """
        async with self.engine.begin() as conn:
            # 有活跃运行时不允许删除，否则正在执行的 run 会写进已被清空的数据里。
            active = await conn.scalar(select(func.count()).select_from(runs).where(
                runs.c.session_id == session_id,
                runs.c.status.in_(["running", "cancel_requested"])
            ))
            if active:
                return None
            files = (await conn.execute(
                select(resources.c.file_key).where(resources.c.session_id == session_id)
            )).fetchall()
            # 删除顺序必须由"叶子"到"根"：先删引用 runs 的表，再删 messages/runs，
            # 然后是直接引用会话的表，最后删会话，否则会被外键约束挡住。
            await conn.execute(delete(tool_calls).where(
                tool_calls.c.run_id.in_(select(runs.c.id).where(runs.c.session_id == session_id))
            ))
            await conn.execute(delete(trace_events).where(
                trace_events.c.run_id.in_(select(runs.c.id).where(runs.c.session_id == session_id))
            ))
            await conn.execute(delete(messages).where(messages.c.session_id == session_id))
            await conn.execute(delete(runs).where(runs.c.session_id == session_id))
            await conn.execute(delete(summaries).where(summaries.c.session_id == session_id))
            await conn.execute(delete(todos).where(todos.c.session_id == session_id))
            await conn.execute(delete(resources).where(resources.c.session_id == session_id))
            result = await conn.execute(delete(sessions).where(sessions.c.id == session_id))
            if not result.rowcount:
                # 会话在判定之前就已经不存在：本次没有删除任何东西，按"拒绝"返回。
                return None
            return [file_key for (file_key,) in files]

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
            active = await conn.scalar(select(func.count()).select_from(runs).where(
                runs.c.session_id == session_id,
                runs.c.status.in_(["running", "cancel_requested"])
            ))
            if active:
                return False
            result = await conn.execute(
                update(sessions).where(sessions.c.id == session_id).values(model_name=model_name)
            )
            return bool(result.rowcount)

    async def add_message(
        self,
        session_id: str,
        run_id: str | None,
        role: str,
        payload: dict[str, Any],
        connection: Any = None
    ) -> int:
        """追加一条消息并返回它的 seq。

        seq 在会话内单调递增：既用于排序，也是前端的消息定位键（SSE 推送靠它对上号）。
        传入 ``connection`` 时可以并入调用方的事务（工具写入 + 消息落库要么同时成功、要么同时回滚）。
        """
        # 角色白名单：越界取值直接拒绝，避免"用户可指定角色"的接口一旦出现就变成提权通道。
        if role not in MESSAGE_ROLES:
            raise ValueError(f"invalid message role: {role}")

        async def write(conn: Any) -> int:
            # max(seq) + 1 在同一个写事务里计算，配合 (session_id, seq) 唯一约束避免并发下撞号。
            seq = int(await conn.scalar(select(func.coalesce(
                func.max(messages.c.seq),
                0
            ) + 1).where(messages.c.session_id == session_id)))
            await conn.execute(insert(messages).values(
                id=str(uuid4()),
                session_id=session_id,
                run_id=run_id,
                seq=seq,
                role=role,
                payload=json.dumps(payload, ensure_ascii=False),
                created_at=utc_now()
            ))
            return seq
        if connection is not None:
            return await write(connection)
        async with self.engine.begin() as conn:
            return await write(conn)

    async def update_message_fields(
        self,
        session_id: str,
        seq: int,
        fields: dict[str, Any],
        connection: Any = None
    ) -> bool:
        """按字段合并更新某条助手消息（保留未提到的字段）。

        流式输出需要"同一条消息被反复续写"：正文与思考过程分别增长，工具轮还要往同一条消息上补
        ``tool_calls``。用合并而不是整条替换，才能让这几路写入互不覆盖。
        """
        async def write(conn: Any) -> bool:
            existing = (await conn.execute(select(messages.c.payload).where(
                messages.c.session_id == session_id,
                messages.c.seq == seq,
                messages.c.role == "assistant"
            ))).first()
            if not existing:
                return False
            current = json.loads(existing[0]) if existing[0] else {}
            current.update(fields)
            # 空值代表"这一路还没有内容"：直接删掉字段而不是留空值，
            # 否则界面与测试都会看到一堆无意义的空字段。
            for key, value in fields.items():
                if not value:
                    current.pop(key, None)
            result = await conn.execute(update(messages).where(
                messages.c.session_id == session_id,
                messages.c.seq == seq,
                messages.c.role == "assistant"
            ).values(payload=json.dumps(current, ensure_ascii=False)))
            return bool(result.rowcount)
        if connection is not None:
            return await write(connection)
        async with self.engine.begin() as conn:
            return await write(conn)

    async def delete_message(self, session_id: str, seq: int) -> bool:
        """删除一条消息。

        流式输出是"先写后判"：内容边收边落库，等这一轮结束才知道模型给的是不是合法响应。
        判定为非法（或重试要从头再流一遍）时，必须把已经写出去的那半句撤回，
        否则界面上会留下一条幽灵回答。
        """
        async with self.engine.begin() as conn:
            result = await conn.execute(delete(messages).where(
                messages.c.session_id == session_id,
                messages.c.seq == seq
            ))
            return bool(result.rowcount)

    async def list_messages(self, session_id: str) -> list[dict[str, Any]]:
        """按 seq 升序返回消息，并把 JSON payload 摊平到顶层（role/seq/run_id 一并带出）。"""
        async with self.engine.connect() as conn:
            rows = (await conn.execute(
                select(messages).where(messages.c.session_id == session_id).order_by(messages.c.seq)
            )).mappings()
            return [{
                "role": row["role"],
                "seq": row["seq"],
                "run_id": row["run_id"],
                **json.loads(row["payload"])
            } for row in rows]

    async def start_run(self, session_id: str, text: str, request_key: str | None) -> tuple[dict[str, Any], bool]:
        """创建一次运行，返回 (run, 是否新建)。

        幂等：带相同 ``request_key`` 重复提交时直接返回已存在的 run（``created=False``），
        避免网络重试或重复点击产生两次回答。若同一个 key 却对应了不同内容，说明客户端用错了 key，
        直接报冲突而不是静默复用。
        """
        input_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        async with self.engine.begin() as conn:
            if request_key:
                existing = (await conn.execute(select(runs).where(
                    runs.c.session_id == session_id,
                    runs.c.request_key == request_key
                ))).mappings().first()
                if existing:
                    if existing["input_hash"] != input_hash:
                        raise ValueError("request_key_conflict")
                    return dict(existing), False
            # 一个会话同时只能跑一个 run：并发会让消息顺序与模型上下文错乱。
            active = await conn.scalar(select(func.count()).select_from(runs).where(
                runs.c.session_id == session_id,
                runs.c.status.in_(["running", "cancel_requested"])
            ))
            if active:
                raise RuntimeError("session_busy")
            # 模型名在创建 run 时快照下来：之后再改会话模型，不影响已经开始的运行。
            model_name = await conn.scalar(select(sessions.c.model_name).where(sessions.c.id == session_id))
            if model_name is None:
                raise LookupError("session_not_found")
            run = {
                "id": str(uuid4()),
                "session_id": session_id,
                "request_key": request_key,
                "input_hash": input_hash,
                "input_preview": text[:500],
                "model_name": model_name,
                "status": "running",
                "cancel_requested": 0,
                "model_calls": 0,
                "created_at": utc_now()
            }
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
            row = (await conn.execute(select(runs).where(
                runs.c.session_id == session_id
            ).order_by(runs.c.created_at.desc()).limit(1))).mappings().first()
            if not row:
                return None
            value = dict(row)
            value["error"] = json.loads(value["error"]) if value["error"] else None
            return value

    async def list_runs(self, session_id: str) -> list[dict[str, Any]]:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(select(runs).where(
                runs.c.session_id == session_id
            ).order_by(runs.c.created_at.desc()))).mappings()
            values = []
            for row in rows:
                value = dict(row)
                value["error"] = json.loads(value["error"]) if value["error"] else None
                values.append(value)
            return values

    async def request_cancel(self, run_id: str) -> bool:
        """标记"请求取消"。只置标志位，由运行时在循环里自行退出，避免强杀导致状态不一致。"""
        async with self.engine.begin() as conn:
            result = await conn.execute(update(runs).where(
                runs.c.id == run_id,
                runs.c.status == "running"
            ).values(status="cancel_requested", cancel_requested=1))
            return bool(result.rowcount)

    async def is_cancel_requested(self, run_id: str) -> bool:
        # 运行时每轮循环前查一次，作为协作式取消的检查点。
        async with self.engine.connect() as conn:
            return bool(await conn.scalar(select(runs.c.cancel_requested).where(runs.c.id == run_id)))

    async def finish_run(self, run_id: str, status: str, answer: str = "", error: dict[str, Any] | None = None) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(update(runs).where(runs.c.id == run_id).values(
                status=status,
                answer=answer,
                error=json.dumps(error, ensure_ascii=False) if error else None,
                finished_at=utc_now()
            ))

    async def add_trace(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        connection: Any = None
    ) -> int | None:
        """记录一条执行轨迹，返回它的事件 id。

        返回 id 是为了让埋点能把事件串成层级：调用方拿到"本轮请求模型"事件的 id 后，
        可以把它写进后续事件的 ``payload["parent_id"]``，前端因此不必靠顺序去猜归属。
        同样支持并入调用方事务，保证轨迹与业务数据一致（此时 id 在事务提交后才有意义）。
        取不到自增 id 时返回 ``None``，调用方据此退化为不带层级，不影响事件本身落库。
        """
        statement = insert(trace_events).values(
            run_id=run_id,
            event_type=event_type,
            payload=json.dumps(payload, ensure_ascii=False),
            created_at=utc_now()
        )
        if connection is not None:
            return _inserted_id(await connection.execute(statement))
        async with self.engine.begin() as conn:
            return _inserted_id(await conn.execute(statement))

    async def list_trace(self, run_id: str) -> list[dict[str, Any]]:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(
                select(trace_events).where(trace_events.c.run_id == run_id).order_by(trace_events.c.id)
            )).mappings()
            return [{
                "id": row["id"],
                "event_type": row["event_type"],
                "created_at": row["created_at"],
                "payload": json.loads(row["payload"])
            } for row in rows]

    async def list_session_trace(self, session_id: str) -> list[dict[str, Any]]:
        """整个会话的轨迹（执行日志页用），按事件 id 升序即时间顺序。"""
        async with self.engine.connect() as conn:
            rows = (await conn.execute(select(
                trace_events.c.id,
                trace_events.c.run_id,
                trace_events.c.event_type,
                trace_events.c.created_at,
                trace_events.c.payload
            ).join(runs, runs.c.id == trace_events.c.run_id).where(
                runs.c.session_id == session_id
            ).order_by(trace_events.c.id))).mappings()
            return [{
                "id": row["id"],
                "run_id": row["run_id"],
                "event_type": row["event_type"],
                "created_at": row["created_at"],
                "payload": json.loads(row["payload"])
            } for row in rows]

    async def get_tool_call(self, run_id: str, call_id: str) -> dict[str, Any] | None:
        """查已有的工具调用记录：模型重复请求同一个调用时，直接复用结果而不重复执行。"""
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(tool_calls).where(
                tool_calls.c.run_id == run_id,
                tool_calls.c.call_id == call_id
            ))).mappings().first()
            return dict(row) if row else None

    async def save_tool_call(
        self,
        run_id: str,
        call_id: str,
        name: str,
        args: dict[str, Any],
        result: ToolResult,
        connection: Any = None
    ) -> None:
        # 参数排序后序列化：同一组参数在不同顺序下入库存成同一串，便于比对与排查。
        statement = insert(tool_calls).values(
            run_id=run_id,
            call_id=call_id,
            name=name,
            arguments=json.dumps(args, ensure_ascii=False, sort_keys=True),
            result=json.dumps(result.__dict__, ensure_ascii=False),
            status="succeeded" if result.ok else "failed"
        )
        if connection is not None:
            await connection.execute(statement)
            return
        async with self.engine.begin() as conn:
            await conn.execute(statement)

    async def latest_summary(self, session_id: str) -> dict[str, Any] | None:
        # 取版本号最大的那份；covered_through_seq 决定哪些历史已被摘要覆盖。
        async with self.engine.connect() as conn:
            row = (await conn.execute(select(summaries).where(
                summaries.c.session_id == session_id
            ).order_by(summaries.c.version.desc()).limit(1))).mappings().first()
            return dict(row) if row else None

    async def save_summary(self, session_id: str, covered_through_seq: int, content: str) -> None:
        # 只做追加、不覆盖旧版本，便于回溯"当时摘要成了什么"。
        async with self.engine.begin() as conn:
            version = int(await conn.scalar(select(func.coalesce(
                func.max(summaries.c.version),
                0
            ) + 1).where(summaries.c.session_id == session_id)))
            await conn.execute(insert(summaries).values(
                id=str(uuid4()),
                session_id=session_id,
                version=version,
                covered_through_seq=covered_through_seq,
                content=content,
                created_at=utc_now()
            ))
