from pathlib import Path

import pytest

from mini_agent.storage import ResourceStore, Store, TodoStore, migrate_database
from mini_agent.tools import build_registry


@pytest.fixture
async def services(tmp_path: Path):
    db_path = tmp_path / "state.db"
    migrate_database(db_path)
    store = Store(db_path)
    await store.init()
    resources = ResourceStore(store, tmp_path / "resources")
    registry = build_registry(TodoStore(store), resources)
    try:
        yield store, resources, registry
    finally:
        await store.close()
