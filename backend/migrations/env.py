import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url

from mini_agent.config import load_config
from mini_agent.storage import metadata

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
url = config.attributes.get("connection_url") or os.environ.get("MINI_AGENT_MIGRATION_URL")
if not url:
    db_path = load_config().data_dir / "state.db"
    url = f"sqlite:///{db_path.as_posix()}"
config.set_main_option("sqlalchemy.url", url)
if url.startswith("sqlite"):
    database_path = make_url(url).database
    if database_path and database_path != ":memory:":
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
target_metadata = metadata

def run_migrations_offline() -> None:
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()

run_migrations_offline() if context.is_offline_mode() else run_migrations_online()
