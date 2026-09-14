"""Alembic 运行环境。

连接串的解析顺序（优先级从高到低）：

1. ``config.attributes["connection_url"]`` —— 应用启动时由 ``storage.migrate_database`` 注入，
   这样迁移用的库与运行时用的是**同一个文件**，避免"迁了 A 库、跑的是 B 库"；
2. ``MINI_AGENT_MIGRATION_URL`` 环境变量 —— 用于 CI 或指向 Postgres；
3. 都没有时回退到 ``load_config().data_dir / state.db``。

迁移使用同步连接（迁移发生在应用启动前，不需要 async）。
"""
import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url

from mini_agent.config import load_config
from mini_agent.storage import metadata

config = context.config
# 让 alembic.ini 里的日志配置生效，否则迁移过程完全没有输出。
if config.config_file_name:
    fileConfig(config.config_file_name)
url = config.attributes.get("connection_url") or os.environ.get("MINI_AGENT_MIGRATION_URL")
if not url:
    db_path = load_config().data_dir / "state.db"
    url = f"sqlite:///{db_path.as_posix()}"
config.set_main_option("sqlalchemy.url", url)
if url.startswith("sqlite"):
    # SQLite 不会自动建目录：首次运行前先把数据目录准备好。
    database_path = make_url(url).database
    if database_path and database_path != ":memory:":
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
# 表结构的"真源"是 storage.metadata，迁移脚本通过它建表/比对。
target_metadata = metadata

def run_migrations_offline() -> None:
    """离线模式：只生成 SQL（用 literal_binds 把参数直接写进语句），不连数据库。"""
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online() -> None:
    """在线模式：真连数据库执行迁移。NullPool 避免迁移连接被池化后长期占着锁。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        # compare_type=True：字段类型变化也要被 autogenerate 检测到。
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()

run_migrations_offline() if context.is_offline_mode() else run_migrations_online()
