"""持久层的表结构与迁移入口。

三个 Store 共用这里的表对象；数据库 DDL 的权威来源是 ``migrations/``，这里只提供
一组可引用的 ``Table``、统一的时间戳格式，以及启动时的迁移调用。
"""
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table, Text, UniqueConstraint


# 表结构在这里以 Core 方式声明（不引入 ORM 映射），与实际 DDL 的权威来源是 migrations/。
metadata = MetaData()
# 会话：一次对话的容器，绑定一个模型与一个时区。
sessions = Table(
    "sessions",
    metadata,
    Column("id", String, primary_key=True),
    Column("model_name", String, nullable=False),
    Column("timezone", String, nullable=False),
    Column("title", String, nullable=True),
    Column("created_at", String, nullable=False)
)
# 运行：一条用户消息对应一次 run。(session_id, request_key) 唯一 —— 幂等去重的落点。
runs = Table(
    "runs",
    metadata,
    Column("id", String, primary_key=True),
    Column("session_id", ForeignKey("sessions.id"), nullable=False, index=True),
    Column("request_key", String),
    Column("input_hash", String, nullable=False),
    Column("input_preview", Text, nullable=False),
    Column("model_name", String, nullable=False),
    Column("status", String, nullable=False),
    Column("answer", Text),
    Column("error", Text),
    Column("cancel_requested", Integer, nullable=False, default=0),
    Column("model_calls", Integer, nullable=False, default=0),
    Column("created_at", String, nullable=False),
    Column("finished_at", String),
    UniqueConstraint("session_id", "request_key")
)
# 消息：payload 存 JSON（content / tool_calls / thinking 等）。seq 是会话内递增序号，前端按它定位消息。
messages = Table(
    "messages",
    metadata,
    Column("id", String, primary_key=True),
    Column("session_id", ForeignKey("sessions.id"), nullable=False, index=True),
    Column("run_id", ForeignKey("runs.id")),
    Column("seq", Integer, nullable=False),
    Column("role", String, nullable=False),
    Column("payload", Text, nullable=False),
    Column("created_at", String, nullable=False),
    UniqueConstraint("session_id", "seq")
)
# 工具调用：(run_id, call_id) 联合主键，同一个调用重复出现时可以查回来做"复用"提示。
tool_calls = Table(
    "tool_calls",
    metadata,
    Column("run_id", ForeignKey("runs.id"), primary_key=True),
    Column("call_id", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("arguments", Text, nullable=False),
    Column("result", Text),
    Column("status", String, nullable=False)
)
# 摘要：version 递增保留历史版本，covered_through_seq 记录"已压缩到哪一条消息"。
summaries = Table(
    "summaries",
    metadata,
    Column("id", String, primary_key=True),
    Column("session_id", ForeignKey("sessions.id"), nullable=False, index=True),
    Column("version", Integer, nullable=False),
    Column("covered_through_seq", Integer, nullable=False),
    Column("content", Text, nullable=False),
    Column("created_at", String, nullable=False),
    UniqueConstraint("session_id", "version")
)
# 待办：按会话隔离。
todos = Table(
    "todos",
    metadata,
    Column("id", String, primary_key=True),
    Column("session_id", ForeignKey("sessions.id"), nullable=False, index=True),
    Column("text", Text, nullable=False),
    Column("status", String, nullable=False),
    Column("created_at", String, nullable=False)
)
# 资源：正文存磁盘文件（file_key），这里只留索引与大小。
resources = Table(
    "resources",
    metadata,
    Column("id", String, primary_key=True),
    Column("session_id", ForeignKey("sessions.id"), nullable=False, index=True),
    Column("kind", String, nullable=False),
    Column("file_key", String, nullable=False),
    Column("size", Integer, nullable=False),
    Column("created_at", String, nullable=False)
)
# 轨迹事件：执行日志的数据源，id 自增保证按发生顺序读取。
trace_events = Table(
    "trace_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", ForeignKey("runs.id"), nullable=False, index=True),
    Column("event_type", String, nullable=False),
    Column("payload", Text, nullable=False),
    Column("created_at", String, nullable=False)
)

def utc_now() -> str:
    """统一的时间戳格式（ISO 8601 UTC），字符串排序即时间排序。"""
    return datetime.now(timezone.utc).isoformat()


def _inserted_id(result: Any) -> int | None:
    """取刚插入行的自增主键，取不到就返回 None。

    不同方言（SQLite 的 lastrowid、Postgres 的 RETURNING）暴露方式不一致，
    用 ``inserted_primary_key`` 统一取；万一某方言不提供，也只是失去层级信息，
    不该让"记一条轨迹"整体失败。
    """
    try:
        return int(result.inserted_primary_key[0])
    except (TypeError, IndexError, KeyError, AttributeError):
        return None


def migrate_database(db_path: Path) -> None:
    """把数据库升级到最新迁移版本。

    用 Alembic 而不是 ``create_all``，保证本地 SQLite 与线上 Postgres 走同一套 DDL 演进。
    注意这里用的是同步连接串：迁移在应用启动前跑，不需要 async。
    """
    # 本文件位于 backend/mini_agent/storage/ 下，alembic.ini 与 migrations/ 在 backend/ 里，故上溯三层。
    backend_dir = Path(__file__).resolve().parents[2]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "migrations"))
    config.attributes["connection_url"] = f"sqlite:///{db_path.resolve().as_posix()}"
    command.upgrade(config, "head")
