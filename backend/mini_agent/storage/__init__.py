"""持久层：SQLite（默认）/Postgres（通过 Alembic 迁移）之上的数据访问。

对外只暴露这几个名字；内部按职责拆成 schema / store / todos / resources 四个模块，
调用方（api、runtime、context、migrations）一律从这里导入，保持导入路径稳定。
"""
from .resources import ResourceStore
from .schema import metadata, migrate_database, utc_now
from .store import Store
from .todos import TodoStore

__all__ = [
    "ResourceStore",
    "Store",
    "TodoStore",
    "metadata",
    "migrate_database",
    "utc_now",
]
